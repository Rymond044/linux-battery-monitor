"""Controlled power measurement: sample battery draw every few seconds for N minutes.

Results are only comparable when conditions match, so brightness, profile, outputs and
CPU load are recorded with each run, and a run is marked invalid if AC was plugged in
or brightness/outputs changed midway.
"""

import statistics
import time

from . import store, sysfs


def run(minutes=5.0, label="", warmup=30, step=5, out=print, stop=lambda: False, progress=None):
    if sysfs.on_ac():
        raise RuntimeError("Unplug the charger first: draw can only be measured on battery.")
    con = store.connect()
    start_cond = _conditions()
    t_end = time.monotonic() + warmup
    while time.monotonic() < t_end and not stop():
        if progress:
            progress("warmup", 1 - (t_end - time.monotonic()) / max(warmup, 1), None)
        time.sleep(1)
    watts, notes = [], set()
    busy0 = sysfs.cpu_times()
    started = time.time()
    t_end = time.monotonic() + minutes * 60
    while time.monotonic() < t_end and not stop():
        b = sysfs.battery()
        if sysfs.on_ac():
            notes.add("charger plugged in")
            break
        if b and b["power"] is not None:
            watts.append(b["power"])
        cond = _conditions()
        if cond["brightness"] != start_cond["brightness"]:
            notes.add("brightness changed")
        if (cond["screen"], cond["external"]) != (start_cond["screen"], start_cond["external"]):
            notes.add("displays changed")
        if cond["profile"] != start_cond["profile"]:
            notes.add("profile changed")
        if progress:
            progress("measure", 1 - (t_end - time.monotonic()) / (minutes * 60), statistics.fmean(watts) if watts else None)
        time.sleep(step)
    if stop():
        notes.add("stopped early")
    busy1 = sysfs.cpu_times()
    ended = time.time()
    if len(watts) < 3:
        raise RuntimeError("Not enough readings.")
    cpu = 100 * (busy1[0] - busy0[0]) / max(1, busy1[1] - busy0[1])
    notes.add(f"cpu {cpu:.1f}%")
    row = {
        "start": started, "end": ended, "label": label,
        "avg": statistics.fmean(watts), "median": statistics.median(watts),
        "min": min(watts), "max": max(watts), "n": len(watts),
        **start_cond,
        "valid": int(not ({"charger plugged in", "brightness changed", "displays changed", "profile changed"} & notes)),
        "note": ", ".join(sorted(notes)),
    }
    with con:
        con.execute("BEGIN")
        con.execute("INSERT INTO benches(start, end, label, avg, median, min, max, n, brightness, profile, screen, external, valid, note)"
                    " VALUES(:start, :end, :label, :avg, :median, :min, :max, :n, :brightness, :profile, :screen, :external, :valid, :note)", row)
    out(f"{label or 'run'}: {row['avg']:.2f} W avg, {row['median']:.2f} W median "
        f"({row['min']:.2f}–{row['max']:.2f}, {row['n']} readings) · {row['note']}"
        + ("" if row["valid"] else " · INVALID"))
    return row


def _conditions():
    screen, external = sysfs.displays()
    return {"brightness": sysfs.brightness(), "profile": sysfs.profile(),
            "screen": None if screen is None else int(screen), "external": external}
