# SniffMe

Watch your screen, smell the game. A Qwen VLM (Unsloth Studio llama.cpp server)
looks at your desktop four times a second and — if a video game is visible —
picks an Omara cartridge + intensity that matches the scene. Commands flow to
`bridge.py`, which talks to Omara Scent Studio over its WebSocket.

## Files
- `sniffme.py`     the watcher (Windows; needs `pip install pillow`)
- `bridge.py`      the TCP-to-Omara bridge
- `run.bat`            start the bridge
- `run_sniffme.bat`    start the watcher (bridge must be running, or use --no-send)
- `pandather_smell_descriptions.txt`  the scent palette (edit anytime; hot-reloads)
- `cartridges.json`     cartridge fill levels tracked across runs
- `game_overrides.json` optional per-game/per-scene scent pins (hot-reloads)

## Pipeline
1. Tier 1 (~4 Hz, no model): grab the screen — or just the located game window
   once known — and compare a colour histogram to the last baseline. Stable
   scenes cost nothing; darkness tracking recognises loading screens.
2. Tier 2 (on events): one JSON-schema-constrained VLM call returns
   `{game, scene, menu_or_loading, indoors, machine_within_5ft, odor,
   intensity}`. The reported game name is matched against window titles; once
   located, frames crop to that window at full resolution.
3. Policy (deterministic, in code): machina only within 5 feet of a real
   machine, else beach indoors / silence outdoors. Menus and loading screens
   emit nothing; a sustained-dark-to-bright transition with live gameplay
   fires one sweet arrival cue. The held scent re-sprays every
   --spray-interval; cartridge accounting warns before you spray a cart dry.

## Run
1. Start Omara Scent Studio (ws://127.0.0.1:8080).
2. Start the bridge: `run.bat`
3. Start the watcher: `run_sniffme.bat`

Dry run (decide + log, never spray): `run_sniffme.bat --no-send`

## Key flags
```
--no-send              never talk to bridge.py (log decisions only)
--palette FILE         scent palette file (default pandather_smell_descriptions.txt)
--capture-interval S   screenshot period (default 0.25)
--spray-interval S     re-spray held scent every S seconds (default 5)
--change-threshold F   histogram delta that triggers a VLM call (default 0.18)
--max-vlm-interval S   heartbeat: re-check even when stable (default 20)
--dump-frames DIR      save frames for --eval
--eval DIR             score saved frames against labels.jsonl
--model / --base-url   default unsloth/Qwen3.8-Flash-Next-GGUF @ 127.0.0.1:8888/v1
```

The API token is read from Unsloth Studio's minted agent key
(`~\.unsloth\studio\auth\agent_api_key.json`) and auto-refreshes on 401;
override with `SNIFFME_API_KEY`.

## License
Business Source License 1.1 — see [LICENSE](LICENSE). Non-commercial personal
use only until the Change Date (2030-09-24), when it converts to Apache-2.0.
