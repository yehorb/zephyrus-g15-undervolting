#!/usr/bin/env python3
"""Log GPU/CPU load data during a gaming session.

Stops with Ctrl+C, or by itself once the GPU has been idle for --idle-stop minutes
after the game ran (i.e. you closed the game and walked away).

Writes data/logs/<start>_<lact-profile>[_<label>]/ (git-ignored):
  meta.json   profile settings, driver, kernel, platform profile at start
  lact.csv    LACT stats + laptop sensors, every --interval seconds
  nvsmi.csv   nvidia-smi: power limit, utilization, clock event (throttle) reasons
  xid.log     NVIDIA kernel messages (Xid = GPU fault) seen during the session

Usage: uv run tools/gamelog.py [label] [--interval 0.5] [--idle-stop 5]
"""

import argparse
import contextlib
import csv
import datetime as dt
import json
import platform
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lact_api import call

NV = "10DE:249C-1043:1622-0000:01:00.0"
ROOT = Path(__file__).resolve().parent.parent / "data" / "logs"
IDLE_BELOW = 30  # busy %; a running game (even in menus) stays above this
NVSMI_FIELDS = (
    "timestamp,pstate,utilization.gpu,utilization.memory,power.draw.average,"
    "power.draw.instant,enforced.power.limit,clocks.current.graphics,clocks.current.memory,"
    "clocks_event_reasons.active,clocks_event_reasons.sw_power_cap,"
    "clocks_event_reasons.sw_thermal_slowdown,clocks_event_reasons.hw_slowdown,"
    "clocks_event_reasons.hw_thermal_slowdown,clocks_event_reasons.hw_power_brake_slowdown"
)


def read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def hwmon(name):
    for d in Path("/sys/class/hwmon").iterdir():
        if read(d / "name") == name:
            return d
    return None


K10, AMDGPU, ASUS = hwmon("k10temp"), hwmon("amdgpu"), hwmon("asus")


def current_profile():
    return call("list_profiles", include_state=False)["current_profile"] or "Default"


def sample():
    s = call("device_stats", id=NV)
    clk, temps = s["clockspeed"], s["temps"]
    sens = clk.get("sensors", {})
    t = lambda k: (temps.get(k) or {}).get("current")
    return {
        "timestamp": int(time.time() * 1000),
        "profile": current_profile(),
        "core_mhz": clk.get("gpu_clockspeed"),
        "vram_mhz": clk.get("vram_clockspeed"),
        "sm_mhz": sens.get("SM"),
        "video_mhz": sens.get("Video"),
        "xbar_mhz": sens.get("XBAR"),
        "sys_mhz": sens.get("SYS"),
        "hub_mhz": sens.get("HUB"),
        "mv": s["voltage"].get("gpu"),
        "power_w": s["power"].get("current"),
        "temp_gpu": t("GPU"),
        "temp_hotspot": t("GPU Hotspot"),
        "temp_vram": t("VRAM"),
        "busy_pct": s.get("busy_percent"),
        "vram_used_mb": round((s["vram"].get("used") or 0) / 2**20),
        "pstate_core": (s.get("active_power_states", {}).get("core") or {}).get(
            "Index"
        ),
        "pstate_mem": (s.get("active_power_states", {}).get("memory") or {}).get(
            "Index"
        ),
        "throttle": json.dumps(s.get("throttle_info") or {}, separators=(",", ":")),
        "cpu_tctl": int(read(K10 / "temp1_input", 0)) / 1000 if K10 else None,
        "apu_power_w": int(read(AMDGPU / "power1_input", 0)) / 1e6 if AMDGPU else None,
        "fan1_rpm": read(ASUS / "fan1_input") if ASUS else None,
        "fan2_rpm": read(ASUS / "fan2_input") if ASUS else None,
        "platform_profile": read("/sys/firmware/acpi/platform_profile"),
        "ac_online": read("/sys/class/power_supply/AC0/online"),
    }


def stop_children(children):
    for c in children:
        c.terminate()
    for c in children:
        try:
            c.wait(timeout=5)
        except subprocess.TimeoutExpired:
            c.kill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("label", nargs="?", default="")
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument(
        "--idle-stop",
        type=float,
        default=5,
        metavar="MIN",
        help="stop after this many idle minutes once the GPU was busy (0: never)",
    )
    a = ap.parse_args()

    prof = current_profile()
    slug = re.sub(r"[^A-Za-z0-9+.-]+", "-", prof).strip("-")
    out = ROOT / "_".join(
        filter(
            None,
            [dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S"), slug, a.label],
        )
    )
    out.mkdir(parents=True)

    driver = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=driver_version,vbios_version",
            "--format=csv,noheader",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    (out / "meta.json").write_text(
        json.dumps(
            {
                "start": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "label": a.label,
                "interval_s": a.interval,
                "lact_profile": prof,
                "lact_profile_settings": call(
                    "get_profile", name=None if prof == "Default" else prof
                ),
                "nvidia_driver_vbios": driver,
                "kernel": platform.release(),
                "platform_profile": read("/sys/firmware/acpi/platform_profile"),
                "ac_online": read("/sys/class/power_supply/AC0/online"),
            },
            indent=1,
        )
    )

    with contextlib.ExitStack() as stack:
        nvsmi_out = stack.enter_context((out / "nvsmi.csv").open("w"))
        xid_out = stack.enter_context((out / "xid.log").open("w"))
        f = stack.enter_context((out / "lact.csv").open("w", newline=""))
        children = [
            subprocess.Popen(
                [
                    "stdbuf",
                    "-oL",
                    "nvidia-smi",
                    f"--query-gpu={NVSMI_FIELDS}",
                    "--format=csv,nounits",
                    "-lms",
                    "1000",
                ],
                stdout=nvsmi_out,
                stderr=subprocess.DEVNULL,
            ),
            subprocess.Popen(
                [
                    "stdbuf",
                    "-oL",
                    "journalctl",
                    "-k",
                    "-f",
                    "-n",
                    "0",
                    "-o",
                    "short-iso",
                    "--grep",
                    "NVRM|Xid",
                ],
                stdout=xid_out,
                stderr=subprocess.DEVNULL,
            ),
        ]
        # registered last, so it runs first: stop writers before their files close
        stack.callback(stop_children, children)

        stop = False

        def on_signal(*_):
            nonlocal stop
            stop = True

        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGTERM, on_signal)

        print(
            f"Logging to {out}\nProfile: {prof} | platform: {read('/sys/firmware/acpi/platform_profile')} | "
            f"AC: {read('/sys/class/power_supply/AC0/online')}\nCtrl+C to stop."
            + (
                f" Stops by itself after {a.idle_stop:g} idle min once the game ran."
                if a.idle_stop
                else ""
            )
        )
        n, t0, writer = 0, time.time(), None
        last_busy, was_busy = time.monotonic(), False
        while not stop:
            tick = time.monotonic()
            try:
                row = sample()
            # daemon restart, GPU reload, odd payload -- keep going
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as e:
                print(f"sample failed: {e}", file=sys.stderr)
                time.sleep(a.interval)
                continue
            if writer is None:
                writer = csv.DictWriter(f, fieldnames=list(row))
                writer.writeheader()
            writer.writerow(row)
            n += 1
            if (row["busy_pct"] or 0) >= IDLE_BELOW:
                last_busy, was_busy = time.monotonic(), True
            elif (
                a.idle_stop
                and was_busy
                and time.monotonic() - last_busy > a.idle_stop * 60
            ):
                print(f"\nGPU idle for {a.idle_stop:g} min, stopping.")
                break
            if n % 20 == 0:
                f.flush()
                print(
                    f"\r{(time.time() - t0) / 60:5.1f} min | {row['core_mhz']} MHz {row['mv']} mV "
                    f"{row['power_w']} W | GPU {row['temp_gpu']}/{row['temp_hotspot']} °C | "
                    f"busy {row['busy_pct']}% | {row['profile']}   ",
                    end="",
                    flush=True,
                )
            time.sleep(max(0, a.interval - (time.monotonic() - tick)))

    xids = [l for l in read(out / "xid.log").splitlines() if "Xid" in l]
    print(
        f"\nStopped after {(time.time() - t0) / 60:.1f} min, {n} samples. Xid errors: {len(xids)}"
    )
    for l in xids:
        print("  ", l)
    print(f"Saved in {out}")


if __name__ == "__main__":
    main()
