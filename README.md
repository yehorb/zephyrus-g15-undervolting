# undervolting

Tools for undervolting the RTX 3080 Laptop GPU in an ASUS ROG Zephyrus G15 (GA503QS) on Linux
with [LACT](https://github.com/ilya-zlobintsev/LACT): they record gaming sessions and analyze
how the GPU behaved under each undervolt profile.

## Layout

- `tools/gamelog.py` — logs GPU/CPU load, clocks, voltage, power limits, temps and fans during a session
- `tools/analyze_session.py` — summarizes or compares sessions
- `tools/mangohud.conf` — MangoHud frame-time logging config
- `tools/lact_api.py` — minimal client for the LACT daemon socket
- `data/` — local raw data, git-ignored:
  - `logs/` — session logs from `gamelog.py`, MangoHud logs in `logs/mangohud/`

## Usage

Requires [uv](https://docs.astral.sh/uv/) and a running LACT daemon (user in `wheel`).

```sh
uv run tools/gamelog.py warframe                          # start before the game; stops 5 min after it closes
uv run tools/analyze_session.py                           # newest session
uv run tools/analyze_session.py data/logs/A data/logs/B   # compare at equal power limit
```

Steam launch option for frame logging:

```text
MANGOHUD_CONFIGFILE=$HOME/Projects/undervolting/tools/mangohud.conf mangohud %command%
```
