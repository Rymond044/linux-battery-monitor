"""Human names and icons for the cgroup keys stored by the collector."""

import os

from gi.repository import Gio

KNOWN = {
    "kernel": ("Kernel & interrupts", "applications-system-symbolic"),
    "user:wayland-wm": ("Compositor", "video-display-symbolic"),
    "user:pipewire": ("PipeWire", "audio-speakers-symbolic"),
    "user:pipewire-pulse": ("PipeWire (Pulse)", "audio-speakers-symbolic"),
    "user:wireplumber": ("WirePlumber", "audio-speakers-symbolic"),
    "user:init": ("systemd (user)", "applications-system-symbolic"),
    "system:docker-containers": ("Docker containers", "application-x-executable-symbolic"),
}

_index = None
_cache = {}


def _build_index():
    idx = {}
    for info in Gio.AppInfo.get_all():
        if not isinstance(info, Gio.DesktopAppInfo):
            continue
        keys = set()
        did = (info.get_id() or "").removesuffix(".desktop")
        keys.add(did.lower())
        keys.add(did.lower().split(".")[-1])
        wm = info.get_startup_wm_class()
        if wm:
            keys.add(wm.lower())
        exe = info.get_executable()
        if exe:
            keys.add(os.path.basename(exe).lower())
        for k in keys:
            if k:
                idx.setdefault(k, info)
    return idx


def describe(key):
    """(name, Gio.Icon or icon name, kind label)."""
    if key in _cache:
        return _cache[key]
    global _index
    if _index is None:
        _index = _build_index()
    kind, _, name = key.partition(":")
    if key in KNOWN:
        n, icon = KNOWN[key]
        res = (n, icon, "system" if kind in ("system", "kernel") else "service")
    else:
        info = _index.get(name.lower()) or _index.get(name.lower().split(".")[-1])
        if info and kind != "system":
            res = (info.get_display_name() or name, info.get_icon() or "application-x-executable-symbolic", "app")
        elif kind == "system":
            res = (name, "applications-system-symbolic", "system service")
        else:
            res = (name, "application-x-executable-symbolic", "service" if kind == "user" else "app")
    _cache[key] = res
    return res
