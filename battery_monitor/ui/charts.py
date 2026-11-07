"""Small Cairo charts that take their colors from the GTK theme.

The chart widget carries the `accent` style class, so its own color is the theme accent;
text and grid use the parent's foreground color.
"""

import math
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, Pango, PangoCairo  # noqa: E402


def _rgba(c, alpha=None):
    return (c.red, c.green, c.blue, c.alpha if alpha is None else alpha)


def nice_step(span, target=4):
    if span <= 0:
        return 1
    raw = span / target
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


TIME_STEPS = [600, 1800, 3600, 2 * 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400, 7 * 86400]


class _Base(Gtk.DrawingArea):
    def __init__(self, height):
        super().__init__()
        self.add_css_class("accent")
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.hover = None
        self.on_hover = None
        m = Gtk.EventControllerMotion()
        m.connect("motion", self._motion)
        m.connect("leave", self._leave)
        self.add_controller(m)

    def _motion(self, _c, x, _y):
        self.hover = x
        self.queue_draw()

    def _leave(self, _c):
        self.hover = None
        if self.on_hover:
            self.on_hover(None)
        self.queue_draw()

    def _colors(self):
        accent = self.get_color()
        parent = self.get_parent()
        fg = parent.get_color() if parent else accent
        return accent, fg

    def _layout(self, cr, text, size=0.82):
        lay = PangoCairo.create_layout(cr)
        font = self.get_pango_context().get_font_description().copy()
        font.set_size(int(font.get_size() * size))
        lay.set_font_description(font)
        lay.set_text(text, -1)
        return lay

    def _text(self, cr, text, x, y, color, align="left", valign="top"):
        lay = self._layout(cr, text)
        w, h = lay.get_pixel_size()
        if align == "right":
            x -= w
        elif align == "center":
            x -= w / 2
        if valign == "middle":
            y -= h / 2
        elif valign == "bottom":
            y -= h
        cr.set_source_rgba(*color)
        cr.move_to(x, y)
        PangoCairo.show_layout(cr, lay)
        return w, h


class TimeChart(_Base):
    """Single-series line/area over time with optional shaded bands (sleep, charging)."""

    def __init__(self, height=170, fmt=lambda v: f"{v:.0f}", y_range=None, area=True, gap=900, empty=""):
        super().__init__(height)
        self.empty = empty
        self.fmt = fmt
        self.y_range = y_range
        self.area = area
        self.gap = gap
        self.points = []
        self.bands = []
        self.t0 = self.t1 = 0

    def set_data(self, t0, t1, points, bands=()):
        self.t0, self.t1 = t0, t1
        self.points = [(t, v) for t, v in points if t0 <= t <= t1]
        self.bands = list(bands)
        self.queue_draw()

    def latest(self):
        for t, v in reversed(self.points):
            if v is not None:
                return t, v
        return None

    def _y_bounds(self):
        if self.y_range:
            lo, hi = self.y_range
            if hi is None:
                vals = [v for _, v in self.points if v is not None]
                hi = max(vals) if vals else 1
                step = nice_step(hi - lo)
                hi = math.ceil(hi / step) * step if hi > lo else lo + step
            return lo, hi
        vals = [v for _, v in self.points if v is not None]
        if not vals:
            return 0, 1
        lo, hi = min(vals), max(vals)
        if hi == lo:
            hi = lo + 1
        step = nice_step(hi - lo)
        return math.floor(lo / step) * step, math.ceil(hi / step) * step

    def _draw(self, _area, cr, width, height):
        accent, fg = self._colors()
        lo, hi = self._y_bounds()
        step = nice_step(hi - lo)
        ticks = []
        v = lo
        while v <= hi + step * 1e-6:
            ticks.append(v)
            v += step
        label_w = max(self._layout(cr, self.fmt(t)).get_pixel_size()[0] for t in ticks)
        _, lh = self._layout(cr, "0").get_pixel_size()
        left, right, top, bottom = label_w + 10, 6, lh / 2 + 2, lh + 8
        pw, ph = width - left - right, height - top - bottom
        if pw <= 10 or ph <= 10 or self.t1 <= self.t0:
            return
        span = self.t1 - self.t0

        def X(t):
            return left + (t - self.t0) / span * pw

        def Y(val):
            return top + (1 - (val - lo) / (hi - lo)) * ph

        # bands
        for b0, b1, kind in self.bands:
            x0, x1 = X(max(b0, self.t0)), X(min(b1, self.t1))
            if x1 - x0 < 0.5:
                continue
            cr.set_source_rgba(*(_rgba(fg, 0.07) if kind == "sleep" else _rgba(accent, 0.10)))
            cr.rectangle(x0, top, x1 - x0, ph)
            cr.fill()

        # grid + y labels
        cr.set_line_width(1)
        for t in ticks:
            y = round(Y(t)) + 0.5
            cr.set_source_rgba(*_rgba(fg, 0.10))
            cr.move_to(left, y)
            cr.line_to(left + pw, y)
            cr.stroke()
            self._text(cr, self.fmt(t), left - 8, y, _rgba(fg, 0.55), "right", "middle")

        # x labels
        tstep = next((s for s in TIME_STEPS if span / s <= 6), TIME_STEPS[-1])
        tz = -time.localtime(self.t0).tm_gmtoff
        first = math.ceil((self.t0 - tz) / tstep) * tstep + tz
        fmt = "%H:%M" if tstep < 86400 else "%a %d"
        t = first
        while t <= self.t1:
            x = X(t)
            self._text(cr, time.strftime(fmt, time.localtime(t)), x, top + ph + 6, _rgba(fg, 0.55), "center")
            t += tstep

        if self.empty and not any(v is not None for _, v in self.points):
            self._text(cr, self.empty, left + pw / 2, top + ph / 2, _rgba(fg, 0.55), "center", "middle")

        # series, split at gaps
        runs, run, prev = [], [], None
        for t, v in self.points:
            if v is None or (prev is not None and t - prev > self.gap):
                if run:
                    runs.append(run)
                run = []
            if v is not None:
                run.append((X(t), Y(min(max(v, lo), hi))))
            prev = t
        if run:
            runs.append(run)
        base = Y(lo)
        for run in runs:
            if self.area and len(run) > 1:
                cr.move_to(run[0][0], base)
                for x, y in run:
                    cr.line_to(x, y)
                cr.line_to(run[-1][0], base)
                cr.close_path()
                cr.set_source_rgba(*_rgba(accent, 0.12))
                cr.fill()
            cr.set_source_rgba(*_rgba(accent))
            cr.set_line_width(2)
            cr.set_line_join(1)
            cr.set_line_cap(1)
            if len(run) == 1:
                cr.arc(run[0][0], run[0][1], 2, 0, 2 * math.pi)
                cr.fill()
                continue
            cr.move_to(*run[0])
            for p in run[1:]:
                cr.line_to(*p)
            cr.stroke()

        # hover
        if self.hover is not None and self.points:
            t = self.t0 + (self.hover - left) / pw * span
            best = min((p for p in self.points if p[1] is not None), key=lambda p: abs(p[0] - t), default=None)
            band = next((b for b in self.bands if b[0] <= t <= b[1]), None)
            if best and abs(best[0] - t) <= max(self.gap, span / pw * 8):
                x, y = X(best[0]), Y(min(max(best[1], lo), hi))
                cr.set_source_rgba(*_rgba(fg, 0.35))
                cr.set_line_width(1)
                cr.move_to(round(x) + 0.5, top)
                cr.line_to(round(x) + 0.5, top + ph)
                cr.stroke()
                cr.set_source_rgba(*_rgba(accent))
                cr.arc(x, y, 4.5, 0, 2 * math.pi)
                cr.fill()
                if self.on_hover:
                    self.on_hover(f"{time.strftime('%a %H:%M', time.localtime(best[0]))} · {self.fmt(best[1])}")
            elif band and self.on_hover:
                self.on_hover(f"{band[2].capitalize()} {time.strftime('%H:%M', time.localtime(band[0]))}"
                              f"–{time.strftime('%H:%M', time.localtime(band[1]))}")
            elif self.on_hover:
                self.on_hover(None)


class BarChart(_Base):
    """Vertical bars, one per item: (label, value, readout)."""

    def __init__(self, height=150, fmt=lambda v: f"{v:.1f}"):
        super().__init__(height)
        self.fmt = fmt
        self.items = []

    def set_items(self, items):
        self.items = list(items)
        self.queue_draw()

    def _draw(self, _area, cr, width, height):
        accent, fg = self._colors()
        if not self.items:
            return
        hi = max(v for _, v, _ in self.items) or 1
        step = nice_step(hi, 3)
        hi = math.ceil(hi / step) * step
        ticks = [i * step for i in range(int(round(hi / step)) + 1)]
        label_w = max(self._layout(cr, self.fmt(t)).get_pixel_size()[0] for t in ticks)
        _, lh = self._layout(cr, "0").get_pixel_size()
        left, right, top, bottom = label_w + 10, 6, lh / 2 + 2, lh + 8
        pw, ph = width - left - right, height - top - bottom
        if pw <= 10 or ph <= 10:
            return
        cr.set_line_width(1)
        for t in ticks:
            y = round(top + (1 - t / hi) * ph) + 0.5
            cr.set_source_rgba(*_rgba(fg, 0.10))
            cr.move_to(left, y)
            cr.line_to(left + pw, y)
            cr.stroke()
            self._text(cr, self.fmt(t), left - 8, y, _rgba(fg, 0.55), "right", "middle")
        n = len(self.items)
        slot = pw / n
        bw = max(2.0, min(24.0, slot - 2))
        hover_i = int((self.hover - left) // slot) if self.hover is not None and self.hover >= left else None
        last_label = -1e9
        for i, (label, v, _) in enumerate(self.items):
            x = left + i * slot + (slot - bw) / 2
            h = max(1.0, v / hi * ph)
            y = top + ph - h
            r = min(4.0, bw / 2, h)
            cr.set_source_rgba(*_rgba(accent, 1.0 if hover_i in (None, i) else 0.45))
            cr.move_to(x, top + ph)
            cr.line_to(x, y + r)
            cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
            cr.line_to(x + bw - r, y)
            cr.arc(x + bw - r, y + r, r, 1.5 * math.pi, 2 * math.pi)
            cr.line_to(x + bw, top + ph)
            cr.close_path()
            cr.fill()
            if label and x + bw / 2 - last_label > 60:
                self._text(cr, label, x + bw / 2, top + ph + 6, _rgba(fg, 0.55), "center")
                last_label = x + bw / 2
        if self.on_hover:
            if hover_i is not None and 0 <= hover_i < n:
                self.on_hover(self.items[hover_i][2])
            else:
                self.on_hover(None)
