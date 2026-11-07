import argparse
import json
import os
import subprocess
import sys
import time

from . import __version__, analysis, store
from .analysis import fmt_duration, fmt_watts


def cmd_status(a):
    con = store.connect(readonly=True)
    e = analysis.estimate(con)
    if e is None:
        print("no battery", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps({k: e[k] for k in ("percent", "status", "ac", "rate", "left", "energy", "full", "design")}))
        return 0
    state = "on AC" if e["ac"] else "on battery"
    line = f"{e['percent']:.0f}% · {e['status'].lower()} · {state} · {fmt_watts(e['power'])}"
    if e["left"]:
        line += f" · {'empty' if not e['ac'] else 'full'} in {fmt_duration(e['left'])}"
    print(line)
    return 0


def _ts(t, fmt="%a %d %b %H:%M"):
    return time.strftime(fmt, time.localtime(t))


def cmd_sessions(a):
    con = store.connect(readonly=True)
    since = time.time() - a.days * 86400
    sl = analysis.load_sleeps(con, since)
    for s in analysis.sessions(analysis.load_samples(con, since), sl, analysis.load_boundaries(con, since)):
        avg = fmt_watts(s.avg_power) if s.avg_power else "—"
        print(f"{_ts(s.start)} → {_ts(s.end, '%H:%M')}  {fmt_duration(s.duration):>12}  "
              f"battery {fmt_duration(s.battery_time):>12}  −{s.percent_used:4.1f}%  {s.energy_used:5.2f} Wh  avg {avg:>7}  "
              f"[{s.begins} → {s.ends}]")
    return 0


def cmd_sleeps(a):
    con = store.connect(readonly=True)
    for s in analysis.load_sleeps(con, time.time() - a.days * 86400):
        d = s.drain_per_hour
        hw = s.hw_fraction
        print(f"{_ts(s.start)} → {_ts(s.end)}  {fmt_duration(s.duration):>12}  "
              f"{'AC' if s.ac else 'battery':7}  "
              f"{'−%.2f%%' % s.percent_used if s.percent_used is not None else '—':>8}  "
              f"{'%.2f %%/h' % d if d is not None else '—':>9}  "
              f"{'%.2f W' % s.watts if s.watts is not None else '—':>7}  "
              f"deep {'%.0f%%' % (hw * 100) if hw is not None else '—':>4}  {s.wake or ''}")
    return 0


def cmd_apps(a):
    con = store.connect(readonly=True)
    rows, total = analysis.app_usage(con, time.time() - a.hours * 3600, battery_only=not a.all)
    print(f"total CPU {total:.0f} s")
    for r in rows[: a.limit]:
        e = f"{r.energy:.2f} Wh" if r.energy is not None else ""
        print(f"{r.share * 100:5.1f}%  {r.cpu:8.1f} s  {e:>9}  {r.key}")
    return 0


def cmd_bench(a):
    from . import bench
    try:
        bench.run(a.minutes, a.label, warmup=a.warmup)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def cmd_benches(a):
    con = store.connect(readonly=True)
    for b in analysis.benches(con)[: a.limit]:
        print(f"{_ts(b['start'])}  {b['avg']:6.2f} W  med {b['median']:6.2f}  "
              f"{fmt_duration(b['end'] - b['start']):>8}  bright {b['brightness']}%  {b['profile']}  "
              f"ext {b['external']}  {'ok ' if b['valid'] else 'BAD'}  {b['label']}  ({b['note']})")
    return 0


def cmd_powertop(a):
    out = os.path.join(store.state_dir(), "reports")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, time.strftime("powertop-%Y%m%d-%H%M%S.html"))
    r = subprocess.run(["pkexec", "powertop", f"--html={path}", f"--time={a.seconds}"])
    if r.returncode == 0 and os.path.exists(path):
        print(path)
        if not a.no_open:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode


def cmd_import(a):
    from . import importer
    print(f"imported {importer.run(store.connect(), force=a.force)} samples")
    return 0


def cmd_app(a):
    from .ui.app import main
    return main(a.page)


def main(argv=None):
    p = argparse.ArgumentParser(prog="battery-monitor", description="Battery usage history for Linux laptops.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("app", help="open the window (default)")
    s.add_argument("page", nargs="?", default=None)
    s.set_defaults(func=cmd_app)

    s = sub.add_parser("collect", help="run the background sampler")
    s.add_argument("--interval", type=float, default=30, help="seconds between samples on battery")
    s.add_argument("--ac-interval", type=float, default=300, help="seconds between samples on AC")
    s.set_defaults(func=lambda a: __import__("battery_monitor.collector", fromlist=["main"]).main(a))

    s = sub.add_parser("import", help="backfill from UPower history and the journal (done once automatically)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("status", help="current charge, draw and estimate")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("sessions", help="awake periods between sleeps")
    s.add_argument("--days", type=float, default=2)
    s.set_defaults(func=cmd_sessions)

    s = sub.add_parser("sleeps", help="suspend periods with drain and deep-sleep share")
    s.add_argument("--days", type=float, default=7)
    s.set_defaults(func=cmd_sleeps)

    s = sub.add_parser("apps", help="CPU time per app")
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--all", action="store_true", help="include time on AC")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_apps)

    s = sub.add_parser("bench", help="measure average draw over a few minutes (on battery)")
    s.add_argument("--minutes", type=float, default=5)
    s.add_argument("--warmup", type=float, default=30)
    s.add_argument("--label", default="")
    s.set_defaults(func=cmd_bench)

    s = sub.add_parser("benches", help="list measurements")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_benches)

    s = sub.add_parser("powertop", help="generate a powertop HTML report (asks for your password)")
    s.add_argument("--seconds", type=int, default=30)
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(func=cmd_powertop)

    a = p.parse_args(argv)
    if not a.cmd:
        a = p.parse_args(["app", *(argv or sys.argv[1:])])
    return a.func(a) or 0
