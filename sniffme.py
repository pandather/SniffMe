#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright (C) 2026 Mark Kuebel
# Use of this software is governed by the Business Source License
# included in the LICENSE file at the root of this repository and at
# https://mariadb.com/bsl11/. On the Change Date stated there, use
# of this software will be governed by the Apache License, Version 2.0.
"""SniffMe: the full scent pipeline.

Architecture:

  Tier 1 - event detection, no model (~4 Hz): grab the screen (or just the
    located game window once known), compare a colour histogram to the
    baseline from the last VLM call, and track darkness so loading screens
    are recognized between calls. Stable scenes cost nothing.

  Tier 2 - structured VLM call on events: JSON-schema-constrained decoding
    guarantees {game, scene, menu_or_loading, indoors, machine_within_5ft,
    odor, intensity}. No regex archaeology. The VLM's reported game name is
    matched against window titles; once located, subsequent frames crop to
    that window at full resolution for far better detail per token.

  Policy - deterministic rules in code, hot-reloadable data files:
    * machina only when machine_within_5ft (schema boolean), else beach
      indoors / silence outdoors
    * menus/loading emit nothing; a sustained-dark -> bright transition with
      live gameplay fires ONE sweet arrival cue
    * game_overrides.json may pin scent choices for known games+scenes
    * repeated sprays of the held scent every --spray-interval are intentional
    * cartridge accounting warns before you spray a cart dry

  Eval - --eval DIR scores saved frames against labels so prompt/model changes
    are measured, not guessed (works with any --model for A/B runs).

Requires: Pillow. Talks to bridge.py's TCP port.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import io
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime

from PIL import Image, ImageGrab

log = logging.getLogger("sniffme")

VALID_ODORS = [
    "marine", "petrichor", "kindred", "beach", "floral", "sweet", "barnyard",
    "winter", "evergreen", "terra_silva", "citrus", "desert", "savory_spice",
    "timber", "smoky", "machina",
]

STUDIO_KEY_FILE = os.path.expandvars(
    r"C:\Users\%USERNAME%\.unsloth\studio\auth\agent_api_key.json")


def load_token() -> str:
    env = os.environ.get("SNIFFME_API_KEY")
    if env:
        return env.strip()
    with open(STUDIO_KEY_FILE, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    server = doc["servers"]["http://127.0.0.1:8888"]
    toks = server.get("minted") or server.get("saved") or []
    if not toks:
        raise RuntimeError(f"no API token found in {STUDIO_KEY_FILE}")
    return toks[-1]


# ---------------------------------------------------------------------------
# Palette
DISPLAY_TO_KEY = {
    "winter": "winter", "barnyard": "barnyard", "sweet": "sweet",
    "floral": "floral", "beach": "beach", "kindred": "kindred",
    "petrichor": "petrichor", "marine": "marine", "evergreen": "evergreen",
    "terra silva": "terra_silva", "citrus": "citrus", "desert": "desert",
    "savory spice": "savory_spice", "timber": "timber", "smoky": "smoky",
    "machina": "machina",
}


def load_palette(path: str) -> dict[str, str]:
    """Parse 'Name / SMELLS LIKE / USE IN GAMES' blocks into {key: text}."""
    palette: dict[str, str] = {}
    cur_key: str | None = None
    cur_lines: list[str] = []

    def close() -> None:
        nonlocal cur_key, cur_lines
        if cur_key is not None:
            joined = " ".join(cur_lines)
            palette[cur_key] = " ".join(joined.split())
        cur_key, cur_lines = None, []

    with open(path, "r", encoding="utf-8-sig") as fh:
        for raw in fh.read().splitlines():
            line = raw.strip()
            if not line:
                continue
            low = line.lower()
            key = DISPLAY_TO_KEY.get(low)
            if key and not low.startswith(("smells", "use")):
                close()
                cur_key = key
                continue
            if cur_key is not None:
                cur_lines.append(line)
    close()
    missing = set(VALID_ODORS) - set(palette)
    if missing:
        raise RuntimeError(f"palette {path} missing cartridges: {sorted(missing)}")
    return palette


# ---------------------------------------------------------------------------
# Windows helpers
user32 = ctypes.windll.user32


def foreground_title() -> str:
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value or ""
    except Exception:
        return ""


def list_windows() -> list[tuple[int, str]]:
    """Visible, non-iconic top-level windows: [(hwnd, title), ...]."""
    out: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        try:
            if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
                n = user32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    t = buf.value.strip()
                    if t:
                        out.append((hwnd, t))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(cb, 0)
    except Exception:
        pass
    return out


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    r = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return r.left, r.top, r.right, r.bottom


def find_game_window(game_name: str) -> int | None:
    """Locate a window whose title contains the game name (or vice versa)."""
    if not game_name:
        return None
    tokens = [t for t in re.split(r"\W+", game_name.lower()) if len(t) >= 3]
    best, best_score = None, 0.0
    for hwnd, title in list_windows():
        tl = title.lower()
        score = 0.0
        name_l = game_name.lower()
        if name_l in tl or tl.startswith(name_l.split(":")[0]):
            score += 10.0
        hits = sum(1 for t in tokens if t in tl)
        score += hits / max(1, len(tokens)) * 5.0
        # emulator windows are excellent candidates too
        if "mgba" in tl or "emulator" in tl:
            score += 4.0
        if score > best_score and score >= 3.0:
            best, best_score = hwnd, score
    return best


# ---------------------------------------------------------------------------
class FrameSource(threading.Thread):
    """Grabs frames every interval; keeps latest JPEG + colour histogram + seq.

    Optionally crops to a window rectangle supplied by get_rect() (absolute
    virtual-desktop coords) so the game fills the frame instead of 15% of a
    4K desktop."""

    HIST_SIDE = 64
    BINS = 8

    def __init__(self, interval: float, max_side: int, quality: int,
                 get_rect=None) -> None:
        super().__init__(name="framesource", daemon=True)
        self.interval = interval
        self.max_side = max_side
        self.quality = quality
        self.get_rect = get_rect
        self.latest: bytes | None = None
        self.hist: list[float] | None = None
        self.mean_lum: float = 0.0
        self.seq = 0                      # increments per real grab
        self.cropped = False
        self._stop = threading.Event()

    @classmethod
    def histogram(cls, img: Image.Image) -> tuple[list[float], float]:
        small = img.convert("RGB").resize((cls.HIST_SIDE, cls.HIST_SIDE))
        raw = small.tobytes()  # RGB bytes, stride 3 (no deprecated getdata)
        hist = [0.0] * (3 * cls.BINS)
        lum = 0.0
        n = len(raw) // 3
        for i in range(0, len(raw), 3):
            r, g, b = raw[i], raw[i + 1], raw[i + 2]
            lum += 0.114 * r + 0.587 * g + 0.299 * b
            hist[min(cls.BINS - 1, r * cls.BINS // 256)] += 1
            hist[cls.BINS + min(cls.BINS - 1, g * cls.BINS // 256)] += 1
            hist[2 * cls.BINS + min(cls.BINS - 1, b * cls.BINS // 256)] += 1
        total = float(n)
        return [h / total for h in hist], lum / total

    @staticmethod
    def distance(a: list[float], b: list[float]) -> float:
        """L1 histogram distance in [0, 1]; 0 = identical distributions."""
        return 0.5 * sum(abs(x - y) for x, y in zip(a, b))

    def run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                rect = self.get_rect() if self.get_rect else None
                img = None
                cropped = False
                if rect:
                    l, t, r, b = rect
                    w, h = r - l, b - t
                    if w > 64 and h > 64:
                        try:
                            img = ImageGrab.grab(bbox=(l, t, r, b),
                                                 all_screens=True)
                            cropped = True
                        except Exception:
                            img = None
                if img is None:
                    rect = None
                    img = ImageGrab.grab(all_screens=True)
                if max(img.size) > self.max_side:
                    scale = self.max_side / max(img.size)
                    img = img.resize((max(1, round(img.width * scale)),
                                      max(1, round(img.height * scale))))
                rgb = img.convert("RGB")
                hist, lum = self.histogram(rgb)
                buf = io.BytesIO()
                rgb.save(buf, format="JPEG", quality=self.quality)
                self.latest, self.hist, self.mean_lum = buf.getvalue(), hist, lum
                self.cropped = cropped
                self.seq += 1
            except Exception as exc:
                log.debug("grab failed (ignored): %s", exc)
            self._stop.wait(max(0.0, self.interval - (time.monotonic() - t0)))

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
class LoadingWatcher:
    """Darkness state machine for loading screens, updated every frame.

    Dark must persist `dark_frames` consecutive frames to count as loading;
    bright must persist `bright_frames` to end it (loading screens blink).
    On a dark->bright exit with live gameplay the caller fires one sweet."""

    def __init__(self, black_lumens: float, dark_frames: int = 3,
                 bright_frames: int = 2) -> None:
        self.black_lumens = black_lumens
        self.dark_frames = dark_frames
        self.bright_frames = bright_frames
        self._dark_run = 0
        self._bright_run = 0
        self.loading = False

    def update(self, mean_lum: float) -> str:
        """Returns 'none', 'enter_loading', or 'arrival'."""
        if mean_lum < self.black_lumens:
            self._dark_run += 1
            self._bright_run = 0
            if not self.loading and self._dark_run >= self.dark_frames:
                self.loading = True
                return "enter_loading"
            return "none"
        self._bright_run += 1
        self._dark_run = 0
        if self.loading and self._bright_run >= self.bright_frames:
            self.loading = False
            return "arrival"
        return "none"


# ---------------------------------------------------------------------------
SCENT_SCHEMA = {
    "name": "scent_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "game": {"type": ["string", "null"]},
            "scene": {"type": "string"},
            "menu_or_loading": {"type": "boolean"},
            "indoors": {"type": "boolean"},
            "machine_within_5ft": {"type": "boolean"},
            "odor": {"enum": VALID_ODORS + [None]},
            "intensity": {"type": "number"},
        },
        "required": ["game", "scene", "menu_or_loading", "indoors",
                     "machine_within_5ft", "odor", "intensity"],
        "additionalProperties": False,
    },
}

PROMPT_HEADER = """You are the scent designer for Omara, a device that sprays \
exactly one of these {n} cartridges: {odors}.

CARTRIDGE PALETTE (what each smells like; USE IN GAMES is authoritative):
{palette}

Look at this screenshot{where}. Decide, using the \
structured fields:
- game: the ONE video game visible (focused window wins, else largest), or \
null if none. Productivity apps, IDEs, chat, launchers with no active game, \
and wallpapers are not games.
- scene: max 8 words describing what is on screen.
- menu_or_loading: true when the game shows a menu, title, loading/black \
screen, logo, warning, or pause overlay instead of live gameplay.
- indoors: true when the player character is inside any building interior.
- machine_within_5ft: true ONLY when a vehicle, engine, factory equipment, or \
a dingy street alley is within about 5 feet (one or two strides) of the \
player character. Computer monitors, desks and offices do NOT count.
- odor: follow this ladder strictly: (a) if a notable object (plant, flower, \
food, animal, person being interacted with, vehicle, fire, water, snow, sand, \
dirt) is within 5 feet of the player, smell THAT thing per the palette; \
(b) else if indoors, use beach (clean indoor air); (c) else use the outdoor \
environment per the palette. If nothing clearly evokes any cartridge, null - \
silence beats a forced fit. When menu_or_loading is true, odor must be null.
- intensity: in (0, 1]: calm ambient ~0.2-0.45, normal gameplay ~0.5-0.7, \
explosive/climactic ~0.75-1. Never 0.

Answer with the structured object only.
"""


def build_prompt(palette: dict[str, str], where: str = "") -> str:
    lines = [f"- {k}: {palette[k]}" for k in sorted(palette)]
    return PROMPT_HEADER.format(n=len(VALID_ODORS),
                                odors=", ".join(sorted(VALID_ODORS)),
                                palette="\n".join(lines), where=where)


def clamp_decision(raw: dict) -> dict | None:
    """Normalize + policy-gate a schema-validated model answer."""
    game = raw.get("game")
    game = game.strip()[:80] if isinstance(game, str) and game.strip() else None
    scene = (raw.get("scene") or "").strip()[:60]
    odor = raw.get("odor")
    odor = odor.strip().lower() if isinstance(odor, str) else None
    try:
        intensity = float(raw.get("intensity"))
    except (TypeError, ValueError):
        intensity = 0.35

    if game is None or raw.get("menu_or_loading"):
        return {"game": game, "scene": scene, "odor": None, "intensity": 0.0}
    if odor is None:
        return {"game": game, "scene": scene, "odor": None, "intensity": 0.0}
    if odor not in VALID_ODORS:
        return None

    # Policy: machina requires a machine within 5 feet (schema boolean).
    if odor == "machina" and not bool(raw.get("machine_within_5ft")):
        odor = "beach" if bool(raw.get("indoors")) else None
        scene = (scene + " [policy]").strip()[:60]

    intensity = min(1.0, max(0.02, round(intensity, 3)))
    return {"game": game, "scene": scene, "odor": odor, "intensity": intensity}


# ---------------------------------------------------------------------------
class Overrides:
    """game_overrides.json: pin scents for known games+scenes. Hot-reloads."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.mtime = 0.0
        self.rules: list[dict] = []

    def reload(self) -> None:
        try:
            if not os.path.exists(self.path):
                return
            mtime = os.path.getmtime(self.path)
            if mtime == self.mtime:
                return
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            rules = []
            for entry in data:
                rules.append({
                    "game": str(entry.get("game", "")).lower(),
                    "scene": re.compile(str(entry.get("scene", "")), re.I)
                             if entry.get("scene") else None,
                    "odor": (entry.get("odor") or "").lower() or None,
                    "intensity": float(entry["intensity"])
                                 if entry.get("intensity") is not None else None,
                })
            self.rules = rules
            self.mtime = mtime
            print(f"[overrides] loaded {len(rules)} rule(s)", flush=True)
        except Exception as exc:
            log.warning("overrides reload failed (keeping previous): %s", exc)

    def apply(self, decision: dict) -> dict | None:
        """Return overridden decision when a rule matches, else None."""
        if not self.rules or decision.get("game") is None:
            return None
        game_l = decision["game"].lower()
        for rule in self.rules:
            if rule["game"] and rule["game"] not in game_l:
                continue
            if rule["scene"] and not rule["scene"].search(decision.get("scene", "")):
                continue
            out = dict(decision)
            if rule["odor"] is not None:
                if rule["odor"] == "null":
                    out["odor"] = None
                elif rule["odor"] in VALID_ODORS:
                    out["odor"] = rule["odor"]
            if rule["intensity"] is not None and out.get("odor"):
                out["intensity"] = min(1.0, max(0.02, round(rule["intensity"], 3)))
            return out
        return None


# ---------------------------------------------------------------------------
class Cartridges:
    """Crude fluid accounting: count sprays per cartridge, warn when low."""

    def __init__(self, path: str, capacity: int) -> None:
        self.path = path
        self.capacity = capacity
        self.counts: dict[str, int] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self.counts = {k: int(v) for k, v in json.load(fh).items()}
            except Exception as exc:
                log.warning("cartridge state load failed: %s", exc)

    def use(self, odor: str) -> None:
        self.counts[odor] = self.counts.get(odor, 0) + 1
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.counts, fh, indent=1)
        except OSError:
            pass
        used = self.counts[odor]
        if self.capacity > 0 and used == int(self.capacity * 0.8):
            print(f"[cartridge] WARNING {odor} at 80% of estimated capacity "
                  f"({used}/{self.capacity} sprays) - consider refilling",
                  flush=True)

    def remaining_pct(self, odor: str) -> float:
        if self.capacity <= 0:
            return 100.0
        used = self.counts.get(odor, 0)
        return max(0.0, (self.capacity - used) / self.capacity * 100.0)


# ---------------------------------------------------------------------------
class BridgeLink:
    def __init__(self, host: str, port: int) -> None:
        self.host, self.port, self.sock = host, port, None

    def send(self, odor: str, intensity: float) -> bool:
        payload = json.dumps({"odor": odor, "intensity": intensity},
                             separators=(",", ":")).encode() + b"\n"
        for _ in range(2):
            try:
                if self.sock is None:
                    self.sock = socket.create_connection((self.host, self.port),
                                                         timeout=2.0)
                self.sock.sendall(payload)
                return True
            except OSError as exc:
                log.warning("bridge send failed (%s); reconnecting", exc)
                try:
                    if self.sock:
                        self.sock.close()
                except OSError:
                    pass
                self.sock = None
        return False

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


# ---------------------------------------------------------------------------
class SniffMe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.token = load_token()
        self._game_hwnd: int | None = None
        self._rect_checked = 0.0
        self.frames = FrameSource(args.capture_interval, args.max_side,
                                  args.jpeg_quality, get_rect=self.game_rect)
        self.watcher = LoadingWatcher(args.black_lumens)
        self.history: deque[dict] = deque(maxlen=12)
        self._load_history()
        self.link = BridgeLink(args.bridge_host, args.bridge_port)
        self.overrides = Overrides(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "game_overrides.json"))
        self.overrides.reload()
        self.cartridges = Cartridges(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "cartridges.json"),
            args.cart_capacity)
        self.palette_mtime = 0.0
        self.palette: dict[str, str] = {}
        self.reload_palette(force=True)

    # -- game window location (crop source) ----------------------------------
    def set_game(self, game_name: str | None) -> None:
        hwnd = find_game_window(game_name) if game_name else None
        if hwnd != self._game_hwnd:
            self._game_hwnd = hwnd
            if hwnd:
                print(f"[window] cropping to game window for {game_name!r}",
                      flush=True)

    def game_rect(self) -> tuple[int, int, int, int] | None:
        """Checked at most ~2 Hz; returns absolute virtual-desktop rect."""
        now = time.monotonic()
        if self._game_hwnd is None or not user32.IsWindow(self._game_hwnd):
            return None
        if now - self._rect_checked > 0.5:
            self._rect_checked = now
            self._cached_rect = window_rect(self._game_hwnd)
        return getattr(self, "_cached_rect", None)

    # -- palette --------------------------------------------------------------
    def reload_palette(self, force: bool = False) -> None:
        try:
            mtime = os.path.getmtime(self.args.palette)
            if force or mtime != self.palette_mtime:
                self.palette = load_palette(self.args.palette)
                self.palette_mtime = mtime
                if not force:
                    print("[palette] reloaded after edit", flush=True)
        except Exception as exc:
            if not self.palette:
                raise
            log.warning("palette reload failed, keeping previous: %s", exc)

    # -- history ---------------------------------------------------------------
    def _history_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "recent_scents.jsonl")

    def _load_history(self) -> None:
        path = self._history_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()[-12:]
            for line in lines:
                try:
                    rec = json.loads(line)
                    if rec.get("odor"):
                        self.history.append(rec)
                except json.JSONDecodeError:
                    continue
        except OSError as exc:
            log.warning("history load failed (ignored): %s", exc)

    def remember(self, decision: dict) -> None:
        rec = dict(decision)
        rec["ts"] = datetime.now().isoformat(timespec="seconds")
        self.history.append(rec)
        try:
            with open(self._history_path(), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("history save failed (ignored): %s", exc)

    def history_text(self) -> str:
        if not self.history:
            return "Recent scent log: none yet."
        lines = [f"- {h['game']} | {h['odor']} @ {h['intensity']:.2f} | {h['scene']}"
                 for h in list(self.history)[-6:]]
        return ("Recent scent log (newest last), informational only:\n" +
                "\n".join(lines) +
                "\nNever copy intensity numbers; judge intensity fresh.")

    # -- auth -------------------------------------------------------------------
    def refresh_token(self) -> None:
        try:
            self.token = load_token()
            log.info("refreshed Studio API token")
        except Exception as exc:
            log.error("token refresh failed: %s", exc)

    # -- structured VLM call ------------------------------------------------------
    def ask_vlm(self, frame: bytes, cropped: bool) -> dict | None:
        self.reload_palette()
        where = (" (cropped to the game window)" if cropped else "")
        content = [{"type": "text", "text": build_prompt(self.palette, where)},
                   {"type": "text", "text": "Desktop context (hint only):\n" +
                       json.dumps({"foreground_window": foreground_title(),
                                   "open_windows": [t for _, t in
                                                    list_windows()[:20]]},
                                  ensure_ascii=False) + "\n\n" +
                       self.history_text()},
                   {"type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," +
                                         base64.b64encode(frame).decode()}}]
        payload = {
            "model": self.args.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.0,
            "max_tokens": self.args.max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema",
                                "json_schema": SCENT_SCHEMA},
        }
        url = f"{self.args.base_url.rstrip('/')}/chat/completions"

        def _request() -> urllib.request.Request:
            return urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.token}"})

        for attempt in range(2):
            try:
                with urllib.request.urlopen(_request(),
                                            timeout=self.args.vlm_timeout) as r:
                    out = json.loads(r.read())
                msg = (out["choices"][0]["message"].get("content") or "").strip()
                if msg.startswith("'") and msg.endswith("'"):
                    msg = msg[1:-1]  # grammar quote artifacts
                try:
                    raw = json.loads(msg)
                except json.JSONDecodeError:
                    log.warning("model returned non-JSON despite schema: %r",
                                msg[:120])
                    return None
                return clamp_decision(raw) if isinstance(raw, dict) else None
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    self.refresh_token()
                    continue
                body = ""
                try:
                    body = exc.read().decode(errors="replace")[:200]
                except Exception:
                    pass
                log.error("HTTP %s: %s", exc.code, body)
                return None
            except Exception as exc:
                log.error("VLM call failed: %s", exc)
                return None
        return None

    # -- main loop ------------------------------------------------------------------
    def run(self) -> int:
        self.frames.start()
        print(f"SniffMe up. model={self.args.model} "
              f"spray_interval={self.args.spray_interval}s "
              f"change_threshold={self.args.change_threshold} "
              f"heartbeat={self.args.max_vlm_interval}s "
              f"mode={'DRY-RUN' if self.args.no_send else 'LIVE'}", flush=True)

        held: dict | None = None
        baseline_hist: list[float] | None = None
        last_vlm = 0.0
        last_emit = 0.0
        seen_seq = -1
        calls = grabs = 0
        try:
            while True:
                deadline = time.monotonic() + self.args.capture_interval * 4 + 1
                while self.frames.seq == seen_seq or self.frames.hist is None:
                    if time.monotonic() > deadline:
                        break
                    time.sleep(0.02)
                if self.frames.latest is None or self.frames.seq == seen_seq:
                    continue
                seen_seq = self.frames.seq
                frame, hist = self.frames.latest, self.frames.hist
                grabs += 1
                now = time.monotonic()

                # Loading state machine runs every frame (no model).
                wstate = self.watcher.update(self.frames.mean_lum)

                changed = (baseline_hist is None or
                           FrameSource.distance(hist, baseline_hist) >=
                           self.args.change_threshold)
                heartbeat = (now - last_vlm) >= self.args.max_vlm_interval

                if frame and (changed or heartbeat or wstate == "arrival"):
                    t0 = time.monotonic()
                    decision = self.ask_vlm(frame, self.frames.cropped)
                    calls += 1
                    dt = time.monotonic() - t0
                    last_vlm = now
                    baseline_hist = hist

                    if decision is not None:
                        self.set_game(decision.get("game"))
                        # Arrival cue: sustained dark -> bright + live gameplay.
                        if (wstate == "arrival" and decision["odor"] is not None
                                and not self.watcher.loading):
                            arrival = {"game": decision["game"],
                                       "scene": ("load complete: " +
                                                 decision["scene"])[:60],
                                       "odor": "sweet", "intensity": 0.5}
                            # one-shot guard: skip if a recent sweet exists for game
                            if not any(h.get("odor") == "sweet" and
                                       str(h.get("game", "")).lower() in
                                       arrival["game"].lower()
                                       for h in list(self.history)[-4:]):
                                decision = arrival

                    if decision is None:
                        print(f"[{dt:.1f}s] VLM no usable decision "
                              f"(grabs={grabs} calls={calls})", flush=True)
                    elif decision["odor"] is None:
                        held = None
                        reason = ("no game" if decision["game"] is None else
                                  f"{decision['game']}: menu/loading/no match")
                        print(f"[{dt:.1f}s] {reason} -> silence "
                              f"(grabs={grabs} calls={calls})", flush=True)
                    else:
                        override = self.overrides.apply(decision)
                        if override is not None:
                            decision = override
                        held = decision
                        self.remember(decision)
                        last_emit = 0.0
                        print(f"[{dt:.1f}s] decided {decision['game']}: "
                              f"{decision['odor']}@{decision['intensity']:.2f} "
                              f"({decision['scene']}) (grabs={grabs} "
                              f"calls={calls})", flush=True)

                # Repeated sprays of the held scent are intentional.
                if held and time.monotonic() - last_emit >= self.args.spray_interval:
                    last_emit = time.monotonic()
                    tag = (f"{held['game']}: {held['odor']} "
                           f"@{held['intensity']:.2f}")
                    pct = self.cartridges.remaining_pct(held["odor"])
                    if self.args.no_send:
                        print(f"DRY-RUN respray {tag} [{pct:.0f}% fluid]",
                              flush=True)
                    else:
                        self.cartridges.use(held["odor"])
                        if self.link.send(held["odor"], held["intensity"]):
                            print(f"SPRAY {tag} [{pct:.0f}% fluid]", flush=True)
                        else:
                            print(f"{tag} -> bridge unreachable", file=sys.stderr)
        except KeyboardInterrupt:
            print("\nbye", file=sys.stderr)
            return 130
        finally:
            self.frames.stop()
            self.link.close()


# ---------------------------------------------------------------------------
def run_eval(args: argparse.Namespace) -> int:
    """Score saved frames against labels.jsonl in DIR (one frame per line).

    labels.jsonl entries: {"file": "x.jpg", "game": "...", "odor": "..."} -
    odor may be null (expect silence). Prints per-case and accuracy."""
    d = args.eval
    lpath = os.path.join(d, "labels.jsonl")
    if not os.path.exists(lpath):
        print(f"--eval {d}: no labels.jsonl", file=sys.stderr)
        return 2
    app = SniffMe.__new__(SniffMe)
    app.args = args
    app.token = load_token()
    app.palette_mtime, app.palette = 0.0, {}
    app.reload_palette(force=True)
    app.history = deque(maxlen=12)
    app._game_hwnd = None
    good = total = 0
    with open(lpath, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            fpath = os.path.join(d, entry["file"])
            try:
                with open(fpath, "rb") as img_fh:
                    frame = img_fh.read()
            except OSError:
                print(f"missing {fpath}", file=sys.stderr)
                continue
            total += 1
            got = app.ask_vlm(frame, cropped=False)
            want_game = (entry.get("game") or "").lower()
            want_odor = entry.get("odor")
            got_odor = got["odor"] if got else None
            ok = ((want_odor is None and got_odor is None) or
                  (want_odor and got_odor == want_odor))
            if want_game and got:
                ok = ok and want_game in str(got.get("game", "")).lower()
            good += bool(ok)
            print(f"{'PASS' if ok else 'FAIL'} {entry['file']}: "
                  f"want={want_odor} got={got_odor}"
                  + (f" ({got.get('scene')})" if got else ""), flush=True)
    if total:
        print(f"accuracy {good}/{total} = {100.0 * good / total:.0f}%")
    return 0 if good == total else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SniffMe: full scent pipeline")
    p.add_argument("--model", default="unsloth/Qwen3.8-Flash-Next-GGUF")
    p.add_argument("--palette", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "pandather_smell_descriptions.txt"))
    p.add_argument("--base-url", default="http://127.0.0.1:8888/v1")
    p.add_argument("--bridge-host", default="127.0.0.1")
    p.add_argument("--bridge-port", type=int, default=8765)
    p.add_argument("--capture-interval", type=float, default=0.25)
    p.add_argument("--max-side", type=int, default=1024)
    p.add_argument("--jpeg-quality", type=int, default=72)
    p.add_argument("--vlm-timeout", type=float, default=90.0)
    p.add_argument("--max-tokens", type=int, default=300)
    p.add_argument("--spray-interval", type=float, default=5.0,
                   help="seconds between repeated sprays of the held scent")
    p.add_argument("--change-threshold", type=float, default=0.18,
                   help="histogram distance (0-1) counting as a scene change")
    p.add_argument("--max-vlm-interval", type=float, default=20.0,
                   help="heartbeat: recompute at least this often if stable")
    p.add_argument("--black-lumens", type=float, default=18.0,
                   help="mean luminance below this counts as a loading screen")
    p.add_argument("--cart-capacity", type=int, default=500,
                   help="estimated sprays per cartridge (0 disables accounting)")
    p.add_argument("--dump-frames", default=None, metavar="DIR",
                   help="save each VLM-seen frame as JPEG in DIR")
    p.add_argument("--eval", default=None, metavar="DIR",
                   help="score DIR/labels.jsonl against the model and exit")
    p.add_argument("--no-send", action="store_true")
    return p


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args()
    if args.eval:
        return run_eval(args)
    return SniffMe(args).run()


if __name__ == "__main__":
    raise SystemExit(main())