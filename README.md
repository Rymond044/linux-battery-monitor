# Battery Monitor

Battery usage history for Linux laptops: where the charge went, how much it drains while
asleep, and which apps were busy on battery. A GTK 4 / libadwaita window on top of a tiny
background sampler. No root, no daemons beyond one user service.

## What it shows

- **Overview** — charge, draw and time left; battery level and power draw over the last
  6 hours to 7 days, with sleep and plugged-in periods shaded.
- **Sessions** — every awake period between two sleeps (or boot/shutdown): time on battery,
  energy and percent used, average draw, and the apps that were busy.
- **Sleep** — drain per hour for each suspend and the *deep sleep* share: how much of the
  suspend the platform actually spent in its lowest-power state (S0ix). A low share means
  some device kept the system half awake.
- **Apps** — CPU time per app on battery, with an estimate of the energy above the idle floor.
  Apps are told apart by their systemd scope (`app-*.scope`), so this works best with a
  session that launches apps that way (GNOME, KDE, uwsm, …).
- **Battery health** — full-charge vs design capacity, cycles (reported or estimated),
  charge thresholds.
- **Measure** — average draw over a few minutes, for before/after comparisons of a setting.
  Runs are flagged when brightness, displays or the power profile change midway.

## How it collects

`battery-monitor collect` runs as a systemd user service. It wakes up once every 30 s on
battery (every 5 min on AC), reads a handful of sysfs files and cgroup `cpu.stat`s, and writes
to SQLite every few minutes. It notices plug/unplug instantly from kernel uevents and suspend
from the gap between `CLOCK_BOOTTIME` and `CLOCK_MONOTONIC`, so there are no D-Bus
connections and no polling between samples. Data older than 90 days is pruned.

On first start it backfills what UPower and the journal already know (charge levels and
suspend times), so the window is not empty.

Data: `~/.local/state/battery-monitor/data.sqlite`.

## Install

Requirements: Python 3.11+, PyGObject, GTK 4, libadwaita ≥ 1.7. Optional: `powertop` and
`pkexec` for the Powertop report.

```sh
git clone https://github.com/Rymond044/linux-battery-monitor
cd linux-battery-monitor
./install.sh            # links into ~/.local, starts the sampler
./install.sh uninstall
```

## Command line

```
battery-monitor                 open the window
battery-monitor status [--json] charge, draw and estimate
battery-monitor sessions        awake periods between sleeps
battery-monitor sleeps          suspends with drain and deep-sleep share
battery-monitor apps            CPU time per app on battery
battery-monitor bench --label X measure average draw (on battery)
battery-monitor benches         list measurements
battery-monitor powertop        powertop HTML report (asks for your password)
```

## License

MIT
