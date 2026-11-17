# Linux setup

[Инструкция на русском](linux.ru.md)

The existing CYD firmware and 12-field USB protocol work unchanged. The Linux
feeder uses kernel sensors directly; LibreHardwareMonitor is only used on Windows.

## Install with one file

Download `cyd-pc-stats-1.2.1-linux.run` and its `.sha256` file from the
[Linux release](https://github.com/4uva4ek/esphome-cyd-pc-monitor/releases/tag/linux-v1.2.1).
The installer contains the feeder sources; no Git checkout is required.
Python dependencies are downloaded from PyPI during installation, so internet
access is required. Run it in a terminal as your normal user:

```bash
sha256sum -c cyd-pc-stats-1.2.1-linux.run.sha256
bash cyd-pc-stats-1.2.1-linux.run
```

Requirements: Linux, Python 3.9+, venv/pip, Bash, tar, awk, and a systemd user
session for autostart. Install the prerequisites for your distribution:

```bash
# Arch / CachyOS
sudo pacman -S --needed python python-pip
# Debian / Ubuntu
sudo apt install python3 python3-venv python3-pip
# Fedora
sudo dnf install python3 python3-pip
```

Alternatively, install directly from this fork:

```bash
git clone https://github.com/4uva4ek/esphome-cyd-pc-monitor.git
cd esphome-cyd-pc-monitor
./install.sh
```

Both entry points accept the same options:

| Option | Meaning |
|---|---|
| `--no-autostart` | Install without enabling a service; also stops/disables an existing service when the user manager is available. |
| `--port /dev/serial/by-id/usb-…` | Select the display explicitly; persistent by-id paths survive ttyUSB numbering changes. |
| `--interval 2` | Send every two seconds (default: 1). |
| `--cpu-fan 'nct6799:fan2'` | Choose a CPU fan using `--list-sensors`. |
| `--gpu-fan 'amdgpu:fan1'` | Choose an AMD GPU fan. |
| `--nvidia-gpu 0` | Select an NVIDIA index, UUID, or PCI address. |
| `--drm-card card1` | Select an AMD GPU explicitly, bypassing NVIDIA. |
| `--verbose` | Include sent packets in the journal. |

Re-run the installer to upgrade. Pass your preferred options again when upgrading.
Files go to `${XDG_DATA_HOME:-$HOME/.local/share}/cyd-pc-stats`; the unit goes to
`${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/cyd-pc-stats.service`.
It runs at user login, without root. On systems without systemd, use
`--no-autostart` and add the installed `run.sh` to your desktop's startup programs.
The installer does not install system packages, change groups or flash the CYD.

## USB access and firmware

Flash one of the [upstream firmware images](https://github.com/nyburglar/esphome-cyd-pc-monitor/releases/latest)
as described in the main README. Stop the feeder before flashing:

```bash
systemctl --user stop cyd-pc-stats
# Flash using web.esphome.io (Chrome/Chromium) or ESPHome, then:
systemctl --user start cyd-pc-stats
```

Check the serial device and its group:

```bash
ls -l /dev/serial/by-id/ /dev/ttyUSB* /dev/ttyACM*
id -nG
```

If you lack serial access, add your account to the **actual device group**:

```bash
sudo usermod -aG uucp "$USER"     # Arch / CachyOS, when the port's group is uucp
# or, when the port's group is dialout:
sudo usermod -aG dialout "$USER"
```

Then log out of all sessions and log back in (or reboot). Restarting just the
terminal may not refresh groups in the systemd user manager. Do not run the feeder
as root or make serial devices world-writable. CH340/CH9102/CP210x adapters are
auto-detected; if more than one is connected, specify `--port` to avoid sending
packets to the wrong device.

## Sensors and diagnostics

```bash
app="${XDG_DATA_HOME:-$HOME/.local/share}/cyd-pc-stats"
"$app/venv/bin/python" "$app/pc_stats_feeder.py" --list-sensors
"$app/venv/bin/python" "$app/pc_stats_feeder.py" --dry-run --once
journalctl --user -u cyd-pc-stats -n 30
systemctl --user status cyd-pc-stats
```

`--dry-run` never opens the serial port. `--once` waits one sampling interval so
CPU load and readable RAPL counters have a baseline. Missing values are empty
fields on the wire and `--` on the display, never fabricated zeroes.

| Metric | Linux source / limitations |
|---|---|
| CPU load, RAM | psutil; RAM usage excludes available cache. |
| CPU temperature | `k10temp`/`zenpower` (AMD), `coretemp` (Intel), or a CPU thermal zone. Disk and RAM temperatures are never mistaken for CPU. |
| CPU power | Readable package `energy_uj` counters under powercap, converted to watts over the sample interval; alternatively package power in hwmon. Root-only counters remain unknown. Core/SoC power is not presented as whole-package power. |
| NVIDIA temperature/load/power/VRAM | `nvidia-smi` from the installed NVIDIA driver. Default: GPU 0. Unsupported fields stay unknown. |
| AMD temperature/load/power/VRAM/RPM | The kernel `amdgpu` DRM/hwmon files. Default: AMD card with the largest VRAM, if no NVIDIA readings are available. For APUs, the driver may report whole-SoC power and reserved memory. |
| CPU fan RPM | hwmon fan labeled CPU, or an explicit label, `chip:label`, `chip:fanN`, or full sysfs path. Unlabeled case fans are not guessed as CPU fans. |
| NVIDIA fan RPM | NVML reports RPM per fan where supported. The display shows the highest available RPM; all fans stopped gives 0, unsupported gives `--`. No percentage conversion. |
| Intel GPU | No GPU metrics backend in this version. CPU/RAM still work. |

GPU values are always taken from one backend/card, including when individual
fields are unavailable. You can use `sensors` from lm-sensors to investigate
missing hwmon entries, but that package is not required by the feeder.

Host smoke test: CachyOS, Ryzen 7 9800X3D, RTX 5090. CPU temperature/load,
GPU temperature/load/power/VRAM, and RAM were read successfully. On that host,
CPU package energy needed the opt-in permission rule below. Loading the built-in nct6683 module exposed the MSI board fans; CPU is nct6687:fan1 and fan2 is the pump. Both RTX 5090 fans reported 0 RPM at idle.
USB transport is covered by a pseudo-terminal integration test; the physical
CYD screen has not been visually verified. Other sensor paths are fixture-tested.

## Enable CPU watts when energy is root-only (optional)

First inspect the change, then explicitly grant your group read access:

```bash
app="${XDG_DATA_HOME:-$HOME/.local/share}/cyd-pc-stats"
python3 "$app/configure_cpu_power.py" --group "$(id -gn)" --dry-run
sudo python3 "$app/configure_cpu_power.py" --group "$(id -gn)"
```

This grants the selected existing group **read-only** access to package energy
counters and installs `/etc/udev/rules.d/70-cyd-cpu-power.rules` for persistence.
All members of that group can read energy telemetry; select a private group if
needed. CPU power limits, fan controls and the feeder's user identity are unchanged.
The first reading establishes a baseline; watts appear on the next sample.

Remove this optional setting **before uninstalling the app**, if desired:

```bash
sudo python3 "$app/configure_cpu_power.py" --group "$(id -gn)" --remove
```

## MSI MAG X870E TOMAHAWK CPU fan

On the tested CachyOS host, the kernel already provides the signed `nct6683`
module for Nuvoton NCT6687. If no motherboard fans appear in `--list-sensors`:

```bash
sudo modprobe nct6683
# After verifying the fan readings, load the driver at future boots:
printf 'nct6683\n' | sudo tee /etc/modules-load.d/cyd-hwmon.conf
bash cyd-pc-stats-1.2.1-linux.run --cpu-fan nct6687:fan1
```

For this board, `fan1` is CPU and `fan2` is the pump. Other boards may differ;
choose the channel for your own hardware. This does not write any PWM/fan curves.
The module-loading configuration is separate from the user app; remove
`/etc/modules-load.d/cyd-hwmon.conf` if you no longer want that boot setting.

## Uninstall

```bash
bash "${XDG_DATA_HOME:-$HOME/.local/share}/cyd-pc-stats/uninstall.sh"
```

This removes the user service and installed application directory. Firmware,
Python, GPU drivers and serial group membership are left in place.

## Build the installer / run tests

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r feeder/requirements.txt
.venv/bin/python -m unittest discover -s tests -v
python3 scripts/build_linux_installer.py
```

The `.run` file and SHA-256 checksum appear in `dist/`. The package bundles the
same sources that `install.sh` uses, plus the license and Linux instructions.

Interface references: [Linux hwmon](https://docs.kernel.org/hwmon/sysfs-interface.html),
[powercap](https://docs.kernel.org/power/powercap/powercap.html),
[AMDGPU sensors](https://docs.kernel.org/gpu/amdgpu/thermal.html),
[NVIDIA SMI](https://docs.nvidia.com/deploy/nvidia-smi/index.html),
[NVML RPM API](https://docs.nvidia.com/deploy/nvml-api/latest/api/group__nvmlDeviceQueries.html),
[nct6683 kernel driver](https://docs.kernel.org/hwmon/nct6683.html).
