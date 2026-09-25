#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
#
# SniffMe Bridge
#
# Copyright (C) 2026 Mark Kuebel
#
# Use of this software is governed by the Business Source License
# included in the LICENSE file at the root of this repository and at
# https://mariadb.com/bsl11/. On the Change Date stated there, use
# of this software will be governed by the Apache License, Version 2.0.
"""SniffMe Bridge: TCP to Omara Scent Studio.

Runtime path:

    game watcher or any emitter
      -> TCP line-delimited JSON  {"odor":"sweet","intensity":1.0}
      -> this Python bridge
      -> Omara Scent Studio WebSocket

Design goals:

  * THREADS, NOT ASYNCIO. On Windows a wedged WebSocket operation inside an
    event loop can starve the TCP accept loop and the reconnect logic at once
    -- the "system freeze" symptom. Every role gets its own OS thread, so no
    single stalled socket operation can stop any other:

        main thread          -> TCP accept loop + lifecycle + Ctrl-C
        client handler x N   -> one per connected client (daemon)
        omara-worker thread  -> owns the WebSocket: connect/send/recv/close

  * NOTHING EVER BLOCKS A CALLER. All cross-thread handoff is a bounded
    queue.Queue; when a queue is full the OLDEST item is dropped (and
    counted), never the caller's thread. An emitter's write can never be
    stalled by Omara being down, slow, or half-dead.

  * EAGER WEBSOCKET LIFECYCLE. Connects to Omara Scent
    Studio when the server starts, keeps the connection alive with automatic
    reconnect + exponential backoff whenever it drops, and closes it cleanly
    when the bridge exits. No first-send connect surprises.

  * EVERY SOCKET CALL IS TIME-BOUNDED. Connects use open_timeout; sends are
    bounded by the underlying socket timeout (set after handshake); receives
    poll with an explicit timeout. A dead peer is detected and replaced, not
    waited on forever.

  * CRASH-PROOF THREADS. Every loop body catches every exception, logs it,
    and continues. A malformed line, a broken client, or an Omara hiccup can
    never take down the bridge. Console output (including emoji in cinematic
    mode) is wrapped so even a cp1252 Windows terminal cannot raise.

Requires:
    py -m pip install -r requirements.txt   (websockets >= 16)
"""

from __future__ import annotations

import argparse
import json
import math
import queue
import socket
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

try:
    from websockets.sync.client import connect as ws_sync_connect
except ImportError:  # pragma: no cover - clear message beats a traceback later
    ws_sync_connect = None

DEFAULT_TCP_HOST = "127.0.0.1"
DEFAULT_TCP_PORT = 8765
DEFAULT_OMARA_WS_URL = "ws://127.0.0.1:8080/"

VALID_ODORS = {
    "marine", "petrichor", "kindred", "beach", "floral", "sweet", "barnyard",
    "winter", "evergreen", "terra_silva", "citrus", "desert", "savory_spice",
    "timber", "smoky", "machina",
}

ODOR_EMOJIS = {
    "marine": "\U0001F30A", "petrichor": "\U0001F327️", "kindred": "\U0001F91D",
    "beach": "\U0001F3D6️", "floral": "\U0001F338", "sweet": "\U0001F36C",
    "barnyard": "\U0001F404", "winter": "❄️", "evergreen": "\U0001F332",
    "terra_silva": "\U0001F33F", "citrus": "\U0001F34B", "desert": "\U0001F3DC️",
    "savory_spice": "\U0001F336️", "timber": "\U0001FA95", "smoky": "\U0001F4A8",
    "machina": "⚙️",
}

WS_QUEUE_SIZE = 256          # pending sends to Omara; oldest dropped when full
CLIENT_LINE_MAX = 4096       # max bytes per client line before it is discarded
CLIENT_BUFFER_MAX = CLIENT_LINE_MAX * 8   # runaway framing guard

STATS_LOCK = threading.Lock()  # guards ALL shared stats/dispatcher counters

# Set by main()/tests to make the accept loop exit cleanly (Ctrl-C parity).
SHUTDOWN = threading.Event()



@dataclass(frozen=True)
class ScentCommand:
    odor: str
    intensity: float
    sequence: int

    @property
    def omara_json(self) -> dict[str, object]:
        return {"odor": self.odor, "intensity": self.intensity}

    @property
    def omara_json_text(self) -> str:
        return json.dumps(self.omara_json, separators=(",", ":"))


@dataclass
class BridgeStats:
    received: int = 0
    emitted: int = 0
    real_sent: int = 0
    dry_sent: int = 0
    dropped_invalid: int = 0
    dropped_zero: int = 0
    dropped_rate_limit: int = 0
    dropped_backpressure: int = 0
    failures: int = 0
    reconnects: int = 0

    @property
    def dropped_total(self) -> int:
        return (self.dropped_invalid + self.dropped_zero
                + self.dropped_rate_limit + self.dropped_backpressure)


class Logger:
    """Lock-guarded, encoding-safe console output for all three modes."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self._lock = threading.Lock()
        self.last_intensity_by_odor: dict[str, float] = {}

    def _emit(self, message: str, *, to_stderr: bool = False, force: bool = False) -> None:
        if self.mode == "off" and not force:
            return
        try:
            stream = sys.stderr if to_stderr else sys.stdout
            print(message, file=stream, flush=True)
        except Exception:
            # A hostile terminal (cp1252 emoji, closed handle) must never raise
            # out of a worker thread. Strip to ASCII and try once more.
            try:
                safe = message.encode("ascii", "replace").decode("ascii")
                print(safe, flush=True)
            except Exception:
                pass

    def status(self, message: str) -> None:
        self._emit(message)

    def error(self, message: str) -> None:
        self._emit(message, to_stderr=True)

    def totals_line(self, stats: BridgeStats) -> str:
        return (
            f"received={stats.received} emitted={stats.emitted} "
            f"real={stats.real_sent} dry={stats.dry_sent} "
            f"dropped_total={stats.dropped_total} "
            f"dropped_invalid={stats.dropped_invalid} "
            f"dropped_zero={stats.dropped_zero} "
            f"dropped_rate_limit={stats.dropped_rate_limit} "
            f"dropped_backpressure={stats.dropped_backpressure} "
            f"failures={stats.failures} reconnects={stats.reconnects}"
        )

    def drop(self, reason: str, raw: str, stats: BridgeStats) -> None:
        if self.mode == "off":
            return
        raw = raw.strip()
        if len(raw) > 180:
            raw = raw[:177] + "..."
        with self._lock:
            self._emit(f"[drop] reason={reason} raw={raw} {self.totals_line(stats)}")

    def emitted(self, *, mode: str, command: ScentCommand, stats: BridgeStats,
                response: Optional[dict[str, Any]] = None) -> None:
        if self.mode == "off":
            return
        with self._lock:
            if self.mode == "cinematic":
                self._emit(self.format_cinematic(command=command, stats=stats))
            else:
                response_text = ""
                if response is not None:
                    ok = bool(response.get("ok"))
                    response_text = f" response={'ok' if ok else 'fail'}"
                self._emit(
                    f"[{mode}] {command.omara_json_text} "
                    f"{self.totals_line(stats)}{response_text}"
                )

    def format_cinematic(self, *, command: ScentCommand, stats: BridgeStats) -> str:
        intensity_pct = round(command.intensity * 100)
        blocks_away = max(0, min(5, round((1.0 - command.intensity) * 5)))
        block_word = "block" if blocks_away == 1 else "blocks"
        previous_intensity = self.last_intensity_by_odor.get(command.odor)
        odor_emoji = ODOR_EMOJIS.get(command.odor, "\U0001F443")
        scent_label = f"{odor_emoji} {command.odor.replace('_', ' ')}"

        if intensity_pct >= 90:
            narrative = (f"\U0001F6A8 MAXIMUM DETECTED! {scent_label} emitted "
                         f"at {intensity_pct}% intensity! \U0001F4A5")
        elif previous_intensity is not None and command.intensity > previous_intensity:
            narrative = (f"\U0001F525 Getting closer! {scent_label} increased "
                         f"to {intensity_pct}% intensity. \U0001F06F")
        elif previous_intensity is not None and command.intensity < previous_intensity:
            narrative = (f"\U0001F32B️ Getting further... {scent_label} decreased "
                         f"to {intensity_pct}% intensity. \U0001F07D")
        elif previous_intensity is not None and command.intensity == previous_intensity:
            narrative = (f"\U0001F3AF Steady signal: {scent_label} holding "
                         f"at {intensity_pct}% intensity.")
        elif blocks_away <= 2 and intensity_pct > 30:
            narrative = (f"\u2728 Item nearby! {scent_label} detected about "
                         f"{blocks_away} {block_word} away at "
                         f"{intensity_pct}% intensity. \U0001F4CD")
        elif blocks_away >= 3:
            narrative = (f"\U0001F443 Faint {scent_label} scent detected about "
                         f"{blocks_away} {block_word} away at "
                         f"{intensity_pct}% intensity. \U0001F32C️")
        else:
            narrative = (f"\U0001F3AF Steady signal: {scent_label} emitted "
                         f"at {intensity_pct}% intensity.")

        self.last_intensity_by_odor[command.odor] = command.intensity
        bar = "\u2501" * 78
        return (f"\n{bar}\n  {narrative}\n"
                f"  \U0001F4E5 received={stats.received}  "
                f"\U0001F4E4 emitted={stats.emitted}\n{bar}")

    def cinematic_banner(self, rate_limit_seconds: float) -> str:
        if rate_limit_seconds == 0:
            rate_str = "UNLIMITED (no cooldown)"
        elif rate_limit_seconds < 1:
            rate_str = f"{int(rate_limit_seconds * 1000)}ms cooldown"
        else:
            rate_str = f"1 per {rate_limit_seconds}s cooldown"
        title = ("\U0001F9A7 SniffMe Bridge \u2014 "
                 "\U0001F443 Spraying what it smells")
        rate_line = f"\u23F1️ Rate limit: {rate_str}"
        subtitle = ("\U0001F9EA Cooldown commands are queued and merged by "
                    "strongest scent")
        max_len = max(len(title), len(rate_line), len(subtitle)) + 6
        bar = "\u2501" * max_len
        return f"\n{bar}\n  {title}\n  {rate_line}\n  {subtitle}\n{bar}"


def parse_command(raw_line: bytes, sequence: int
                  ) -> tuple[Optional[ScentCommand], Optional[str], str]:
    raw_text = raw_line.decode("utf-8", errors="replace").strip()
    if not raw_text:
        return None, "empty", raw_text
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return None, "invalid_json", raw_text
    if not isinstance(parsed, dict):
        return None, "json_not_object", raw_text

    odor = parsed.get("odor")
    intensity = parsed.get("intensity")
    if not isinstance(odor, str):
        return None, "missing_or_invalid_odor", raw_text
    odor = odor.strip()
    if not odor:
        return None, "missing_or_invalid_odor", raw_text
    if odor not in VALID_ODORS:
        return None, f"unknown_odor:{odor}", raw_text

    try:
        intensity_float = float(intensity)
    except (TypeError, ValueError):
        return None, "missing_or_invalid_intensity", raw_text
    if not math.isfinite(intensity_float):
        return None, "non_finite_intensity", raw_text
    if intensity_float <= 0:
        return None, "zero_intensity", raw_text
    if intensity_float > 1:
        return None, "intensity_above_1", raw_text

    return (ScentCommand(odor=odor, intensity=round(intensity_float, 4),
                         sequence=sequence), None, raw_text)


class OmaraWorker(threading.Thread):
    """Owns the WebSocket: eager connect, send queue, recv poll, clean close.

    All socket work happens in this one thread (websockets' sync client is
    not safe to drive from several threads at once). The queue between TCP
    handlers and here is bounded; when it fills, the oldest queued scent is
    dropped so no producer ever blocks.
    """

    def __init__(self, *, url: str, logger: Logger, stats: BridgeStats,
                 connect_timeout: float, send_timeout: float,
                 keepalive_pings: bool, wait_response: bool,
                 response_timeout: float) -> None:
        super().__init__(name="omara-worker", daemon=True)
        self.url = url
        self.logger = logger
        self.stats = stats
        self.connect_timeout = connect_timeout
        self.send_timeout = send_timeout
        self.keepalive_pings = keepalive_pings
        self.wait_response = wait_response
        self.response_timeout = response_timeout

        self._queue: queue.Queue[Optional[ScentCommand]] = queue.Queue(maxsize=WS_QUEUE_SIZE)
        self._stop = threading.Event()
        self._conn: Any = None
        self._ever_connected = False
        self._connected_reported = False

    # -- producer side (never blocks) ------------------------------------
    def submit(self, command: ScentCommand) -> bool:
        """Queue a scent for sending. Returns False if backpressure dropped it."""
        try:
            self._queue.put_nowait(command)
            return True
        except queue.Full:
            # Drop the OLDEST queued scent to make room for this fresher one.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(command)
            except (queue.Empty, queue.Full):
                pass
            with STATS_LOCK:
                self.stats.dropped_backpressure += 1
            return False

    def queue_size(self) -> int:
        return self._queue.qsize()

    # -- lifecycle ---------------------------------------------------------
    def stop_and_join(self, timeout: float = 5.0) -> None:
        self._stop.set()
        try:
            self._queue.put(None)          # wake a blocking get, if any
        except queue.Full:
            pass
        self.join(timeout=timeout)

    def _open_connection(self) -> Any:
        if ws_sync_connect is None:
            raise RuntimeError("Missing dependency: pip install websockets")
        ping_interval = 20.0 if self.keepalive_pings else None
        ping_timeout = 5.0 if self.keepalive_pings else None
        # Plain connect (NOT a pre-made socket= argument): on websockets 16.x
        # the sock= path leaves the connection dying ~2s after handshake, and
        # open_timeout already bounds the TCP connect + handshake anyway.
        return ws_sync_connect(
            self.url,
            open_timeout=self.connect_timeout,
            ping_interval=ping_interval, ping_timeout=ping_timeout,
            close_timeout=0.5,
        )

    def _close_connection(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            if self._connected_reported:
                self._connected_reported = False
                self.logger.status("Omara WebSocket closed")

    def _ensure_connected(self) -> bool:
        if self._conn is not None:
            return True
        try:
            self._conn = self._open_connection()
            if self._ever_connected:
                with STATS_LOCK:
                    self.stats.reconnects += 1
            self._ever_connected = True
            if not self._connected_reported:
                self.logger.status(f"Connected to Omara Scent Studio at {self.url}")
                self._connected_reported = True
            return True
        except Exception as exc:
            self.logger.error(f"[ws] connect failed: {exc}")
            return False

    def _send_one(self, command: ScentCommand) -> None:
        conn = self._conn
        assert conn is not None
        payload = command.omara_json_text

        # Watchdog: if the send wedges longer than send_timeout (a half-dead
        # peer that never ACKs and never RSTs), terminate the socket from this
        # guard thread so conn.send raises immediately instead of hanging.
        watchdog_stop = threading.Event()
        def _watchdog() -> None:
            if not watchdog_stop.wait(self.send_timeout):
                self.logger.error(
                    f"[ws] send stalled past {self.send_timeout}s, terminating socket")
                try:
                    conn.close_socket(None)
                except Exception:
                    pass
        watchdog = threading.Thread(target=_watchdog, daemon=True)
        watchdog.start()
        try:
            conn.send(payload)
        except Exception as exc:
            watchdog_stop.set()
            with STATS_LOCK:
                self.stats.failures += 1
            self.logger.error(f"[ws] send failed, will reconnect: {exc}")
            self._close_connection()
            return
        watchdog_stop.set()

        if not self.wait_response:
            with STATS_LOCK:
                self.stats.real_sent += 1
                self.stats.emitted += 1
            self.logger.emitted(mode="sent", command=command,
                                stats=self._snapshot())
            return

        # wait_response: the next inbound message is this send's response.
        try:
            raw = conn.recv(timeout=self.response_timeout)
        except Exception as exc:
            with STATS_LOCK:
                self.stats.failures += 1
            self.logger.error(f"[ws] no response, will reconnect: {exc}")
            self._close_connection()
            return
        response = self._parse_response(raw)
        ok = bool(response.get("ok")) if response is not None else False
        with STATS_LOCK:
            if ok:
                self.stats.real_sent += 1
                self.stats.emitted += 1
            else:
                self.stats.failures += 1
        self.logger.emitted(mode="sent", command=command, stats=self._snapshot(),
                            response=response)

    @staticmethod
    def _parse_response(raw: Any) -> Optional[dict[str, Any]]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"non-JSON response: {raw!r}"}
        return parsed if isinstance(parsed, dict) else {"ok": False, "error": repr(parsed)}

    def _snapshot(self) -> BridgeStats:
        with STATS_LOCK:
            return BridgeStats(**{k: getattr(self.stats, k) for k in
                                  self.stats.__dataclass_fields__})

    # -- the thread body ----------------------------------------------------
    def run(self) -> None:
        backoff = 0.5
        self._ever_connected = False
        while not self._stop.is_set():
            try:
                if not self._ensure_connected():
                    # Reconnect with exponential backoff, capped at 10s.
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2.0, 10.0)
                    continue
                backoff = 0.5

                try:
                    command = self._queue.get(timeout=0.25)
                except queue.Empty:
                    command = None

                if command is None:
                    # Idle: drain inbound messages (responses, broadcasts,
                    # keepalive pongs) so nothing piles up in the socket.
                    if self._conn is not None:
                        try:
                            self._conn.recv(timeout=0.05)
                        except Exception as exc:
                            if not _is_timeout(exc):
                                self.logger.error(
                                    f"[ws] inbound lost, will reconnect: {exc}")
                                self._close_connection()
                    continue

                if self._conn is None:          # stopped mid-queue
                    break
                self._send_one(command)
            except Exception as exc:            # belt and braces: never die
                self.logger.error(f"[ws] worker error (continuing): {exc}")
                self._close_connection()
                self._stop.wait(0.25)

        # Shutdown: close cleanly so Omara sees a proper WebSocket close.
        self._close_connection()

    def drain_now(self, timeout: float = 1.0) -> None:
        """Best-effort flush of queued sends during shutdown (worker still alive)."""
        deadline = time.monotonic() + timeout
        while self.queue_size() > 0 and time.monotonic() < deadline:
            time.sleep(0.05)


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or type(exc).__name__ in (
        "TimeoutError", "WebSocketTimeoutException")


class Dispatcher:
    """Rate limiting + window merging, shared by all TCP handler threads.

    All mutable state (sequence, stats counters, pending window) is guarded
    by the single STATS_LOCK that the Omara worker also uses, so totals are
    always consistent and no field has two competing guards.
    """

    def __init__(self, *, rate_limit_seconds: float, window_mode: str,
                 logger: Logger, stats: BridgeStats,
                 worker: Optional[OmaraWorker], no_send: bool) -> None:
        self.rate_limit_seconds = rate_limit_seconds
        self.window_mode = window_mode
        self.logger = logger
        self.stats = stats
        self.worker = worker
        self.no_send = no_send

        self.sequence = 0
        self.last_emit_at: Optional[float] = None
        self.pending: Optional[ScentCommand] = None
        self.pending_received = 0

    def submit_raw_line(self, raw_line: bytes) -> None:
        with STATS_LOCK:
            self.sequence += 1
            sequence = self.sequence
            self.stats.received += 1

        command, drop_reason, raw_text = parse_command(raw_line, sequence)
        if command is None:
            with STATS_LOCK:
                if drop_reason == "zero_intensity":
                    self.stats.dropped_zero += 1
                else:
                    self.stats.dropped_invalid += 1
            self.logger.drop(drop_reason or "invalid", raw_text, self._snapshot())
            return

        self.submit_command(command)

    def submit_command(self, command: ScentCommand) -> None:
        emit_now = False
        now = time.monotonic()
        with STATS_LOCK:
            if self.rate_limit_seconds <= 0:
                self.last_emit_at = now
                emit_now = True
            elif (self.pending_received == 0 and
                  (self.last_emit_at is None or
                   now - self.last_emit_at >= self.rate_limit_seconds)):
                self.last_emit_at = now
                emit_now = True
            else:
                self.pending_received += 1
                self.pending = self._choose_pending(self.pending, command)
        if emit_now:
            self.emit(command)

    def _choose_pending(self, current: Optional[ScentCommand],
                       incoming: ScentCommand) -> ScentCommand:
        if current is None:
            return incoming
        if self.window_mode == "most-recent":
            return incoming
        if incoming.intensity > current.intensity:
            return incoming
        if (incoming.intensity == current.intensity and
                incoming.sequence > current.sequence):
            return incoming
        return current

    def flush_due(self) -> None:
        """Emit the merged pending command once its cooldown has elapsed."""
        to_emit = None
        now = time.monotonic()
        with STATS_LOCK:
            if self.pending is not None and (
                    self.last_emit_at is None or
                    now - self.last_emit_at >= self.rate_limit_seconds):
                to_emit = self.pending
                window_received = self.pending_received
                self.pending = None
                self.pending_received = 0
                self.last_emit_at = now
                # Record the merged drops immediately so stats are consistent.
                self.stats.dropped_rate_limit += max(0, window_received - 1)
            else:
                window_received = 0
        if to_emit is not None:
            self.emit(to_emit)

    def flush_final(self) -> None:
        """Force-emit any pending command during shutdown."""
        with STATS_LOCK:
            to_emit = self.pending
            self.pending = None
            self.pending_received = 0
        if to_emit is not None:
            self.emit(to_emit, final=True)

    def emit(self, command: ScentCommand, *, final: bool = False) -> None:
        if self.no_send or self.worker is None:
            with STATS_LOCK:
                self.stats.dry_sent += 1
                self.stats.emitted += 1
            self.logger.emitted(mode="dry-run", command=command,
                                stats=self._snapshot())
            return
        self.worker.submit(command)

    def _snapshot(self) -> BridgeStats:
        with STATS_LOCK:
            return BridgeStats(**{k: getattr(self.stats, k) for k in
                                  self.stats.__dataclass_fields__})


class ClientHandler(threading.Thread):
    """One per connected client. Reads lines, never blocks the accept loop."""

    def __init__(self, *, sock: socket.socket, peer: str,
                 dispatcher: Dispatcher, logger: Logger,
                 registry: "ClientRegistry") -> None:
        super().__init__(name=f"client-{peer}", daemon=True)
        self.sock = sock
        self.peer = peer
        self.dispatcher = dispatcher
        self.logger = logger
        self.registry = registry
        self._killed = threading.Event()

    def run(self) -> None:
        self.logger.status(f"Client connected from {self.peer}")
        buf = b""
        try:
            while not self._killed.is_set():
                try:
                    chunk = self.sock.recv(1024)
                except (TimeoutError, socket.timeout):
                    continue
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    self.dispatcher.submit_raw_line(
                        line[:CLIENT_LINE_MAX] if len(line) > CLIENT_LINE_MAX else line)
                if len(buf) > CLIENT_BUFFER_MAX:      # never framed; discard runaway
                    buf = b""
            if not self._killed.is_set():
                self.logger.status(f"Client disconnected from {self.peer}")
        except Exception as exc:
            if not self._killed.is_set():
                self.logger.error(f"[tcp] client {self.peer} error (ignored): {exc}")
        finally:
            try:
                self.sock.close()
            except Exception:
                pass
            self.registry.forget(self)

    def kill(self) -> None:
        self._killed.set()
        try:
            self.sock.close()   # makes the blocked recv above raise/return
        except Exception:
            pass


class ClientRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: set[ClientHandler] = set()
        self.stopping = False

    def remember(self, client: ClientHandler) -> None:
        with self._lock:
            self._clients.add(client)

    def forget(self, client: ClientHandler) -> None:
        with self._lock:
            self._clients.discard(client)

    def kill_all(self) -> None:
        self.stopping = True
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.kill()


def run_bridge(args: argparse.Namespace) -> int:
    logger = Logger(args.output)
    stats = BridgeStats()

    if args.output == "cinematic":
        logger.status(logger.cinematic_banner(args.rate_limit_seconds))
    elif args.output == "pretty":
        logger.status("Omara Yellow bridge v2 (threaded)")
        logger.status(f"mode={'dry-run' if args.no_send else 'send'}")
        logger.status(f"tcp={args.tcp_host}:{args.tcp_port}")
        logger.status(f"omara_ws={args.omara_ws}")
        logger.status(f"rate_limit={args.rate_limit_seconds}s "
                      f"window={args.rate_limit_window}")
        logger.status(f"keepalive_pings={'on' if args.keepalive_pings else 'off'}")
        logger.status(f"wait_response={'on' if args.wait_response else 'off'}")

    # Eager WebSocket lifecycle: connect at startup (unless dry-run), close at exit.
    worker: Optional[OmaraWorker] = None
    if not args.no_send:
        worker = OmaraWorker(
            url=args.omara_ws, logger=logger, stats=stats,
            connect_timeout=args.connect_timeout, send_timeout=args.send_timeout,
            keepalive_pings=args.keepalive_pings, wait_response=args.wait_response,
            response_timeout=args.response_timeout,
        )
        worker.start()

    dispatcher = Dispatcher(
        rate_limit_seconds=args.rate_limit_seconds,
        window_mode=args.rate_limit_window,
        logger=logger, stats=stats, worker=worker, no_send=args.no_send,
    )

    registry = ClientRegistry()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((args.tcp_host, args.tcp_port))
    except OSError as exc:
        logger.error(f"fatal: cannot bind {args.tcp_host}:{args.tcp_port}: {exc}")
        if worker is not None:
            worker.stop_and_join()
        return 1
    listener.listen(4)
    listener.settimeout(0.5)   # lets Ctrl-C interrupt the accept loop
    logger.status(f"TCP listening on {args.tcp_host}:{args.tcp_port}")

    try:
        while not SHUTDOWN.is_set():
            try:
                client_sock, peer = listener.accept()
            except (TimeoutError, socket.timeout):
                dispatcher.flush_due()
                continue
            except OSError:
                if registry.stopping:
                    break
                raise
            client_sock.settimeout(None)   # handlers use blocking recv + kill()
            handler = ClientHandler(sock=client_sock, peer=f"{peer[0]}:{peer[1]}",
                                    dispatcher=dispatcher, logger=logger,
                                    registry=registry)
            registry.remember(handler)
            handler.start()
    except KeyboardInterrupt:
        logger.status("interrupted, shutting down")
    finally:
        registry.kill_all()
        try:
            listener.close()
        except Exception:
            pass
        dispatcher.flush_final()
        if worker is not None:
            worker.drain_now(timeout=1.0)
            worker.stop_and_join(timeout=5.0)
        if args.output == "pretty":
            logger.status("totals " + logger.totals_line(stats))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Threaded TCP JSON to Omara Scent Studio bridge: eager "
                    "WebSocket connect at start, clean close at exit, automatic "
                    "reconnect.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--tcp-host", default=DEFAULT_TCP_HOST, help="host for TCP input")
    parser.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT, help="port for TCP input")
    parser.add_argument("--omara-ws", default=DEFAULT_OMARA_WS_URL, help="Omara Scent Studio WebSocket URL")

    parser.add_argument("--rate-limit-seconds", type=float, default=2.0, help="minimum seconds between emitted Omara commands (0 disables)")
    parser.add_argument("--rate-limit-window", choices=("strongest", "most-recent"), default="strongest", help="which queued command wins inside the rate-limit window")

    parser.add_argument("--output", choices=("pretty", "cinematic", "off"), default="pretty", help="terminal output mode")
    parser.add_argument("--no-send", action="store_true", help="dry-run mode; do not connect or send to Omara Scent Studio")

    parser.add_argument("--connect-timeout", type=float, default=2.0, help="seconds to wait when connecting to Omara Scent Studio")
    parser.add_argument("--send-timeout", type=float, default=1.0, help="seconds bound on a WebSocket send (socket timeout)")
    parser.add_argument("--response-timeout", type=float, default=1.0, help="seconds to wait for Omara response when --wait-response is enabled")
    parser.add_argument("--wait-response", action="store_true", help="wait for Omara Studio WebSocket responses after each send")
    parser.add_argument("--keepalive-pings", action="store_true", help="enable WebSocket keepalive pings; disabled by default")
    return parser


def main() -> int:
    # Emoji output must survive a cp1252 Windows terminal instead of raising.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = build_parser()
    args = parser.parse_args()
    if args.rate_limit_seconds < 0:
        parser.error("--rate-limit-seconds must be >= 0")
    try:
        return run_bridge(args)
    except KeyboardInterrupt:
        print("bye", file=sys.stderr, flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
