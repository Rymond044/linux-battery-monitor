"""SQLite storage shared by the collector (writer) and the app/CLI (readers)."""

import os
import sqlite3
import time

RETENTION_DAYS = 90
BUCKET = 300  # seconds per row in the per-app CPU table

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples(
    ts REAL PRIMARY KEY,
    ac INTEGER, status TEXT, percent REAL, energy REAL, full REAL, power REAL,
    brightness REAL, screen INTEGER, external INTEGER, profile TEXT, cpu REAL, temp REAL);
CREATE TABLE IF NOT EXISTS sleeps(
    start REAL PRIMARY KEY, end REAL, ac_before INTEGER, ac_after INTEGER,
    percent_before REAL, percent_after REAL, energy_before REAL, energy_after REAL,
    hw_sleep REAL, wake TEXT);
CREATE TABLE IF NOT EXISTS events(ts REAL, kind TEXT, value TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS apps(
    bucket INTEGER, unit TEXT, ac INTEGER, cpu INTEGER,
    PRIMARY KEY(bucket, unit, ac)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS health(day TEXT PRIMARY KEY, full REAL, design REAL, cycles INTEGER);
CREATE TABLE IF NOT EXISTS benches(
    id INTEGER PRIMARY KEY, start REAL, end REAL, label TEXT,
    avg REAL, median REAL, min REAL, max REAL, n INTEGER,
    brightness REAL, profile TEXT, screen INTEGER, external INTEGER, valid INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "battery-monitor")


def db_path():
    return os.environ.get("BATTERY_MONITOR_DB") or os.path.join(state_dir(), "data.sqlite")


def connect(path=None, readonly=False):
    path = path or db_path()
    if readonly and not os.path.exists(path):
        readonly = False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    if not readonly:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        if not con.execute("SELECT name FROM sqlite_master WHERE name='samples'").fetchone():
            con.execute("PRAGMA auto_vacuum=INCREMENTAL")
        con.executescript(SCHEMA)
    return con


def prune(con, days=RETENTION_DAYS):
    cut = time.time() - days * 86400
    with con:
        con.execute("BEGIN")
        con.execute("DELETE FROM samples WHERE ts < ?", (cut,))
        con.execute("DELETE FROM sleeps WHERE end < ?", (cut,))
        con.execute("DELETE FROM events WHERE ts < ?", (cut,))
        con.execute("DELETE FROM apps WHERE bucket < ?", (cut,))
        con.execute("DELETE FROM benches WHERE end < ?", (cut,))
    con.execute("PRAGMA incremental_vacuum")


def meta_get(con, key, default=None):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def meta_set(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (key, str(value)))


def request_flush():
    """Ask a running collector to write its buffer. Returns True if one was signalled."""
    import signal
    try:
        with open(os.path.join(state_dir(), "collector.pid")) as f:
            pid = int(f.read().strip())
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            if b"battery_monitor" not in f.read():
                return False
        os.kill(pid, signal.SIGUSR1)
        return True
    except (OSError, ValueError):
        return False
