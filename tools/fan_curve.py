#!/usr/bin/env python3
"""Build an S-shaped (logistic) fan curve for asusctl, and optionally apply it.

The curve runs from --bottom % at START °C to --top % at FULL °C. By default an 8th point at
100 °C holds the top value: above its last point the EC runs the fan at 100 %, whatever the
curve says, so a capped curve needs that point far above gaming temperatures. --no-hold spreads
all 8 points over START..FULL instead (the fan then goes to 100 % above FULL).

Usage: uv run tools/fan_curve.py cpu 50 78 --top 83            # print the asusctl data string
       uv run tools/fan_curve.py gpu 45 80 --top 78 --apply    # set it for the profile and enable
"""

import argparse
import math
import subprocess

HOLD_TEMP = 100  # °C; the last point, so the EC's full-speed override never kicks in


def s_curve(start, full, bottom, top, steepness, hold):
    """8 (temp °C, fan %) points, logistic between start and full, normalized to bottom..top."""
    n = 7 if hold else 8
    mid, k = (start + full) / 2, steepness / (full - start)
    s = lambda x: 1 / (1 + math.exp(-k * (x - mid)))
    temps = [start + i * (full - start) / (n - 1) for i in range(n)]
    pts = [
        (
            round(t),
            round(bottom + (top - bottom) * (s(t) - s(start)) / (s(full) - s(start))),
        )
        for t in temps
    ]
    return pts + [(HOLD_TEMP, top)] if hold else pts


def asusctl(*args):
    subprocess.run(
        ["asusctl", "fan-curve", *args], check=True, stdout=subprocess.DEVNULL
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fan", choices=["cpu", "gpu", "mid"])
    ap.add_argument("start", type=int, help="°C where the curve leaves --bottom")
    ap.add_argument("full", type=int, help="°C where the curve reaches --top")
    ap.add_argument(
        "--bottom", type=int, default=13, help="fan %% at START (default 13)"
    )
    ap.add_argument("--top", type=int, default=100, help="fan %% at FULL (default 100)")
    ap.add_argument(
        "--steepness",
        type=float,
        default=10,
        help="logistic slope across START..FULL; higher = sharper S (default 10)",
    )
    ap.add_argument(
        "--no-hold",
        dest="hold",
        action="store_false",
        help=f"no {HOLD_TEMP} °C point: the EC runs the fan at 100 %% above FULL",
    )
    ap.add_argument(
        "--apply", action="store_true", help="write it with asusctl and enable"
    )
    ap.add_argument(
        "--profile", default="performance", help="asusd profile (default performance)"
    )
    a = ap.parse_args()
    if not (a.start < a.full < HOLD_TEMP and 0 <= a.bottom <= a.top <= 100):
        ap.error(f"need START < FULL < {HOLD_TEMP} and 0 <= bottom <= top <= 100")

    data = ",".join(
        f"{t}c:{p}%"
        for t, p in s_curve(a.start, a.full, a.bottom, a.top, a.steepness, a.hold)
    )
    print(data)
    if a.apply:
        asusctl("--mod-profile", a.profile, "--fan", a.fan, "--data", data)
        # writing a curve disables the profile's curves, so re-enable them
        asusctl("--mod-profile", a.profile, "--enable-fan-curves", "true")
        subprocess.run(["asusctl", "fan-curve", "--get-enabled"], check=False)


if __name__ == "__main__":
    main()
