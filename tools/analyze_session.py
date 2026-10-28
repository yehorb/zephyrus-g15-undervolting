#!/usr/bin/env python3
"""Summarize a gamelog.py session, plus any MangoHud logs recorded during it.

Usage: uv run tools/analyze_session.py [data/logs/<session-dir>]   (default: newest)
       uv run tools/analyze_session.py <dir> <dir> ...      (compare sessions)
"""

import datetime as dt
import json
import re
import sys
from pathlib import Path

import pandas as pd

LOGS = Path(__file__).resolve().parent.parent / "data" / "logs"
LOAD = 90  # busy % at which a sample counts as GPU-bound
BOGUS_POWER_W = 200  # the NVIDIA sensor occasionally reports ~750 W at idle
ACTIVE = 30  # busy % that marks the game running; idle before/after is trimmed
LOCAL_TZ = dt.datetime.now().astimezone().tzinfo

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)


def load_lact(d):
    df = pd.read_csv(d / "lact.csv")
    df["t"] = pd.to_datetime(df.timestamp, unit="ms", utc=True).dt.tz_convert(LOCAL_TZ)
    df.loc[df.power_w > BOGUS_POWER_W, "power_w"] = float("nan")
    df["power_cap"] = df.throttle.str.contains("POWER_CAP")
    df["thermal"] = df.throttle.str.contains("THERMAL")
    return df


def trim_idle(lact, nv):
    """Drop idle stretches before the game started and after it closed."""
    active = lact.t[lact.busy_pct >= ACTIVE]
    if active.empty:
        return lact, nv, None
    a, b = active.iloc[0], active.iloc[-1]
    idle = (a - lact.t.iloc[0]) + (lact.t.iloc[-1] - b)
    return (
        lact[lact.t.between(a, b)].copy(),
        nv[nv.t.between(a, b)].copy(),
        idle if idle > dt.timedelta(minutes=1) else None,
    )


def load_nvsmi(d):
    df = pd.read_csv(d / "nvsmi.csv", skipinitialspace=True)
    df.columns = [re.sub(r" \[.*\]$", "", c) for c in df.columns]
    df["t"] = pd.to_datetime(
        df.timestamp, format="%Y/%m/%d %H:%M:%S.%f"
    ).dt.tz_localize(LOCAL_TZ)
    for c in ("power.draw.instant", "power.draw.average"):
        df.loc[df[c] > BOGUS_POWER_W, c] = float("nan")
    return df


def load_mangohud(start, end):
    """MangoHud logs whose start falls inside the session; one row per frame."""
    frames = []
    for f in sorted((LOGS / "mangohud").glob("*.csv")):
        m = re.search(r"(\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d)\.csv$", f.name)
        if not m:
            continue
        t0 = pd.Timestamp(
            dt.datetime.strptime(m[1], "%Y-%m-%d_%H-%M-%S").replace(tzinfo=LOCAL_TZ)
        )
        if not start <= t0 <= end:
            continue
        df = pd.read_csv(f, skiprows=2)
        df["t"] = t0 + pd.to_timedelta(df.elapsed.to_numpy(dtype="int64"), unit="ns")
        df["file"] = f.name
        # first frames after startup carry absurd frame times (~84 000 s)
        frames.append(df[(df.frametime > 0) & (df.frametime < 5000)])
    return pd.concat(frames) if frames else None


def frame_stats(ft):
    """ft: frame times in ms."""
    return pd.Series(
        {
            "frames": len(ft),
            "avg_fps": 1000 / ft.mean(),
            "1%_low_fps": 1000 / ft.quantile(0.99),
            "0.1%_low_fps": 1000 / ft.quantile(0.999),
            "p99_ms": ft.quantile(0.99),
            "hitches>50ms": int((ft > 50).sum()),
            "hitches>100ms": int((ft > 100).sum()),
        }
    )


def by_limit(lact, nv):
    """GPU-bound samples grouped by the enforced power limit, for like-for-like comparisons."""
    j = pd.merge_asof(
        lact.sort_values("t"),
        nv[["t", "enforced.power.limit"]].sort_values("t"),
        on="t",
        direction="nearest",
        tolerance=dt.timedelta(seconds=1),
    )
    gb = j[j.busy_pct >= LOAD]
    out = gb.groupby(gb["enforced.power.limit"].round(-1).rename("limit_W")).agg(
        minutes=("t", lambda s: len(s) / 120),
        clk=("core_mhz", "mean"),
        mv=("mv", "median"),
        W=("power_w", "mean"),
        power_capped=("power_cap", lambda s: f"{100 * s.mean():.0f}%"),
        gpu_temp=("temp_gpu", "mean"),
        cpu_temp=("cpu_tctl", "mean"),
        cpu_W=("apu_power_w", "mean"),
    )
    return out[out.minutes >= 0.5]


def compare(dirs):
    rows = []
    for d in dirs:
        lact, nv, _ = trim_idle(load_lact(d), load_nvsmi(d))
        t = by_limit(lact, nv).reset_index()
        t.insert(0, "session", f"{lact.t.iloc[0]:%m-%d %H:%M} {lact.profile.mode()[0]}")
        rows.append(t)
    print(
        "GPU-bound samples by enforced power limit (compare rows with the same limit_W)"
    )
    print(pd.concat(rows).round(1).to_string(index=False))


def pct(s):
    return f"{100 * s.mean():.0f}%"


def main():
    if len(sys.argv) > 2:
        compare([Path(a) for a in sys.argv[1:]])
        return
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else max(LOGS.glob("2*_*/"))
    meta = json.loads((d / "meta.json").read_text())
    lact, nv, idle = trim_idle(load_lact(d), load_nvsmi(d))
    start, end = lact.t.iloc[0], lact.t.iloc[-1]
    gpu_cfg = next(iter(meta["lact_profile_settings"].get("gpus", {}).values()), {})

    print(f"# {d.name}")
    print(
        f"{start:%Y-%m-%d %H:%M} -> {end:%H:%M} ({(end - start).total_seconds() / 60:.0f} min) | "
        f"driver {meta['nvidia_driver_vbios']} | platform {meta['platform_profile']} | "
        f"AC {meta['ac_online']}"
    )
    if idle:
        print(
            f"(trimmed {idle.total_seconds() / 60:.0f} min of idle before/after the game)"
        )
    print(
        f"Profile at start: {meta['lact_profile']} (max {gpu_cfg.get('max_core_clock', 'stock')}, "
        f"offset {next(iter(gpu_cfg.get('gpu_clock_offsets', {}).values()), 0)})"
    )
    if lact.profile.nunique() > 1:
        print("Profile changes:", lact.groupby("profile").size().to_dict())
    if (lact.platform_profile != meta["platform_profile"]).any() or (
        lact.ac_online != 1
    ).any():
        print("WARNING: platform profile or AC state changed during the session")

    xids = [x for x in (d / "xid.log").read_text().splitlines() if "Xid" in x]
    print(f"Xid errors: {len(xids)}")
    for x in xids:
        print("  ", x)

    # --- GPU-bound samples -------------------------------------------------
    gb = lact[lact.busy_pct >= LOAD]
    cap = gpu_cfg.get("max_core_clock")
    print(
        f"\n## GPU-bound ({LOAD}%+ busy): {len(gb) / 2 / 60:.1f} min "
        f"of {len(lact) / 2 / 60:.1f} ({pct(lact.busy_pct >= LOAD)})"
    )
    if len(gb):
        print(
            f"clock  mean {gb.core_mhz.mean():.0f}, median {gb.core_mhz.median():.0f}, p10 "
            f"{gb.core_mhz.quantile(0.1):.0f} MHz"
            + (f", at cap {pct(gb.core_mhz >= cap)}" if cap else "")
        )
        print(
            f"volt   median {gb.mv.median():.0f} mV, range {gb.mv.min():.0f}-{gb.mv.max():.0f}"
        )
        print(
            f"power  mean {gb.power_w.mean():.1f} W, p95 {gb.power_w.quantile(0.95):.1f}, max {gb.power_w.max():.1f}"
        )
        print(
            f"limits power cap {pct(gb.power_cap)}, thermal {pct(gb.thermal)} of GPU-bound samples"
        )
        print(
            f"temps  GPU max {gb.temp_gpu.max():.0f} / hotspot {gb.temp_hotspot.max():.0f} / "
            f"VRAM {gb.temp_vram.max():.0f} °C, CPU Tctl max {gb.cpu_tctl.max():.0f} °C"
        )
        print(
            f"memory 6001 MHz {pct(gb.vram_mhz == 6001)} (P0), 5501 {pct(gb.vram_mhz == 5501)} (P3)"
        )
        nv_gb = nv[nv["utilization.gpu"] >= LOAD]
        print(
            "enforced power limit (W, share of GPU-bound time):",
            (
                nv_gb["enforced.power.limit"]
                .round(-1)
                .value_counts(normalize=True)
                .sort_index()
                .mul(100)
                .round(0)
                .astype(int)
                .to_dict()
            ),
        )

        print("\nV/F points actually used (GPU-bound):")
        vf = gb.groupby("core_mhz").agg(
            share=("mv", lambda s: f"{100 * len(s) / len(gb):.0f}%"),
            mv=("mv", "median"),
            power=("power_w", "mean"),
            xbar=("xbar_mhz", "median"),
            sys=("sys_mhz", "median"),
            video=("video_mhz", "median"),
            temp=("temp_gpu", "mean"),
        )
        print(
            vf[vf.index >= gb.core_mhz.quantile(0.02)]
            .sort_index(ascending=False)
            .round(1)
            .to_string()
        )
        print("\nBy enforced power limit (Dynamic Boost shifts it with CPU load):")
        print(by_limit(lact, nv).round(1).to_string())

    th = lact[lact.thermal]
    if len(th):
        print(
            f"\nThermal slowdown: {len(th) / 2:.0f} s total; GPU temp then {th.temp_gpu.min():.0f}-"
            f"{th.temp_gpu.max():.0f} °C, CPU Tctl {th.cpu_tctl.min():.0f}-{th.cpu_tctl.max():.0f} °C"
        )

    # --- frames ------------------------------------------------------------
    mh = load_mangohud(start - dt.timedelta(minutes=1), end)
    if mh is not None:
        print("\n## Frames (MangoHud)")
        print(
            mh.groupby("file")
            .frametime.apply(frame_stats)
            .unstack()
            .round(1)
            .to_string()
        )
        if mh.gpu_mem_clock.max() < 100:
            print(
                "NOTE: MangoHud GPU columns read the integrated GPU -- use tools/mangohud.conf"
            )
    else:
        print(
            "\n## Frames: no MangoHud log found (with no_display MangoHud writes none)"
        )

    # --- timeline ----------------------------------------------------------
    bucket = "1min" if end - start <= dt.timedelta(hours=1) else "5min"
    print(f"\n## Timeline (per {bucket})")
    lact["min"] = lact.t.dt.floor(bucket)
    nv["min"] = nv.t.dt.floor(bucket)
    tl = lact.groupby("min").agg(
        busy=("busy_pct", "mean"),
        clk=("core_mhz", "mean"),
        mv=("mv", "mean"),
        W=("power_w", "mean"),
        gpu=("temp_gpu", "max"),
        hot=("temp_hotspot", "max"),
        cpu=("cpu_tctl", "max"),
        apuW=("apu_power_w", "mean"),
        pcap=("power_cap", "mean"),
        therm=("thermal", "sum"),
    )
    tl["limitW"] = nv.groupby("min")["enforced.power.limit"].mean()
    tl["pcap"] = (tl.pcap * 100).round(0)
    tl["therm"] = tl.therm / 2  # seconds
    if mh is not None:
        mh["min"] = mh.t.dt.floor(bucket)
        g = mh.groupby("min").frametime
        tl["fps"] = 1000 / g.mean()
        tl["1%low"] = 1000 / g.quantile(0.99)
    tl.index = tl.index.strftime("%H:%M")
    print(tl.round(0).to_string())


if __name__ == "__main__":
    main()
