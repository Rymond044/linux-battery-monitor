"""Background sampler.

One process, no threads, no D-Bus. It blocks in select() on two fds:
  * a CLOCK_BOOTTIME timerfd: periodic samples; it also fires right after resume,
    which is how sleeps are noticed (BOOTTIME - MONOTONIC grows by the time suspended);
  * a kernel uevent socket filtered to power_supply: plug/unplug is recorded immediately.
Rows are buffered and written in one transaction every few minutes or on events.
"""

import os
import select
import signal
import socket
import sys
import time

from . import store, sysfs

NETLINK_KOBJECT_UEVENT = 15
SLEEP_MIN = 5          # a BOOTTIME/MONOTONIC jump larger than this is a suspend
SETTLE = 10            # wait after resume before reading the battery again
FLUSH_EVERY = 300      # seconds between writes when nothing happens


class Collector:
    def __init__(self, battery_interval=30, ac_interval=300, db=None):
        self.intervals = (battery_interval, ac_interval)
        self.con = store.connect(db)
        self.rows, self.apps, self.events, self.sleeps = [], {}, [], []
        self.last = None            # previous sample dict
        self.cpu = sysfs.cpu_times()
        self.cg = sysfs.cgroup_usage()
        self.offset = sysfs.sleep_offset()
        self.hw = sysfs.hw_sleep_total()
        self.pending = None         # sleep waiting for the battery to settle
        self.ac = sysfs.on_ac()
        self.flushed = time.monotonic()
        self.health_day = None
        self.pruned = 0
        self.stop = False
        self.discharged = float(store.meta_get(self.con, "discharged_wh", 0))

    # --- io ---------------------------------------------------------------

    def _uevent_socket(self):
        try:
            s = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC, NETLINK_KOBJECT_UEVENT)
            s.bind((0, 1))
            s.setblocking(False)
            return s
        except OSError as e:
            print(f"uevents unavailable ({e}); plug events will be seen on the next sample", file=sys.stderr)
            return None

    def _arm(self, seconds):
        os.timerfd_settime(self.tfd, initial=max(0.1, seconds))

    def _interval(self):
        return self.intervals[1 if self.ac else 0]

    # --- sampling ---------------------------------------------------------

    def sample(self, now=None):
        now = now or time.time()
        b = sysfs.battery()
        if b is None:
            return None
        busy, total = sysfs.cpu_times()
        dbusy, dtotal = busy - self.cpu[0], total - self.cpu[1]
        self.cpu = (busy, total)
        cg = sysfs.cgroup_usage()
        ac = sysfs.on_ac()
        bucket = int(now // store.BUCKET * store.BUCKET)
        for key, usec in cg.items():
            prev = self.cg.get(key)
            d = usec - prev if prev is not None else 0
            if d > 0:
                k = (bucket, sysfs.unit_key(*key), int(ac))
                self.apps[k] = self.apps.get(k, 0) + d
        self.cg = cg
        if dbusy > 0:
            k = (bucket, "", int(ac))
            self.apps[k] = self.apps.get(k, 0) + dbusy
        screen, external = sysfs.displays()
        power = b["power"]
        if power is not None and b["status"] == "Discharging":
            power = -power
        s = {
            "ts": now, "ac": int(ac), "status": b["status"], "percent": round(b["percent"], 3),
            "energy": round(b["energy"], 4), "full": round(b["full"], 4),
            "power": round(power, 3) if power is not None else None,
            "brightness": sysfs.brightness(), "screen": None if screen is None else int(screen),
            "external": external, "profile": sysfs.profile(),
            "cpu": round(100 * dbusy / dtotal, 1) if dtotal > 0 else None,
            "temp": sysfs.temperature(),
        }
        if self.last is not None:
            if s["ac"] != self.last["ac"]:
                self.events.append((now, "ac", str(s["ac"])))
            drop = self.last["energy"] - s["energy"]
            if 0 < drop < 50:
                self.discharged += drop
        self.rows.append(s)
        self.last = s
        self.ac = ac
        self._health(b, now)
        return s

    def _health(self, b, now):
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if day != self.health_day:
            self.health_day = day
            self.con.execute("INSERT OR REPLACE INTO health VALUES(?, ?, ?, ?)",
                             (day, round(b["full"], 3), round(b["design"], 3), b["cycles"]))

    def tick(self):
        now = time.time()
        off = sysfs.sleep_offset()
        slept = off - self.offset
        self.offset = off
        if slept > SLEEP_MIN:
            if self.pending:
                self._finish_sleep(now)
            self.pending = {"start": now - slept, "end": now, "before": self.last, "hw": self.hw}
            self._arm(SETTLE)
            return
        if self.pending:
            s = self.sample(now)
            self._finish_sleep(now, s)
            self.flush()
        else:
            self.sample(now)
            self.hw = sysfs.hw_sleep_total()
            if time.monotonic() - self.flushed >= FLUSH_EVERY - 1:
                self.flush()
        self._arm(self._interval())

    def _finish_sleep(self, now, after=None):
        p, self.pending = self.pending, None
        before = p["before"] or {}
        after = after or self.last or {}
        hw_now = sysfs.hw_sleep_total()
        hw = (hw_now - p["hw"]) / 1e6 if hw_now is not None and p["hw"] is not None else None
        self.hw = hw_now
        self.sleeps.append((
            round(p["start"], 1), round(p["end"], 1), before.get("ac"), after.get("ac"),
            before.get("percent"), after.get("percent"), before.get("energy"), after.get("energy"),
            hw, sysfs.wakeup_irq()))

    def on_uevent(self):
        changed = False
        while True:
            try:
                msg = self.nl.recv(8192)
            except BlockingIOError:
                break
            except OSError:
                break
            if b"SUBSYSTEM=power_supply" in msg:
                changed = True
        if not changed or self.pending:
            return
        if sysfs.on_ac() != self.ac:
            self.sample()
            self.flush()
            self._arm(self._interval())

    # --- storage ----------------------------------------------------------

    def flush(self):
        con = self.con
        cols = ("ts", "ac", "status", "percent", "energy", "full", "power",
                "brightness", "screen", "external", "profile", "cpu", "temp")
        with con:
            con.execute("BEGIN")
            con.executemany(f"INSERT OR REPLACE INTO samples VALUES({','.join('?' * len(cols))})",
                            [tuple(r[c] for c in cols) for r in self.rows])
            con.executemany("INSERT OR REPLACE INTO sleeps VALUES(?,?,?,?,?,?,?,?,?,?)", self.sleeps)
            con.executemany("INSERT INTO events VALUES(?,?,?)", self.events)
            con.executemany(
                "INSERT INTO apps VALUES(?,?,?,?) ON CONFLICT(bucket, unit, ac) DO UPDATE SET cpu = cpu + excluded.cpu",
                [(*k, v) for k, v in self.apps.items()])
            store.meta_set(con, "discharged_wh", round(self.discharged, 4))
        self.rows, self.apps, self.events, self.sleeps = [], {}, [], []
        self.flushed = time.monotonic()
        if time.time() - self.pruned > 86400:
            store.prune(con)
            self.pruned = time.time()

    # --- main loop --------------------------------------------------------

    def run(self):
        if sysfs.battery() is None:
            print("no battery found", file=sys.stderr)
            return 1
        self.tfd = os.timerfd_create(time.CLOCK_BOOTTIME, flags=os.TFD_CLOEXEC)
        self.nl = self._uevent_socket()
        rfd, wfd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        signal.set_wakeup_fd(wfd)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda *_: setattr(self, "stop", True))
        # SIGUSR1 from the window: write buffered rows now so the newest minutes show up
        self.flush_req = False
        signal.signal(signal.SIGUSR1, lambda *_: setattr(self, "flush_req", True))
        os.makedirs(store.state_dir(), exist_ok=True)
        pidfile = os.path.join(store.state_dir(), "collector.pid")
        with open(pidfile, "w") as f:
            f.write(str(os.getpid()))
        self.events.append((time.time(), "start", sysfs.boot_id()))
        store.meta_set(self.con, "boot:" + sysfs.boot_id(), sysfs.boot_time())
        try:
            from . import importer
            importer.run(self.con)
        except Exception as e:  # backfill is best effort
            print(f"import skipped: {e}", file=sys.stderr)
        self.sample()
        self.flush()
        self._arm(self._interval())
        fds = [self.tfd, rfd] + ([self.nl] if self.nl else [])
        while not self.stop:
            try:
                ready, _, _ = select.select(fds, [], [])
            except InterruptedError:
                continue
            if rfd in ready:
                try:
                    os.read(rfd, 64)
                except BlockingIOError:
                    pass
            if self.stop:
                break
            if self.tfd in ready:
                try:
                    os.read(self.tfd, 8)
                except BlockingIOError:
                    pass
                self.tick()
            if self.nl and self.nl in ready:
                self.on_uevent()
            if self.flush_req:
                self.flush_req = False
                if not self.pending:
                    self.sample()
                    self.flush()
                    self._arm(self._interval())
        if self.pending:
            self._finish_sleep(time.time(), self.sample())
        else:
            self.sample()
        self.events.append((time.time(), "stop", ""))
        self.flush()
        try:
            os.unlink(pidfile)
        except OSError:
            pass
        return 0


def main(args):
    return Collector(args.interval, args.ac_interval).run()
