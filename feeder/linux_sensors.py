"""Read Linux hwmon, powercap and AMD DRM sensors without root or a daemon.

All sysfs access is read-only. Missing, inaccessible and invalid readings remain
unknown. The sysfs root and clock are injectable for hardware-independent tests.
"""

from dataclasses import dataclass
import math
from pathlib import Path
import re
import time


CPU_CHIPS = {"k10temp", "zenpower", "coretemp", "k8temp", "amd_energy"}
GPU_CHIPS = {"amdgpu", "radeon", "nouveau", "i915", "xe"}


def read_text(path):
    try:
        return path.read_text().strip()
    except (OSError, UnicodeError):
        return ""


def read_number(path, divisor=1):
    try:
        value = float(read_text(path)) / divisor
        return value if math.isfinite(value) else None
    except ValueError:
        return None


@dataclass
class Sensor:
    path: Path
    chip: str
    kind: str
    label: str
    value: float

    def matches(self, selector):
        return selector.casefold() in {
            str(self.path).casefold(), self.label.casefold(),
            f"{self.chip}:{self.label}".casefold(),
            f"{self.chip}:{self.path.stem.rsplit('_', 1)[0]}".casefold(),
        }


class LinuxSensors:
    def __init__(self, sysfs="/sys", clock=time.monotonic):
        self.root = Path(sysfs)
        self.clock = clock
        self.energy_samples = {}

    def hwmon(self, directory=None):
        sensors = []
        directories = ([directory] if directory is not None else
                       sorted((self.root / "class/hwmon").glob("hwmon*")))
        for hw in directories:
            chip = read_text(hw / "name")
            # Prefer average power, using instantaneous power when unavailable.
            channels = {}
            for pattern, kind, divisor in (("temp*_input", "Temperature", 1000),
                                            ("fan*_input", "Fan", 1),
                                            ("power*_average", "Power", 1e6),
                                            ("power*_input", "Power", 1e6)):
                for path in sorted(hw.glob(pattern)):
                    channel = path.stem.rsplit("_", 1)[0]
                    value = read_number(path, divisor)
                    if value is None or (kind != "Temperature" and value < 0):
                        continue
                    label = read_text(hw / f"{channel}_label") or channel
                    channels.setdefault(channel, Sensor(path, chip, kind, label, value))
            sensors.extend(channels.values())
        return sensors

    def rapl_zones(self):
        # Class entries are symlinks; do not recursively follow device/subsystem.
        base = self.root / "class/powercap"
        zones = list(base.glob("*/energy_uj")) + list(base.glob("*/*/energy_uj"))
        return sorted({p.resolve().parent for p in zones
                       if read_text(p.parent / "name").startswith("package-")})

    def cpu_power(self):
        now = self.clock()
        samples = {}
        powers = []
        zones = self.rapl_zones()
        for zone in zones:
            energy = read_number(zone / "energy_uj")
            if energy is None or energy < 0:
                continue
            samples[zone] = (now, energy)
            old = self.energy_samples.get(zone)
            if old is None or now <= old[0]:
                continue
            delta = energy - old[1]
            if delta < 0:
                limit = read_number(zone / "max_energy_range_uj")
                if limit is None or limit <= max(energy, old[1]):
                    continue
                delta += limit
            powers.append(delta / 1e6 / (now - old[0]))
        self.energy_samples = samples
        # Never report a partial sum as total CPU package power.
        return sum(powers) if zones and len(powers) == len(zones) else None

    def cpu_stats(self, sensors, cpu_fan=None):
        out = {}
        temps = [s for s in sensors if s.kind == "Temperature" and s.chip in CPU_CHIPS]
        for label in ("Tdie", "Tctl", "CPU Package", "Package id 0"):
            preferred = [s.value for s in temps if s.label.casefold() == label.casefold()]
            if preferred:
                out["cpu_temp"] = max(preferred)
                break
        if "cpu_temp" not in out and temps:
            out["cpu_temp"] = max(s.value for s in temps)
        if "cpu_temp" not in out:
            for zone in sorted((self.root / "class/thermal").glob("thermal_zone*")):
                if read_text(zone / "type") in {"x86_pkg_temp", "cpu-thermal", "cpu_thermal"}:
                    value = read_number(zone / "temp", 1000)
                    if value is not None:
                        out["cpu_temp"] = value
                        break

        out["cpu_power"] = self.cpu_power()
        if out["cpu_power"] is None:
            powers = [s for s in sensors if s.kind == "Power" and s.chip in CPU_CHIPS]
            package = [s for s in powers if "package" in s.label.casefold()]
            # A rail called Core/SoC is not whole-package power.
            out["cpu_power"] = sum(s.value for s in package) if package else None

        fans = [s for s in sensors if s.kind == "Fan" and s.chip not in GPU_CHIPS]
        candidates = [s for s in fans if s.matches(cpu_fan)] if cpu_fan else [
            s for s in fans if "cpu" in s.label.casefold()]
        out["cpu_fan"] = candidates[0].value if candidates else None
        return out

    def amd_stats(self, card_name=None, gpu_fan=None):
        cards = sorted((self.root / "class/drm").glob("card*"))
        cards = [c for c in cards if re.fullmatch(r"card\d+", c.name)
                 and read_text(c / "device/vendor") == "0x1002"]
        if card_name:
            cards = [c for c in cards if c.name == card_name]
        # Prefer a discrete card (dedicated VRAM) over an APU.
        cards.sort(key=lambda c: read_number(c / "device/mem_info_vram_total") or 0,
                   reverse=True)
        if not cards:
            return {}
        device = cards[0] / "device"
        out = {"gpu_load": read_number(device / "gpu_busy_percent"),
               "vram_used": read_number(device / "mem_info_vram_used", 1024**3),
               "vram_total": read_number(device / "mem_info_vram_total", 1024**3)}
        sensors = []
        for hw in sorted((device / "hwmon").glob("hwmon*")):
            sensors.extend(self.hwmon(hw))
        for kind, key in (("Temperature", "gpu_temp"), ("Power", "gpu_power"), ("Fan", "gpu_fan")):
            candidates = [s for s in sensors if s.kind == kind]
            if kind == "Temperature":
                candidates.sort(key=lambda s: s.label.casefold() != "edge")
            if kind == "Fan" and gpu_fan:
                candidates = [s for s in candidates if s.matches(gpu_fan)]
            out[key] = candidates[0].value if candidates else None
        return out

    def describe(self):
        for sensor in self.hwmon():
            print(f"{sensor.kind:12} {sensor.chip + ':' + sensor.label:32} "
                  f"{sensor.value:10.2f}   {sensor.path}")
        for zone in self.rapl_zones():
            value = read_number(zone / "energy_uj")
            print(f"{'Energy (uJ)':12} {read_text(zone / 'name'):32} "
                  f"{value if value is not None else 'unreadable':>10}   {zone / 'energy_uj'}")
