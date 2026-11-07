import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import APP_ID, __version__, analysis, store, sysfs  # noqa: E402
from ..analysis import fmt_duration, fmt_watts  # noqa: E402
from . import names  # noqa: E402
from .charts import BarChart, TimeChart  # noqa: E402

CSS = """
.hero { font-size: 46px; font-weight: 700; }
.tile { padding: 12px 14px; }
.tile .value { font-size: 19px; font-weight: 700; }
.chart-card { padding: 14px 14px 8px 14px; }
.readout { font-feature-settings: "tnum"; }
.legend-swatch { min-width: 10px; min-height: 10px; border-radius: 3px; }
.swatch-sleep { background: alpha(currentColor, 0.14); }
.swatch-charge { background: alpha(var(--accent-color), 0.22); }
"""

RANGES = [("6 h", 6 * 3600), ("24 h", 86400), ("3 days", 3 * 86400), ("7 days", 7 * 86400)]
APP_RANGES = [("24 h", 86400), ("7 days", 7 * 86400), ("30 days", 30 * 86400), ("90 days", 90 * 86400)]


# --- small building blocks ------------------------------------------------

def label(text="", css=(), xalign=0.0, wrap=False, **kw):
    lab = Gtk.Label(label=text, xalign=xalign, wrap=wrap, **kw)
    for c in css:
        lab.add_css_class(c)
    return lab


def page_box():
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                  margin_top=18, margin_bottom=24, margin_start=16, margin_end=16)
    clamp = Adw.Clamp(maximum_size=1000, tightening_threshold=700, child=box)
    scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=clamp)
    return scroll, box


def clear(box):
    while (c := box.get_first_child()) is not None:
        box.remove(c)


class Tiles(Gtk.FlowBox):
    def __init__(self, per_line=4):
        super().__init__(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                         column_spacing=12, row_spacing=12, max_children_per_line=per_line, min_children_per_line=2)

    def set_tiles(self, tiles):
        self.remove_all()
        for caption, value, sub in tiles:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.add_css_class("card")
            box.add_css_class("tile")
            box.append(label(caption, ("caption", "dim-label")))
            box.append(label(value, ("value",), ellipsize=3))
            if sub:
                box.append(label(sub, ("caption", "dim-label"), ellipsize=3))
            child = Gtk.FlowBoxChild(child=box, focusable=False)
            self.append(child)


class ChartCard(Gtk.Box):
    def __init__(self, title, chart, legend=()):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add_css_class("card")
        self.add_css_class("chart-card")
        head = Gtk.Box(spacing=8)
        head.append(label(title, ("heading",), hexpand=True))
        for css, text in legend:
            sw = Gtk.Box(valign=Gtk.Align.CENTER)
            sw.add_css_class("legend-swatch")
            sw.add_css_class(css)
            head.append(sw)
            head.append(label(text, ("caption", "dim-label")))
        self.readout = label("", ("caption", "dim-label", "readout"), xalign=1.0)
        head.append(self.readout)
        self.append(head)
        self.append(chart)
        self.chart = chart
        self.default = ""
        chart.on_hover = self._hover

    def _hover(self, text):
        GLib.idle_add(self._set, text if text is not None else self.default)

    def _set(self, text):
        if self.readout.get_label() != text:
            self.readout.set_label(text)
        return False

    def set_default(self, text):
        self.default = text
        self._set(text)


def toggle_group(options, active, on_change):
    g = Adw.ToggleGroup()
    for name, _ in options:
        g.add(Adw.Toggle(label=name, name=name))
    g.set_active_name(active)
    g.connect("notify::active-name", lambda w, _p: on_change(w.get_active_name()))
    g.set_halign(Gtk.Align.START)
    return g


def battery_icon(percent, charging):
    lvl = int(round(percent / 10.0)) * 10
    return f"battery-level-{lvl}{'-charging' if charging else ''}-symbolic"


def ac_bands(samples, t_end=None):
    """Contiguous on-AC ranges from samples."""
    out, start, prev = [], None, None
    for s in samples:
        if s["ac"] and start is None:
            start = s["ts"]
        if not s["ac"] and start is not None:
            out.append((start, s["ts"], "charging"))
            start = None
        if prev is not None and start is not None and s["ts"] - prev > analysis.GAP:
            out.append((start, prev, "charging"))
            start = s["ts"] if s["ac"] else None
        prev = s["ts"]
    if start is not None:
        out.append((start, t_end or prev, "charging"))
    return out


def level_points(samples):
    return [(s["ts"], s["percent"]) for s in samples]


def draw_points(samples):
    """Battery draw in W; rows without a power reading (imported history) use the energy slope."""
    out, prev = [], None
    for s in samples:
        v = None
        if not s["ac"]:
            if s["power"] is not None:
                v = abs(s["power"])
            elif prev is not None and not prev["ac"] and 0 < s["ts"] - prev["ts"] <= analysis.GAP and s["energy"] is not None:
                v = max(0.0, (prev["energy"] - s["energy"]) / (s["ts"] - prev["ts"]) * 3600)
        out.append((s["ts"], v))
        prev = s
    return out


def fmt_w(v):
    return f"{v:.0f} W" if v >= 10 else f"{v:.2f}".rstrip("0").rstrip(".") + " W"


def fmt_pct(v, digits=0):
    return "—" if v is None else f"{v:.{digits}f} %"


def day_title(t):
    d = time.localtime(t)
    today = time.localtime()
    if (d.tm_year, d.tm_yday) == (today.tm_year, today.tm_yday):
        return "Today"
    y = time.localtime(time.time() - 86400)
    if (d.tm_year, d.tm_yday) == (y.tm_year, y.tm_yday):
        return "Yesterday"
    return time.strftime("%A, %d %B", d)


def hm(t):
    return time.strftime("%H:%M", time.localtime(t))


def midnight():
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def merge_apps(apps):
    """Rows that resolve to the same app name (e.g. a scope and a process) become one."""
    merged = {}
    for a in apps:
        name = names.describe(a.key)[0]
        m = merged.get(name)
        if m is None:
            merged[name] = analysis.AppUse(a.key, a.cpu, a.share, a.energy)
        else:
            m.cpu += a.cpu
            m.share += a.share
            m.energy = (m.energy or 0) + (a.energy or 0) if m.energy is not None or a.energy is not None else None
    return sorted(merged.values(), key=lambda a: a.cpu, reverse=True)


def app_row(a, max_share, show_energy=True):
    name, icon, kind = names.describe(a.key)
    row = Adw.ActionRow(title=GLib.markup_escape_text(name))
    sub = f"CPU {fmt_duration(a.cpu)} · {a.share * 100:.1f} %"
    if kind != "app":
        sub += f" · {kind}"
    row.set_subtitle(sub)
    img = Gtk.Image(pixel_size=32)
    if isinstance(icon, str):
        img.set_from_icon_name(icon)
    else:
        img.set_from_gicon(icon)
    row.add_prefix(img)
    box = Gtk.Box(spacing=12, valign=Gtk.Align.CENTER)
    if show_energy and a.energy is not None:
        box.append(label(f"≈ {a.energy:.2f} Wh", ("numeric",), xalign=1.0))
    bar = Gtk.ProgressBar(fraction=a.share / max_share if max_share else 0, valign=Gtk.Align.CENTER)
    bar.set_size_request(90, -1)
    box.append(bar)
    row.add_suffix(box)
    return row


# --- pages ----------------------------------------------------------------

class Page:
    title = ""
    icon = ""

    def __init__(self, win):
        self.win = win
        self.con = win.con

    def refresh(self):
        pass


class OverviewPage(Page):
    name, title, icon = "overview", "Overview", "battery-level-80-symbolic"

    def __init__(self, win):
        super().__init__(win)
        self.widget, box = page_box()
        self.range = "24 h"
        hero = Gtk.Box(spacing=18)
        self.pct = label("", ("hero",))
        hero.append(self.pct)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        self.state = label("", ("title-3",), wrap=True)
        self.detail = label("", ("dim-label",), wrap=True)
        info.append(self.state)
        info.append(self.detail)
        hero.append(info)
        box.append(hero)
        self.tiles = Tiles()
        box.append(self.tiles)
        box.append(toggle_group(RANGES, self.range, self._range))
        self.level = ChartCard("Battery level", TimeChart(fmt=lambda v: f"{v:.0f} %", y_range=(0, 100), area=True, gap=3 * 86400),
                               (("swatch-sleep", "Sleep"), ("swatch-charge", "Plugged in")))
        self.draw = ChartCard("Power draw on battery", TimeChart(fmt=fmt_w, y_range=(0, None), area=True,
                                                                empty="No time on battery in this period"))
        box.append(self.level)
        box.append(self.draw)

    def _range(self, name):
        self.range = name
        self.refresh()

    def refresh(self):
        now = time.time()
        span = dict(RANGES)[self.range]
        samples = analysis.load_samples(self.con, now - max(span, 86400) - 600)
        e = analysis.estimate(self.con, samples=samples)
        if e is None:
            self.state.set_label("No battery found")
            return
        live = {"ts": now, "ac": int(e["ac"]), "percent": e["percent"],
                "power": -e["power"] if e["power"] is not None and e["status"] == "Discharging" else e["power"]}
        series = [s for s in samples if s["ts"] >= now - span - 600] + [live]
        self.pct.set_label(f"{e['percent']:.0f} %")
        st = e["status"]
        if st == "Discharging":
            parts = ["On battery", fmt_watts(e["power"])]
            if e["left"]:
                parts.append(f"{fmt_duration(e['left'])} left")
        elif st == "Charging":
            parts = ["Charging", fmt_watts(e["power"])]
            if e["left"]:
                parts.append(f"full in {fmt_duration(e['left'])}")
        elif st == "Full":
            parts = ["Fully charged"]
        else:
            parts = ["Plugged in", "not charging"]
        self.state.set_label(" · ".join(parts))
        lim = f" · charges {e['start']}–{e['end']} %" if e.get("end") is not None and e.get("start") is not None else ""
        self.detail.set_label(f"{e['energy']:.1f} of {e['full']:.1f} Wh{lim}")
        self.win.set_battery_icon(battery_icon(e["percent"], st == "Charging"))

        sleeps = analysis.load_sleeps(self.con, now - span - 600)
        bands = [(s.start, s.end, "sleep") for s in sleeps] + ac_bands(series, now)
        self.level.chart.set_data(now - span, now, level_points(series), bands)
        self.level.set_default(f"{e['percent']:.0f} % now")
        self.draw.chart.set_data(now - span, now, draw_points(series), [(s.start, s.end, "sleep") for s in sleeps])
        self.draw.set_default(fmt_watts(e["power"]) + " now" if st == "Discharging" else "")

        today = [s for s in samples if s["ts"] >= midnight()] + [live]
        energy, bt, _ = analysis.battery_energy(today)
        used_pct = sum(max(0.0, a["percent"] - b["percent"]) for a, b in zip(today, today[1:])
                       if not a["ac"] and not b["ac"] and b["ts"] - a["ts"] <= analysis.GAP)
        avg = energy / (bt / 3600) if bt > 300 else None
        day_sleeps = [s for s in analysis.load_sleeps(self.con, now - 86400) if not s.ac and s.drain_per_hour is not None]
        drain = None
        if day_sleeps:
            hours = sum(s.duration for s in day_sleeps) / 3600
            drain = sum(s.percent_used for s in day_sleeps) / hours if hours > 0 else None
        self.tiles.set_tiles([
            ("On battery today", fmt_duration(bt) if bt else "—", None),
            ("Used today", f"{energy:.1f} Wh" if bt else "—", f"{used_pct:.0f} % of battery" if bt else None),
            ("Average draw", fmt_watts(avg), f"full battery ≈ {fmt_duration(e['full'] / avg * 3600)}" if avg else "today, on battery"),
            ("Sleep drain", f"{drain:.2f} %/h" if drain is not None else "—", "last 24 h, on battery"),
        ])


class SessionsPage(Page):
    name, title, icon = "sessions", "Sessions", "view-list-symbolic"

    def __init__(self, win):
        super().__init__(win)
        self.days = 7
        self.widget = Adw.PreferencesPage()
        self.groups = []

    def refresh(self):
        for g in self.groups:
            self.widget.remove(g)
        self.groups = []
        now = time.time()
        since = now - self.days * 86400
        samples = analysis.load_samples(self.con, since)
        sleeps = analysis.load_sleeps(self.con, since)
        items = [("session", s.start, s) for s in analysis.sessions(samples, sleeps, analysis.load_boundaries(self.con, since))
                 if s.duration >= 90 or s.ends == "now"]
        items += [("sleep", s.start, s) for s in sleeps]
        items.sort(key=lambda i: i[1], reverse=True)
        if not items:
            g = Adw.PreferencesGroup()
            g.add(Adw.StatusPage(title="No data yet", icon_name="battery-level-50-symbolic",
                                 description="The collector records a sample every 30 seconds on battery."))
            self._add(g)
            return
        group, cur_day = None, None
        for kind, t, obj in items:
            d = day_title(t)
            if d != cur_day:
                group = Adw.PreferencesGroup(title=d)
                self._add(group)
                cur_day = d
            group.add(self._session_row(obj) if kind == "session" else self._sleep_row(obj))
        more = Adw.PreferencesGroup()
        btn = Gtk.Button(label="Show older", halign=Gtk.Align.CENTER)
        btn.add_css_class("pill")
        btn.connect("clicked", self._more)
        more.add(btn)
        if self.days < store.RETENTION_DAYS:
            self._add(more)

    def _add(self, g):
        self.widget.add(g)
        self.groups.append(g)

    def _more(self, _b):
        self.days = min(store.RETENTION_DAYS, self.days + 7)
        self.refresh()

    def _session_row(self, s):
        end = "now" if s.ends == "now" else hm(s.end)
        row = Adw.ActionRow(title=f"{hm(s.start)} – {end}", activatable=True)
        parts = [f"{fmt_duration(s.duration)} awake"]
        if s.battery_time > 60:
            parts.append(f"{fmt_duration(s.battery_time)} on battery")
        if s.avg_power:
            parts.append(fmt_watts(s.avg_power))
        if s.percent_charged > 0.5:
            parts.append(f"+{s.percent_charged:.0f} % charged")
        row.set_subtitle(" · ".join(parts))
        on_batt = s.battery_time > s.ac_time
        row.add_prefix(Gtk.Image(icon_name="battery-level-60-symbolic" if on_batt else "ac-adapter-symbolic"))
        if s.percent_used >= 0.5:
            row.add_suffix(label(f"−{s.percent_used:.0f} %", ("numeric",)))
        row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        row.connect("activated", lambda _r: SessionDialog(self.win, s).present(self.win))
        return row

    def _sleep_row(self, s):
        row = Adw.ActionRow(title=f"Sleep {hm(s.start)} – {hm(s.end)}")
        row.add_prefix(Gtk.Image(icon_name="weather-clear-night-symbolic"))
        row.set_subtitle(sleep_subtitle(s))
        if s.percent_used is not None and s.percent_used >= 0.5 and not s.ac:
            row.add_suffix(label(f"−{s.percent_used:.0f} %", ("numeric",)))
        return row


def sleep_subtitle(s):
    parts = [fmt_duration(s.duration)]
    if s.ac:
        parts.append("on AC")
    elif s.drain_per_hour is not None:
        parts.append(f"{s.drain_per_hour:.2f} %/h")
        if s.watts is not None:
            parts.append(f"{s.watts:.2f} W")
    if s.hw_fraction is not None:
        parts.append(f"deep sleep {s.hw_fraction * 100:.0f} %")
    return " · ".join(parts)


class SessionDialog(Adw.Dialog):
    def __init__(self, win, s):
        super().__init__(title=f"{day_title(s.start)}, {hm(s.start)} – {hm(s.end)}", content_width=820, content_height=700)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        scroll, box = page_box()
        tv.set_content(scroll)
        self.set_child(tv)
        tiles = Tiles()
        tiles.set_tiles([
            ("Awake", fmt_duration(s.duration), f"{hm(s.start)} – {hm(s.end)}"),
            ("On battery", fmt_duration(s.battery_time) if s.battery_time else "—", None),
            ("Used", f"{s.energy_used:.1f} Wh" if s.battery_time else "—", f"{s.percent_used:.0f} % of battery" if s.battery_time else None),
            ("Average draw", fmt_watts(s.avg_power),
             f"full battery ≈ {fmt_duration(s.samples[-1]['full'] / s.avg_power * 3600)}" if s.avg_power else None),
        ])
        box.append(tiles)
        t0, t1 = s.start, max(s.end, s.start + 600)
        lvl = ChartCard("Battery level", TimeChart(fmt=lambda v: f"{v:.0f} %", y_range=(0, 100), gap=3 * 86400),
                        (("swatch-charge", "Plugged in"),))
        lvl.chart.set_data(t0, t1, level_points(s.samples), ac_bands(s.samples, s.end))
        box.append(lvl)
        if s.battery_time > 60:
            pw = ChartCard("Power draw on battery", TimeChart(fmt=fmt_w, y_range=(0, None), empty="No readings"))
            pw.chart.set_data(t0, t1, draw_points(s.samples))
            pw.set_default(f"avg {fmt_watts(s.avg_power)}" if s.avg_power else "")
            box.append(pw)
        apps, _ = analysis.app_usage(win.con, s.start, s.end, battery_only=s.battery_time > s.ac_time, samples=s.samples)
        apps = merge_apps(apps)
        if apps:
            g = Adw.PreferencesGroup(title="Apps", description="Share of CPU time" + (" on battery" if s.battery_time > s.ac_time else ""))
            mx = max(a.share for a in apps)
            for a in apps[:10]:
                g.add(app_row(a, mx))
            box.append(g)


class SleepPage(Page):
    name, title, icon = "sleep", "Sleep", "weather-clear-night-symbolic"

    def __init__(self, win):
        super().__init__(win)
        self.widget, self.box = page_box()

    def refresh(self):
        clear(self.box)
        now = time.time()
        sleeps = analysis.load_sleeps(self.con, now - 30 * 86400)
        week = [s for s in sleeps if s.end >= now - 7 * 86400]
        batt = [s for s in week if not s.ac and s.drain_per_hour is not None and s.duration > 600]
        hours = sum(s.duration for s in batt) / 3600
        drain = sum(s.percent_used for s in batt) / hours if hours > 0 else None
        hw = [s for s in week if s.hw_fraction is not None]
        deep = sum(s.hw_sleep for s in hw) / sum(s.duration for s in hw) if hw else None
        tiles = Tiles()
        tiles.set_tiles([
            ("Sleeps", str(len(week)), "last 7 days"),
            ("Asleep", fmt_duration(sum(s.duration for s in week)) if week else "—", "last 7 days"),
            ("Drain on battery", f"{drain:.2f} %/h" if drain is not None else "—",
             f"≈ {100 / drain / 24:.1f} days from full" if drain else None),
            ("Deep sleep", fmt_pct(deep * 100 if deep is not None else None), "share of time asleep"),
        ])
        self.box.append(tiles)
        recent = [s for s in sleeps if not s.ac and s.drain_per_hour is not None and s.duration > 600][-40:]
        if recent:
            chart = BarChart(fmt=lambda v: f"{v:.1f}")
            card = ChartCard("Drain per hour on battery, %", chart)
            days = [time.strftime("%d %b", time.localtime(s.start)) for s in recent]
            chart.set_items([("" if i and days[i] == days[i - 1] else days[i], max(0.0, s.drain_per_hour),
                              f"{time.strftime('%a %d %H:%M', time.localtime(s.start))} · {s.drain_per_hour:.2f} %/h"
                              + (f" · deep {s.hw_fraction * 100:.0f} %" if s.hw_fraction is not None else ""))
                             for i, s in enumerate(recent)])
            card.set_default(f"{len(recent)} sleeps")
            self.box.append(card)
        g = Adw.PreferencesGroup(
            title="History",
            description="Deep sleep is the share of suspend time the platform spent in its lowest-power state (S0ix). "
                        "When it is low, a device kept the system from sleeping fully.")
        for s in reversed(sleeps):
            row = Adw.ActionRow(title=f"{day_title(s.start)}, {hm(s.start)} – {hm(s.end)}", subtitle=sleep_subtitle(s))
            if s.wake:
                row.set_tooltip_text(f"Woken by {s.wake}")
            f = s.hw_fraction
            if f is None:
                icon, css = "weather-clear-night-symbolic", None
            elif f >= 0.8:
                icon, css = "object-select-symbolic", "success"
            elif f >= 0.3:
                icon, css = "dialog-warning-symbolic", "warning"
            else:
                icon, css = "dialog-warning-symbolic", "error"
            img = Gtk.Image(icon_name=icon)
            if css:
                img.add_css_class(css)
            row.add_prefix(img)
            if s.percent_used is not None and not s.ac:
                row.add_suffix(label(f"−{s.percent_used:.1f} %", ("numeric",)))
            g.add(row)
        if not sleeps:
            g.add(Adw.ActionRow(title="No sleeps recorded yet"))
        self.box.append(g)


class AppsPage(Page):
    name, title, icon = "apps", "Apps", "application-x-executable-symbolic"

    def __init__(self, win):
        super().__init__(win)
        self.widget, box = page_box()
        self.range = "24 h"
        self.all = False
        bar = Gtk.Box(spacing=12)
        bar.append(toggle_group(APP_RANGES, self.range, self._range))
        bar.append(Gtk.Box(hexpand=True))
        bar.append(label("Include time on AC", ("dim-label",)))
        sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        sw.connect("notify::active", lambda w, _p: self._all(w.get_active()))
        bar.append(sw)
        box.append(bar)
        self.tiles = Tiles(3)
        box.append(self.tiles)
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.list)

    def _range(self, name):
        self.range = name
        self.refresh()

    def _all(self, v):
        self.all = v
        self.refresh()

    def refresh(self):
        clear(self.list)
        now = time.time()
        since = now - dict(APP_RANGES)[self.range]
        samples = analysis.load_samples(self.con, since)
        apps, total = analysis.app_usage(self.con, since, battery_only=not self.all, samples=samples)
        apps = merge_apps(apps)
        e, t, floor = analysis.battery_energy(samples)
        active = max(0.0, e - floor * t / 3600) if floor is not None else None
        self.tiles.set_tiles([
            ("Energy on battery", f"{e:.1f} Wh" if t else "—", fmt_duration(t) if t else None),
            ("Idle floor", fmt_watts(floor), "screen, Wi-Fi, platform"),
            ("Above idle", f"{active:.1f} Wh" if active is not None else "—", "split between apps by CPU time"),
        ])
        g = Adw.PreferencesGroup(
            title="By CPU time" + ("" if self.all else " on battery"),
            description="Energy is an estimate: the draw above the idle floor, split by each app's share of CPU time. "
                        "GPU, screen and radios are not attributed.")
        if not apps:
            g.add(Adw.ActionRow(title="No data for this period"))
        mx = max((a.share for a in apps), default=1)
        for a in apps[:40]:
            g.add(app_row(a, mx, show_energy=not self.all))
        self.list.append(g)


class HealthPage(Page):
    name, title, icon = "health", "Battery health", "battery-level-100-symbolic"

    def __init__(self, win):
        super().__init__(win)
        self.widget, self.box = page_box()

    def refresh(self):
        clear(self.box)
        b = sysfs.battery()
        if b is None:
            return
        rows, discharged = analysis.health(self.con)
        health = 100 * b["full"] / b["design"] if b["design"] else None
        cycles = b["cycles"]
        if cycles:
            cyc, cyc_sub = str(cycles), "reported by the battery"
        else:
            cyc = f"≈ {discharged / b['design']:.1f}" if b["design"] else "—"
            cyc_sub = "estimated since tracking began"
        limit = f"{b['start']}–{b['end']} %" if b["end"] is not None else "—"
        tiles = Tiles(3)
        tiles.set_tiles([
            ("Health", fmt_pct(health, 1), "full charge vs design"),
            ("Full charge", f"{b['full']:.1f} Wh", f"design {b['design']:.1f} Wh"),
            ("Cycles", cyc, cyc_sub),
            ("Charge limit", limit, "start–stop thresholds" if b["end"] is not None else None),
            ("Discharged", f"{discharged:.0f} Wh", "since tracking began"),
            ("Power now", fmt_watts(b["power"]), b["status"].lower()),
        ])
        self.box.append(tiles)
        if len(rows) >= 2:
            pts = [(time.mktime(time.strptime(r["day"], "%Y-%m-%d")) + 43200, r["full"]) for r in rows]
            chart = TimeChart(fmt=lambda v: f"{v:.1f}", area=False, gap=10 * 86400)
            card = ChartCard("Full charge capacity, Wh", chart)
            chart.set_data(pts[0][0] - 43200, pts[-1][0] + 43200, pts)
            self.box.append(card)
        g = Adw.PreferencesGroup(title="Battery")
        for k, v in (("Manufacturer", b["vendor"]), ("Model", b["model"]), ("Technology", b["tech"]),
                     ("Device", b["name"] + (f" (+{b['count'] - 1})" if b["count"] > 1 else ""))):
            if v:
                row = Adw.ActionRow(title=k)
                row.add_suffix(label(v, ("dim-label",)))
                g.add(row)
        self.box.append(g)


class MeasurePage(Page):
    name, title, icon = "measure", "Measure", "preferences-system-time-symbolic"

    def __init__(self, win):
        super().__init__(win)
        self.widget = Adw.PreferencesPage()
        self.cancel = False
        self.running = False
        g = Adw.PreferencesGroup(
            title="Measure power draw",
            description="Averages the battery draw over a few minutes. Run it on battery, leave the laptop alone, "
                        "and keep brightness and displays the same between runs you compare.")
        self.label_row = Adw.EntryRow(title="Label")
        g.add(self.label_row)
        self.minutes = Adw.SpinRow.new_with_range(1, 30, 1)
        self.minutes.set_title("Duration, minutes")
        self.minutes.set_value(5)
        g.add(self.minutes)
        self.progress_row = Adw.ActionRow(title="Ready")
        self.progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER, hexpand=True)
        self.progress.set_size_request(160, -1)
        self.button = Gtk.Button(label="Start", valign=Gtk.Align.CENTER)
        self.button.add_css_class("suggested-action")
        self.button.connect("clicked", self._toggle)
        self.progress_row.add_suffix(self.progress)
        self.progress_row.add_suffix(self.button)
        g.add(self.progress_row)
        self.widget.add(g)
        self.results = None

    def refresh(self):
        if self.results:
            self.widget.remove(self.results)
        self.results = Adw.PreferencesGroup(title="Results")
        rows = analysis.benches(self.con)
        if not rows:
            self.results.add(Adw.ActionRow(title="No measurements yet"))
        for i, b in enumerate(rows):
            prev = next((p for p in rows[i + 1:] if p["valid"]), None)
            title = GLib.markup_escape_text(b["label"] or "Untitled")
            sub = (f"{time.strftime('%d %b %H:%M', time.localtime(b['start']))} · "
                   f"{fmt_duration(b['end'] - b['start'])} · brightness {b['brightness']:.0f} % · {b['profile']}"
                   + (f" · {b['external']} external" if b["external"] else "") + (f" · {b['note']}" if b["note"] else ""))
            row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(sub))
            if not b["valid"]:
                img = Gtk.Image(icon_name="dialog-warning-symbolic")
                img.add_css_class("warning")
                img.set_tooltip_text("Conditions changed during the run")
                row.add_prefix(img)
            if prev and b["valid"]:
                d = b["avg"] - prev["avg"]
                row.add_suffix(label(f"{'+' if d >= 0 else '−'}{abs(d):.2f} W", ("dim-label", "numeric")))
            row.add_suffix(label(f"{b['avg']:.2f} W", ("heading", "numeric")))
            rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Delete")
            rm.add_css_class("flat")
            rm.connect("clicked", lambda _b, bid=b["id"]: self._delete(bid))
            row.add_suffix(rm)
            self.results.add(row)
        self.widget.add(self.results)

    def _delete(self, bid):
        self.con.execute("DELETE FROM benches WHERE id = ?", (bid,))
        self.refresh()

    def _toggle(self, _b):
        if self.running:
            self.cancel = True
            return
        if sysfs.on_ac():
            self.win.toast("Unplug the charger to measure")
            return
        self.running, self.cancel = True, False
        self.button.set_label("Stop")
        self.button.remove_css_class("suggested-action")
        self.button.add_css_class("destructive-action")
        minutes, lab = self.minutes.get_value(), self.label_row.get_text().strip()
        threading.Thread(target=self._run, args=(minutes, lab), daemon=True).start()

    def _run(self, minutes, lab):
        from .. import bench

        def progress(phase, frac, avg):
            GLib.idle_add(self._progress, phase, frac, avg)

        try:
            bench.run(minutes, lab, out=lambda _m: None, stop=lambda: self.cancel, progress=progress)
            msg = None
        except RuntimeError as e:
            msg = str(e)
        GLib.idle_add(self._done, msg)

    def _progress(self, phase, frac, avg):
        self.progress.set_fraction(max(0.0, min(1.0, frac)))
        self.progress_row.set_title("Settling…" if phase == "warmup" else
                                    f"Measuring · {avg:.2f} W" if avg else "Measuring…")
        return False

    def _done(self, msg):
        self.running = False
        self.button.set_label("Start")
        self.button.remove_css_class("destructive-action")
        self.button.add_css_class("suggested-action")
        self.progress.set_fraction(0)
        self.progress_row.set_title("Ready")
        if msg:
            self.win.toast(msg)
        self.refresh()
        return False


PAGES = [OverviewPage, SessionsPage, SleepPage, AppsPage, HealthPage, MeasurePage]


# --- window ---------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app, page=None):
        super().__init__(application=app, title="Battery Monitor", default_width=1060, default_height=760)
        self.con = store.connect()
        self.flushed = 0
        self.pages = [cls(self) for cls in PAGES]
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        for p in self.pages:
            self.stack.add_named(p.widget, p.name)

        self.sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE)
        self.sidebar.add_css_class("navigation-sidebar")
        self.icons = {}
        for p in self.pages:
            row = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6)
            img = Gtk.Image(icon_name=p.icon)
            self.icons[p.name] = img
            row.append(img)
            row.append(label(p.title))
            self.sidebar.append(row)
        self.sidebar.connect("row-activated", self._select)
        self.sidebar.connect("row-selected", lambda lb, r: r and self._show(r.get_index()))

        side_tv = Adw.ToolbarView()
        side_tv.add_top_bar(Adw.HeaderBar())
        side_tv.set_content(Gtk.ScrolledWindow(child=self.sidebar, hscrollbar_policy=Gtk.PolicyType.NEVER))
        side = Adw.NavigationPage(title="Battery Monitor", child=side_tv)

        menu = Gio.Menu()
        menu.append("Powertop Report", "app.powertop")
        menu.append("About Battery Monitor", "app.about")
        head = Adw.HeaderBar()
        head.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main Menu"))
        self.toasts = Adw.ToastOverlay(child=self.stack)
        content_tv = Adw.ToolbarView()
        content_tv.add_top_bar(head)
        content_tv.set_content(self.toasts)
        self.content = Adw.NavigationPage(title="Overview", child=content_tv)

        self.split = Adw.NavigationSplitView(sidebar=side, content=self.content, min_sidebar_width=200, max_sidebar_width=240)
        self.set_content(self.split)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 680sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

        names_ = [p.name for p in self.pages]
        self.sidebar.select_row(self.sidebar.get_row_at_index(names_.index(page) if page in names_ else 0))
        GLib.timeout_add_seconds(30, self._tick)

    def _select(self, _lb, row):
        self.split.set_show_content(True)

    def _show(self, i):
        p = self.pages[i]
        self.stack.set_visible_child_name(p.name)
        self.content.set_title(p.title)
        p.refresh()
        if time.monotonic() - self.flushed > 60 and store.request_flush():
            self.flushed = time.monotonic()
            GLib.timeout_add(400, lambda: (self.current().refresh(), False)[1])

    def current(self):
        return next(p for p in self.pages if p.name == self.stack.get_visible_child_name())

    def _tick(self):
        if self.is_visible() and isinstance(self.current(), OverviewPage):
            self.current().refresh()
        return True

    def set_battery_icon(self, name):
        self.icons["overview"].set_from_icon_name(name)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text))


class App(Adw.Application):
    def __init__(self, page=None):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.page = page
        self.connect("activate", self._activate)
        for name, cb in (("powertop", self._powertop), ("about", self._about)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
        self.set_accels_for_action("window.close", ["<Control>w"])
        self.set_accels_for_action("app.quit", ["<Control>q"])
        q = Gio.SimpleAction.new("quit", None)
        q.connect("activate", lambda *_: self.quit())
        self.add_action(q)

    def _activate(self, _app):
        win = self.get_active_window()
        if not win:
            prov = Gtk.CssProvider()
            prov.load_from_string(CSS)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            win = Window(self, self.page)
        win.present()

    def _powertop(self, *_):
        win = self.get_active_window()
        win.toast("Running powertop for 30 seconds…")

        def run():
            import subprocess
            r = subprocess.run([sys.executable, "-m", "battery_monitor", "powertop"], capture_output=True, text=True)
            GLib.idle_add(win.toast, "Report opened" if r.returncode == 0 else "Powertop was cancelled or failed")

        threading.Thread(target=run, daemon=True).start()

    def _about(self, *_):
        Adw.AboutDialog(application_name="Battery Monitor", application_icon="battery-level-80-symbolic",
                        version=__version__, license_type=Gtk.License.MIT_X11,
                        website="https://github.com/Rymond044/linux-battery-monitor",
                        comments="Battery usage history for Linux laptops").present(self.get_active_window())


def _screenshot(win, out):
    """Dev aid (README images, visual checks): BATTERY_MONITOR_SCREENSHOTS=dir renders the open page to PNG and quits.
    Only the first frame of a window is guaranteed when it sits on a hidden workspace, hence one page per run."""
    import os
    gi.require_version("Graphene", "1.0")
    from gi.repository import Graphene
    os.makedirs(out, exist_ok=True)

    def render():
        w, h = win.get_width(), win.get_height()
        snap = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(win).snapshot(snap, w, h)
        node = snap.to_node()
        if node:
            tex = win.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
            tex.save_to_png(os.path.join(out, f"{win.current().name}.png"))
        win.get_application().quit()
        return False

    GLib.timeout_add(1500, render)


def main(page=None):
    import os
    app = App(page)
    scheme = os.environ.get("BATTERY_MONITOR_SCHEME")
    out = os.environ.get("BATTERY_MONITOR_SCREENSHOTS")

    def ready(_a):
        if scheme:
            Adw.StyleManager.get_default().set_color_scheme(
                Adw.ColorScheme.FORCE_DARK if scheme == "dark" else Adw.ColorScheme.FORCE_LIGHT)
        if out:
            _screenshot(app.get_active_window(), out)

    app.connect_after("activate", ready)
    return app.run([sys.argv[0]])
