"""Turns raw rows into sessions, sleeps, per-app usage and estimates."""

import bisect
import time
from dataclasses import dataclass, field

from . import store, sysfs

GAP = 900  # longest interval treated as continuous when integrating energy


@dataclass
class Sleep:
    start: float
    end: float
    ac: bool
    percent_before: float | None
    percent_after: float | None
    energy_before: float | None
    energy_after: float | None
    hw_sleep: float | None
    wake: str | None

    @property
    def duration(self):
        return self.end - self.start

    @property
    def percent_used(self):
        if self.percent_before is None or self.percent_after is None:
            return None
        return self.percent_before - self.percent_after

    @property
    def energy_used(self):
        if self.energy_before is None or self.energy_after is None:
            return None
        return self.energy_before - self.energy_after

    @property
    def drain_per_hour(self):
        p = self.percent_used
        return p / (self.duration / 3600) if p is not None and self.duration > 60 else None

    @property
    def watts(self):
        e = self.energy_used
        return e / (self.duration / 3600) if e is not None and self.duration > 60 else None

    @property
    def hw_fraction(self):
        if self.hw_sleep is None or self.duration <= 0:
            return None
        return max(0.0, min(1.0, self.hw_sleep / self.duration))


@dataclass
class Session:
    """Awake time between two sleeps (or boot/shutdown)."""
    start: float
    end: float
    samples: list = field(default_factory=list)
    ends: str = "now"            # sleep | off | now
    begins: str = "boot"         # sleep | boot
    battery_time: float = 0
    ac_time: float = 0
    energy_used: float = 0       # Wh drawn from the battery while awake on battery
    percent_used: float = 0
    percent_charged: float = 0

    @property
    def duration(self):
        return self.end - self.start

    @property
    def avg_power(self):
        return self.energy_used / (self.battery_time / 3600) if self.battery_time > 120 else None

    @property
    def percent_start(self):
        return self.samples[0]["percent"] if self.samples else None

    @property
    def percent_end(self):
        return self.samples[-1]["percent"] if self.samples else None


def load_samples(con, since, until=None):
    until = until or time.time() + 60
    return [dict(r) for r in con.execute(
        "SELECT * FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts", (since, until))]


def load_sleeps(con, since, until=None):
    until = until or time.time() + 60
    def plugged(r):
        if r["percent_before"] is not None and r["percent_after"] is not None and r["percent_after"] > r["percent_before"] + 0.5:
            return True  # charged while asleep
        return bool(r["ac_before"] if r["ac_before"] is not None else r["ac_after"])

    return [Sleep(r["start"], r["end"], plugged(r),
                  r["percent_before"], r["percent_after"], r["energy_before"], r["energy_after"],
                  r["hw_sleep"], r["wake"])
            for r in con.execute("SELECT * FROM sleeps WHERE end >= ? AND start <= ? ORDER BY start",
                                 (since, until))]


def load_boundaries(con, since, until=None):
    """Shutdown/boot times. A collector restart within the same boot is not a boundary."""
    until = until or time.time() + 60
    rows = con.execute("SELECT ts, kind, value FROM events WHERE kind IN ('start', 'stop') AND ts >= ? AND ts <= ? "
                       "ORDER BY ts", (since - 7 * 86400, until)).fetchall()
    out, boot = [], None
    for i, (ts, kind, value) in enumerate(rows):
        if kind == "start":
            bid = (value or "").replace("-", "")
            if boot is not None and bid != boot:
                out.append(ts)
            boot = bid
        else:
            nxt = next((r for r in rows[i + 1:] if r[1] == "start"), None)
            if nxt is None or (nxt[2] or "").replace("-", "") != boot:
                out.append(ts)
    return [t for t in out if t >= since]


def sessions(samples, sleeps, boundaries=(), now=None):
    now = now or time.time()
    out = []
    bounds = list(boundaries)
    edges = sorted(sleeps, key=lambda s: s.start)
    cur = None
    si = 0
    prev = None
    for s in samples:
        ts = s["ts"]
        slept = None
        while si < len(edges) and edges[si].end <= ts + 1:
            if prev is not None and edges[si].start >= prev["ts"] - 1:
                slept = edges[si]
            si += 1
        off = False
        if cur is not None and not slept and ts - prev["ts"] > 120:
            i = bisect.bisect_left(bounds, prev["ts"] - 1)
            off = i < len(bounds) and bounds[i] <= ts + 1
        if cur is not None and (slept or off):
            cur.end = prev["ts"] if not slept else slept.start
            cur.ends = "sleep" if slept else "off"
            out.append(cur)
            cur = None
        if cur is None:
            cur = Session(start=slept.end if slept else ts, end=ts, begins="sleep" if slept else "boot")
        elif prev is not None:
            dt = ts - prev["ts"]
            if off:
                pass
            elif prev["ac"] == 0 and s["ac"] == 0:
                cur.battery_time += dt
                cur.energy_used += battery_energy([prev, s])[0]
                cur.percent_used += max(0.0, prev["percent"] - s["percent"])
            else:
                cur.ac_time += dt
                cur.percent_charged += max(0.0, s["percent"] - prev["percent"])
        cur.samples.append(s)
        cur.end = ts
        prev = s
    if cur is not None:
        if now - cur.end > 3600:
            cur.ends = "off"
        out.append(cur)
    return out


def percentile(values, q):
    v = sorted(values)
    if not v:
        return None
    i = (len(v) - 1) * q
    lo = int(i)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def battery_energy(samples):
    """(Wh drawn while awake on battery, seconds on battery, 10th percentile watts).
    Intervals longer than GAP are skipped: the machine was asleep or off."""
    e = t = 0.0
    watts = []
    for a, b in zip(samples, samples[1:]):
        dt = b["ts"] - a["ts"]
        if a["ac"] == 0 and b["ac"] == 0 and dt <= GAP:
            if a["power"] is not None and b["power"] is not None:
                e += (abs(a["power"]) + abs(b["power"])) / 2 * dt / 3600
                watts.append(abs(b["power"]))
            elif a["energy"] is not None and b["energy"] is not None:
                e += max(0.0, a["energy"] - b["energy"])  # imported history has no power readings
            t += dt
    return e, t, percentile(watts, 0.1)


@dataclass
class AppUse:
    key: str
    cpu: float       # CPU seconds
    share: float     # share of all CPU time in the period
    energy: float | None  # estimated Wh above the idle floor


def app_usage(con, since, until=None, battery_only=True, samples=None):
    until = until or time.time() + 60
    q = "SELECT unit, SUM(cpu) AS cpu FROM apps WHERE bucket >= ? AND bucket <= ?"
    args = [since - store.BUCKET, until]
    if battery_only:
        q += " AND ac = 0"
    rows = {r["unit"]: r["cpu"] / 1e6 for r in con.execute(q + " GROUP BY unit", args)}
    total = rows.pop("", 0.0)
    known = sum(rows.values())
    total = max(total, known)
    if total - known > 0.5:
        rows["kernel"] = total - known
    active = None
    if battery_only:
        samples = samples if samples is not None else load_samples(con, since, until)
        e, t, floor = battery_energy(samples)
        if t > 0 and floor is not None:
            active = max(0.0, e - floor * t / 3600)
    out = [AppUse(k, v, v / total if total else 0, active * v / total if active is not None and total else None)
           for k, v in rows.items() if v > 0.05]
    out.sort(key=lambda a: a.cpu, reverse=True)
    return out, total


def estimate(con=None, b=None, samples=None):
    """Live status: battery dict + time-to-empty/full in seconds."""
    b = b or sysfs.battery()
    if b is None:
        return None
    ac = sysfs.on_ac()
    left = None
    rate = b["power"]
    if not ac and b["status"] == "Discharging":
        if con is not None:
            recent = samples if samples is not None else load_samples(con, time.time() - 900)
            e, t, _ = battery_energy([s for s in recent if s["ts"] > time.time() - 900])
            if t > 300:
                rate = e / (t / 3600)
        if rate:
            left = b["energy"] / rate * 3600
    elif b["status"] == "Charging" and rate:
        target = b["full"] * (b["end"] or 100) / 100
        left = max(0.0, target - b["energy"]) / rate * 3600
    return {**b, "ac": ac, "rate": rate, "left": left}


def health(con):
    rows = [dict(r) for r in con.execute("SELECT * FROM health ORDER BY day")]
    discharged = float(store.meta_get(con, "discharged_wh", 0))
    return rows, discharged


def benches(con):
    return [dict(r) for r in con.execute("SELECT * FROM benches ORDER BY start DESC")]


# --- formatting helpers shared by the CLI and the UI ---------------------

def fmt_duration(sec, short=False):
    if sec is None:
        return "—"
    sec = int(round(sec))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} d {h} h"
    if h:
        return f"{h} h {m:02d} min" if not short else f"{h}:{m:02d}"
    if m:
        return f"{m} min"
    return f"{sec} s"


def fmt_watts(w):
    return "—" if w is None else f"{abs(w):.1f} W"
