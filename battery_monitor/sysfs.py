"""Readers for the kernel interfaces the collector samples. Everything here works without root."""

import os
import time

PSU = "/sys/class/power_supply"
CGROUP = "/sys/fs/cgroup"
CLK_TCK = os.sysconf("SC_CLK_TCK")


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, ValueError):
        return default


def read_int(path, default=None):
    v = read(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# --- power supply ---------------------------------------------------------

def _supplies(kind):
    try:
        names = sorted(os.listdir(PSU))
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(PSU, n)
        if read(p + "/type") == kind and read(p + "/scope", "System") != "Device":
            out.append(p)
    return out


def batteries():
    return [p for p in _supplies("Battery") if read(p + "/present", "1") == "1"]


def on_ac():
    mains = _supplies("Mains")
    if mains:
        return any(read(p + "/online") == "1" for p in mains)
    usb = _supplies("USB")
    if usb:
        return any(read(p + "/online") == "1" for p in usb)
    st = [read(p + "/status") for p in batteries()]
    return any(s in ("Charging", "Full", "Not charging") for s in st)


def _battery(p):
    def i(name):
        return read_int(f"{p}/{name}")

    vnom = i("voltage_min_design") or i("voltage_now") or 0
    if i("energy_now") is not None:
        now, full, design = i("energy_now"), i("energy_full"), i("energy_full_design")
        to_wh = 1e-6
    elif i("charge_now") is not None and vnom:
        now, full, design = i("charge_now"), i("charge_full"), i("charge_full_design")
        to_wh = vnom * 1e-12
    else:
        return None
    if i("power_now") is not None:
        power = i("power_now") * 1e-6
    elif i("current_now") is not None and i("voltage_now"):
        power = abs(i("current_now")) * i("voltage_now") * 1e-12
    else:
        power = None
    return {
        "status": read(p + "/status", "Unknown"),
        "capacity": i("capacity"),
        "energy": (now or 0) * to_wh,
        "full": (full or 0) * to_wh,
        "design": (design or 0) * to_wh,
        "power": power,
        "cycles": i("cycle_count"),
        "start": i("charge_control_start_threshold"),
        "end": i("charge_control_end_threshold"),
        "model": read(p + "/model_name"),
        "vendor": read(p + "/manufacturer"),
        "tech": read(p + "/technology"),
        "name": os.path.basename(p),
    }


def battery():
    """All present batteries merged into one. None when the machine has no battery."""
    bats = [b for b in map(_battery, batteries()) if b]
    if not bats:
        return None
    b = dict(bats[0])
    for o in bats[1:]:
        for k in ("energy", "full", "design"):
            b[k] += o[k]
        b["power"] = (b["power"] or 0) + (o["power"] or 0)
    statuses = [o["status"] for o in bats]
    for s in ("Discharging", "Charging", "Not charging", "Full"):
        if s in statuses:
            b["status"] = s
            break
    b["percent"] = 100 * b["energy"] / b["full"] if b["full"] else (b["capacity"] or 0)
    b["percent"] = max(0.0, min(100.0, b["percent"]))
    b["count"] = len(bats)
    return b


# --- display, profile, cpu ------------------------------------------------

def _backlight():
    base = "/sys/class/backlight"
    try:
        names = os.listdir(base)
    except OSError:
        return None
    rank = {"firmware": 0, "platform": 1, "raw": 2}
    names.sort(key=lambda n: rank.get(read(f"{base}/{n}/type"), 3))
    return f"{base}/{names[0]}" if names else None


_BACKLIGHT = _backlight()


def brightness():
    if not _BACKLIGHT:
        return None
    cur, mx = read_int(_BACKLIGHT + "/brightness"), read_int(_BACKLIGHT + "/max_brightness")
    return round(100 * cur / mx, 1) if cur is not None and mx else None


INTERNAL = ("eDP", "LVDS", "DSI")


def displays():
    """(internal panel on, number of external outputs in use)."""
    base = "/sys/class/drm"
    internal, external = False, 0
    try:
        names = os.listdir(base)
    except OSError:
        return None, 0
    for n in names:
        if "-" not in n or not n.startswith("card"):
            continue
        p = f"{base}/{n}"
        if read(p + "/status") != "connected" or read(p + "/enabled") != "enabled":
            continue
        conn = n.split("-", 1)[1]
        if conn.startswith(INTERNAL):
            internal = internal or read(p + "/dpms") == "On"
        else:
            external += 1
    return internal, external


def profile():
    for line in (read("/var/lib/power-profiles-daemon/state.ini") or "").splitlines():
        if line.startswith("Profile="):
            return line.split("=", 1)[1]
    return read("/sys/firmware/acpi/platform_profile")


TEMP_ZONES = ("x86_pkg_temp", "TCPU", "cpu-thermal", "cpu_thermal", "acpitz")


def _temp_zone():
    base = "/sys/class/thermal"
    zones = {}
    try:
        for n in os.listdir(base):
            if n.startswith("thermal_zone"):
                zones.setdefault(read(f"{base}/{n}/type"), f"{base}/{n}/temp")
    except OSError:
        return None
    for t in TEMP_ZONES:
        if t in zones:
            return zones[t]
    return None


_TEMP = _temp_zone()


def temperature():
    v = read_int(_TEMP) if _TEMP else None
    return round(v / 1000, 1) if v is not None else None


def cpu_times():
    """(busy, total) CPU time in microseconds summed over all CPUs."""
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = [int(x) for x in parts[:8]]
    idle = vals[3] + vals[4]
    total = sum(vals)
    k = 1_000_000 // CLK_TCK
    return (total - idle) * k, total * k


# --- sleep ----------------------------------------------------------------

def sleep_offset():
    """Seconds spent suspended since boot (CLOCK_BOOTTIME - CLOCK_MONOTONIC)."""
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)


def hw_sleep_total():
    """Microseconds the platform spent in its deepest sleep state (S0ix) in suspend, if exposed."""
    v = read_int("/sys/power/suspend_stats/total_hw_sleep")
    if v is None:
        v = read_int("/sys/devices/system/cpu/cpuidle/low_power_idle_system_residency_us")
    return v


def wakeup_irq():
    irq = read("/sys/power/pm_wakeup_irq")
    if not irq:
        return None
    try:
        with open("/proc/interrupts") as f:
            for line in f:
                head, _, rest = line.partition(":")
                if head.strip() == irq:
                    name = rest.split()[-1] if rest.split() else ""
                    return f"IRQ {irq} ({name})" if name else f"IRQ {irq}"
    except OSError:
        pass
    return f"IRQ {irq}"


def boot_id():
    return read("/proc/sys/kernel/random/boot_id", "")


def boot_time():
    for line in open("/proc/stat"):
        if line.startswith("btime"):
            return int(line.split()[1])
    return int(time.time() - time.clock_gettime(time.CLOCK_BOOTTIME))


# --- per-app CPU from cgroups ---------------------------------------------

def _unescape(s):
    out, i = [], 0
    while i < len(s):
        if s.startswith("\\x", i) and i + 4 <= len(s):
            try:
                out.append(chr(int(s[i + 2:i + 4], 16)))
                i += 4
                continue
            except ValueError:
                pass
        out.append(s[i])
        i += 1
    return "".join(out)


def _strip_id(name):
    """Drop trailing instance ids systemd/launchers add: -12345, -0cf90dd1, @0, -1097193-0."""
    name = name.split("@", 1)[0]
    parts = name.split("-")
    while len(parts) > 1:
        last = parts[-1]
        hexy = len(last) >= 4 and all(c in "0123456789abcdef" for c in last) and any(c.isdigit() for c in last)
        if last.isdigit() or hexy:
            parts.pop()
        else:
            break
    return "-".join(parts)


def unit_key(kind, unit):
    """Map a cgroup leaf to a stable app key, e.g. app:org.chromium.Chromium, user:pipewire, system:iwd."""
    name = _unescape(unit)
    for suf in (".scope", ".service", ".slice", ".mount", ".socket", ".swap"):
        if name.endswith(suf):
            name = name[: -len(suf)]
            break
    if kind == "system":
        if name.startswith("docker-") and len(name) > 40:
            return "system:docker-containers"
        return "system:" + _strip_id(name)
    if name.startswith("app-"):
        name = name[4:]
        for pre in ("Hyprland-", "graphical-", "niri-", "sway-", "gnome-", "KDE-"):
            if name.startswith(pre):
                name = name[len(pre):]
                break
    if name.startswith("dbus-:"):
        name = name.split("-", 2)[-1]
    name = _strip_id(name)
    return ("app:" if kind == "app" else "user:") + name


def _cpu_usec(path):
    try:
        with open(path + "/cpu.stat") as f:
            line = f.readline()
        return int(line.split()[1]) if line.startswith("usage_usec") else None
    except (OSError, ValueError, IndexError):
        return None


def _leaves(path, kind, out):
    try:
        it = os.scandir(path)
    except OSError:
        return
    with it:
        for e in it:
            if not e.is_dir(follow_symlinks=False):
                continue
            n = e.name
            if n.endswith(".slice"):
                _leaves(e.path, kind, out)
            elif n.endswith((".scope", ".service")):
                # A unit with its own children (e.g. user@.service) is descended into instead.
                if n.startswith("user@"):
                    continue
                u = _cpu_usec(e.path)
                if u is not None:
                    out[(kind, n)] = u


def cgroup_usage():
    """{(kind, unit): usage_usec} for every leaf unit we can read."""
    out = {}
    uid = os.getuid()
    user = f"{CGROUP}/user.slice/user-{uid}.slice/user@{uid}.service"
    for sl, kind in (("app.slice", "app"), ("background.slice", "app"), ("session.slice", "user")):
        _leaves(f"{user}/{sl}", kind, out)
    try:
        for e in os.scandir(user):
            if e.is_dir() and e.name.endswith((".service", ".scope")):
                u = _cpu_usec(e.path)
                if u is not None:
                    out[("user", e.name)] = u
    except OSError:
        pass
    _leaves(f"{CGROUP}/system.slice", "system", out)
    return out
