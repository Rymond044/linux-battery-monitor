"""One-time backfill from history other tools already keep:
UPower's charge log (/var/lib/upower/history-charge-*.dat) and suspend/resume lines in the journal."""

import bisect
import glob
import json
import os
import subprocess

from . import store, sysfs

STATES = {
    "discharging": ("Discharging", 0),
    "charging": ("Charging", 1),
    "fully-charged": ("Full", 1),
    "pending-charge": ("Not charging", 1),
    "pending-discharge": ("Discharging", 0),
}


def _upower_file(b):
    files = glob.glob("/var/lib/upower/history-charge-*.dat")
    model = (b.get("model") or "").replace(" ", "_")
    named = [f for f in files if model and os.path.basename(f).startswith(f"history-charge-{model}-")]
    return max(named or [], key=os.path.getmtime, default=None)


def _journal(args):
    try:
        return subprocess.run(["journalctl", "-q", "--no-pager", *args], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def run(con, force=False):
    if store.meta_get(con, "imported") and not force:
        return 0
    b = sysfs.battery()
    if b is None:
        return 0
    first = con.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
    cutoff = first if first is not None else float("inf")
    points = []
    path = _upower_file(b)
    if path:
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) != 3 or parts[2] not in STATES:
                    continue
                ts, pct = float(parts[0]), float(parts[1])
                if ts < cutoff and pct > 0:
                    points.append((ts, pct, *STATES[parts[2]]))
    points.sort()
    full = b["full"]
    rows = [(ts, ac, status, pct, round(pct / 100 * full, 4), round(full, 4)) for ts, pct, status, ac in points]

    events = []
    try:
        boots = json.loads(_journal(["--list-boots", "-o", "json"]) or "[]")
    except json.JSONDecodeError:
        boots = []
    lo = points[0][0] if points else cutoff
    current = sysfs.boot_id().replace("-", "")
    for bt in boots:
        start, end = bt.get("first_entry", 0) / 1e6, bt.get("last_entry", 0) / 1e6
        if end < lo or start >= cutoff:
            continue
        events.append((start, "start", bt.get("boot_id", "")))
        if end < cutoff and bt.get("boot_id", "").replace("-", "") != current:
            events.append((end, "stop", ""))

    sleeps = []
    if points:
        times = [p[0] for p in points]
        out = _journal(["-k", "-o", "short-unix", "--grep", r"PM: suspend (entry|exit)",
                        "--since", f"@{int(lo)}", "--until", f"@{int(min(cutoff, 4e9))}"])
        entry = None
        for line in out.splitlines():
            try:
                ts = float(line.split()[0])
            except (ValueError, IndexError):
                continue
            if "suspend entry" in line:
                entry = ts
            elif "suspend exit" in line and entry is not None:
                i = bisect.bisect_right(times, entry) - 1
                j = bisect.bisect_left(times, ts)
                before = points[i] if i >= 0 else None
                after = points[j] if j < len(points) and points[j][0] - ts < 900 else None
                sleeps.append((round(entry, 1), round(ts, 1),
                               before[3] if before else None, after[3] if after else None,
                               before[1] if before else None, after[1] if after else None,
                               round(before[1] / 100 * full, 4) if before else None,
                               round(after[1] / 100 * full, 4) if after else None,
                               None, None))
                entry = None

    with con:
        con.execute("BEGIN")
        con.executemany("INSERT OR IGNORE INTO samples(ts, ac, status, percent, energy, full) VALUES(?,?,?,?,?,?)", rows)
        con.executemany("INSERT OR IGNORE INTO sleeps VALUES(?,?,?,?,?,?,?,?,?,?)", sleeps)
        con.executemany("INSERT INTO events VALUES(?,?,?)", events)
        store.meta_set(con, "imported", "1")
    return len(rows)
