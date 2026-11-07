#!/bin/sh
# Installs from this checkout into ~/.local (symlinked, so `git pull` updates the app).
#   ./install.sh            install and start the sampler
#   ./install.sh uninstall  stop the sampler and remove the files (data is kept)
set -e
repo=$(cd "$(dirname "$0")" && pwd)
id=io.github.rymond044.BatteryMonitor
bin=$HOME/.local/bin/battery-monitor
data=${XDG_DATA_HOME:-$HOME/.local/share}
unit=$data/systemd/user/battery-monitor.service

if [ "${1:-}" = uninstall ]; then
	systemctl --user disable --now battery-monitor.service 2>/dev/null || true
	rm -f "$bin" "$unit" "$data/applications/$id.desktop" "$data/icons/hicolor/scalable/apps/$id.svg"
	systemctl --user daemon-reload
	echo "Removed. Data is in ${XDG_STATE_HOME:-$HOME/.local/state}/battery-monitor"
	exit 0
fi

python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")' 2>/dev/null ||
	echo "warning: GTK 4 / libadwaita bindings not found (python-gobject, gtk4, libadwaita); only the CLI will work" >&2

mkdir -p "$(dirname "$bin")" "$(dirname "$unit")" "$data/applications" "$data/icons/hicolor/scalable/apps"
ln -sf "$repo/bin/battery-monitor" "$bin"
sed "s|@BIN@|$bin|" "$repo/data/battery-monitor.service" >"$unit"
sed "s|@BIN@|$bin|" "$repo/data/$id.desktop" >"$data/applications/$id.desktop"
cp "$repo/data/$id.svg" "$data/icons/hicolor/scalable/apps/$id.svg"
gtk-update-icon-cache -q -t "$data/icons/hicolor" 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable --now battery-monitor.service
echo "Installed. Open “Battery Monitor” or run: battery-monitor"
