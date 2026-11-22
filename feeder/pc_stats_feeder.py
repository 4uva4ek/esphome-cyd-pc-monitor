"""Send PC temps and usage to the CYD over its USB serial port.

Sources:
  * Linux: hwmon (CPU temperature/fans), powercap (CPU package power),
    nvidia-smi (NVIDIA GPU) or DRM/hwmon (AMD GPU).
  * LibreHardwareMonitor's web server (Options > Remote Web Server > Run, port 8085):
    CPU temp/power, GPU stats, fan RPM.
  * psutil: CPU load and RAM (always available).
  * nvidia-smi: GPU stats if LibreHardwareMonitor isn't running.

Unavailable sensors are sent as empty fields, displayed as -- on the CYD.

Usage:
  python pc_stats_feeder.py                 # auto-detect the CYD's COM port
  python pc_stats_feeder.py --port COM5
  python pc_stats_feeder.py --port /dev/serial/by-id/usb-...
  python pc_stats_feeder.py --list-sensors  # print platform sensor names
  python pc_stats_feeder.py --dry-run       # print what would be sent
  python pc_stats_feeder.py --dry-run --once
"""

import argparse
import ctypes
import json
import logging
import logging.handlers
import math
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request

import psutil
import serial
from serial.tools import list_ports

try:
    import pynvml
except ImportError:
    pynvml = None

if sys.platform.startswith("linux"):
    from linux_sensors import LinuxSensors

__version__ = "1.3.0"

log = logging.getLogger("cyd-pc-stats")

LHM_URL = "http://localhost:8085/data.json"
# USB-serial chips used on CYD boards: WCH CH340/CH9102, Silicon Labs CP210x.
CYD_USB_VIDS = {0x1A86, 0x10C4}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- LibreHardwareMonitor

def _parse_value(text):
    """'55.4 Â°C' -> 55.4, '6,144 MB' -> 6144.0 (also handles comma decimals)."""
    m = re.search(r"-?\d[\d.,]*", text or "")
    if not m:
        return None
    num = m.group(0)
    if "," in num and "." in num:
        num = num.replace(",", "")
    else:
        num = num.replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


def read_lhm():
    """Return a flat list of (sensor_id, type, name, value) from LibreHardwareMonitor, or None."""
    try:
        with urllib.request.urlopen(LHM_URL, timeout=0.8) as resp:
            root = json.load(resp)
    except (OSError, ValueError):
        return None

    sensors = []

    def walk(node):
        sid = node.get("SensorId")
        if sid:
            sensors.append((sid, node.get("Type", ""), node.get("Text", ""), _parse_value(node.get("Value"))))
        for child in node.get("Children", []):
            walk(child)

    walk(root)
    return sensors


def _pick(sensors, prefixes, stype, names=(), contains=None):
    """First sensor matching hardware prefix + type, preferring names in order."""
    cands = [s for s in sensors if s[0].startswith(prefixes) and s[1] == stype and s[3] is not None]
    for name in names:
        for s in cands:
            if s[2].lower() == name.lower():
                return s[3]
    if contains:
        for s in cands:
            if contains.lower() in s[2].lower():
                return s[3]
    return None


CPU_HW = ("/amdcpu/", "/intelcpu/")


def gpu_prefix(sensors):
    # Prefer the discrete GPU; the Ryzen's integrated Radeon also shows up as /gpu-amd/.
    for prefix in ("/gpu-nvidia/", "/gpu-amd/", "/gpu-intel/"):
        if any(s[0].startswith(prefix) for s in sensors):
            return prefix
    return None


def stats_from_lhm(sensors, cpu_fan_name, gpu_fan_name):
    out = {}
    out["cpu_temp"] = _pick(sensors, CPU_HW, "Temperature",
                            ("Core (Tctl/Tdie)", "CPU Package", "Core (Tctl)", "Core (Tdie)", "Package"))
    out["cpu_load"] = _pick(sensors, CPU_HW, "Load", ("CPU Total",))
    out["cpu_power"] = _pick(sensors, CPU_HW, "Power", ("Package", "CPU Package"))

    gp = gpu_prefix(sensors)
    if gp:
        out["gpu_temp"] = _pick(sensors, gp, "Temperature", ("GPU Core",))
        out["gpu_load"] = _pick(sensors, gp, "Load", ("GPU Core",))
        out["gpu_power"] = _pick(sensors, gp, "Power", ("GPU Package", "GPU Power", "GPU Core"))
        used = _pick(sensors, gp, "SmallData", ("GPU Memory Used", "D3D Dedicated Memory Used"))
        total = _pick(sensors, gp, "SmallData", ("GPU Memory Total",))
        out["vram_used"] = used / 1024 if used is not None else None  # MB -> GB
        out["vram_total"] = total / 1024 if total is not None else None
        out["gpu_fan"] = _pick(sensors, gp, "Fan", (gpu_fan_name,) if gpu_fan_name else (), contains="fan")

    # Motherboard fan headers live under /lpc/ (Super I/O chip).
    fans = [s for s in sensors if s[0].startswith("/lpc/") and s[1] == "Fan" and s[3] is not None]
    if cpu_fan_name:
        out["cpu_fan"] = next((s[3] for s in fans if s[2].lower() == cpu_fan_name.lower()), None)
    else:
        named = [s for s in fans if "cpu" in s[2].lower()]
        spinning = [s for s in fans if s[3] > 0]
        out["cpu_fan"] = (named or spinning or [(None, None, None, None)])[0][3]
    return out


# ---------------------------------------------------------------- OS

def os_label():
    """Human-readable OS label, e.g. 'Windows 11', 'macOS 15', 'Debian 12'."""
    sysname = platform.system()
    if sysname == "Windows":
        try:
            getver = getattr(sys, "getwindowsversion", None)
            v = getver() if getver else None
            if v and v.build >= 22000:
                return "Windows 11"
            if v and v.build >= 10240:
                return "Windows 10"
            if v:
                return f"Windows {v.major}.{v.minor}"
            return "Windows"
        except Exception:
            return "Windows"
    if sysname == "Darwin":
        ver = platform.mac_ver()[0]
        return f"macOS {ver.split('.')[0]}" if ver else "macOS"
    if sysname == "Linux":
        try:
            for line in open("/etc/os-release", encoding="utf-8"):
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        return "Linux"
    return sysname or "Unknown"


# ---------------------------------------------------------------- network

class NetSpeed:
    """Tracks aggregate receive throughput (MB/s) over successive calls."""

    def __init__(self):
        self.last_t = time.monotonic()
        self.last_rx = sum(i.bytes_recv for i in psutil.net_io_counters(pernic=True).values())

    def update(self):
        now = time.monotonic()
        rx = sum(i.bytes_recv for i in psutil.net_io_counters(pernic=True).values())
        dt = now - self.last_t
        self.last_t = now
        self.last_rx = rx
        if dt <= 0:
            return None
        return max(0.0, (rx - self.last_rx) / dt / 1e6)  # MB/s


# ---------------------------------------------------------------- fallbacks

class NvmlFanSpeedInfo(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint), ("fan", ctypes.c_uint), ("speed", ctypes.c_uint)]


def _read_nvml_fans(device):
    # nvidia-ml-py's RPM wrapper currently hardcodes fan 0. Use the documented
    # versioned C API to read every fan without relying on binding internals.
    library = ctypes.CDLL("nvml.dll" if sys.platform == "win32" else "libnvidia-ml.so.1")
    query = library.nvmlDeviceGetFanSpeedRPM
    query.argtypes = [ctypes.c_void_p, ctypes.POINTER(NvmlFanSpeedInfo)]
    query.restype = ctypes.c_int
    readings = {}
    for fan in range(pynvml.nvmlDeviceGetNumFans(device)):
        info = NvmlFanSpeedInfo(ctypes.sizeof(NvmlFanSpeedInfo) | (1 << 24), fan, 0)
        if query(ctypes.cast(device, ctypes.c_void_p), ctypes.byref(info)) == 0:
            readings[fan] = info.speed
    return readings


def nvidia_fan_rpms(gpu=None):
    """Return supported NVML RPM channels, never percentages or estimates."""
    if pynvml is None:
        return {}
    initialized = False
    try:
        pynvml.nvmlInit()
        initialized = True
        selector = gpu or "0"
        if selector.isdecimal():
            device = pynvml.nvmlDeviceGetHandleByIndex(int(selector))
        elif selector.startswith("GPU-"):
            device = pynvml.nvmlDeviceGetHandleByUUID(selector)
        else:
            device = pynvml.nvmlDeviceGetHandleByPciBusId(selector)
        return _read_nvml_fans(device)
    except (pynvml.NVMLError, AttributeError, OSError, ValueError):
        return {}
    finally:
        if initialized:
            try:
                pynvml.nvmlShutdown()
            except pynvml.NVMLError:
                pass


def nvidia_fan_rpm(gpu=None):
    # The display has one GPU fan field: show the fastest available fan.
    return max(nvidia_fan_rpms(gpu).values(), default=None)


def stats_from_nvidia_smi(gpu=None):
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {}
    try:
        res = subprocess.run(
            [exe, "--query-gpu=temperature.gpu,utilization.gpu,power.draw,memory.used,memory.total",
             "--format=csv,noheader,nounits", "--id=" + (gpu or "0")],
            capture_output=True, text=True, timeout=2, creationflags=NO_WINDOW)
        if res.returncode != 0:
            return {}
        vals = [_parse_value(v) for v in res.stdout.splitlines()[0].split(",")]
    except (OSError, subprocess.SubprocessError, IndexError):
        return {}
    if len(vals) != 5:
        return {}
    t, load, power, used, total = vals
    return {"gpu_temp": t, "gpu_load": load, "gpu_power": power,
            "vram_used": used / 1024 if used is not None else None,
            "vram_total": total / 1024 if total is not None else None,
            "gpu_fan": nvidia_fan_rpm(gpu)}


def collect(args, linux=None, net_monitor=None):
    stats = {}
    if linux is not None:
        sensors = linux.hwmon()
        stats.update(linux.cpu_stats(sensors, args.cpu_fan))
        gpu = {} if args.drm_card else stats_from_nvidia_smi(args.nvidia_gpu)
        # Select one GPU backend; never mix readings from two different cards.
        if not any(v is not None for v in gpu.values()) and not args.nvidia_gpu:
            gpu = linux.amd_stats(args.drm_card, args.gpu_fan)
        stats.update(gpu)
    else:
        sensors = read_lhm()
        if sensors:
            stats.update({k: v for k, v in stats_from_lhm(sensors, args.cpu_fan, args.gpu_fan).items() if v is not None})
        if any(k not in stats for k in ("gpu_temp", "gpu_load", "vram_used")):
            for k, v in stats_from_nvidia_smi(args.nvidia_gpu).items():
                if v is not None:
                    stats.setdefault(k, v)
    stats.setdefault("cpu_load", psutil.cpu_percent(interval=None))
    mem = psutil.virtual_memory()
    stats["ram_used"] = (mem.total - mem.available) / 1024**3
    stats["ram_total"] = mem.total / 1024**3
    net = net_monitor.update() if net_monitor else None
    if net is not None:
        stats["net_mb"] = net
    return stats, sensors is not None


FIELDS = ("cpu_temp", "cpu_load", "cpu_power", "gpu_temp", "gpu_load", "gpu_power",
          "ram_used", "ram_total", "vram_used", "vram_total", "cpu_fan", "gpu_fan", "net_mb")


def format_line(stats, os_str):
    def fmt(v):
        return "" if v is None or not math.isfinite(v) else f"{v:.1f}"
    os_str = (os_str or "").replace(",", " ").strip()
    os_str = os_str.encode("ascii", "ignore").decode("ascii").strip()
    return "PCS," + ",".join(fmt(stats.get(f)) for f in FIELDS) + "," + os_str + "\n"


# ---------------------------------------------------------------- serial

def find_port():
    ports = [p for p in list_ports.comports() if p.vid in CYD_USB_VIDS]
    if len(ports) > 1:
        raise serial.SerialException("Multiple USB serial devices found; select the CYD with --port: "
                                     + ", ".join(p.device for p in ports))
    return ports[0].device if ports else None


def open_port(name):
    ser = serial.Serial()
    ser.port = name
    ser.baudrate = 115200
    ser.write_timeout = 1
    if sys.platform.startswith("linux"):
        ser.exclusive = True
    # Keep DTR/RTS low so opening the port doesn't reset the ESP32 or put it in bootloader mode.
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def setup_logging(log_file, verbose):
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    if sys.stdout is not None:  # None under pythonw.exe
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        log.addHandler(console)
    if log_file:
        handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=512_000, backupCount=1, encoding="utf-8")
        handler.setFormatter(fmt)
        log.addHandler(handler)


class StatusLog:
    """Log a status message only when it changes, so retry loops don't flood the log."""

    def __init__(self):
        self.last = None

    def __call__(self, msg):
        if msg != self.last:
            log.info(msg)
            self.last = msg


class LhmWatchdog:
    """Restart LibreHardwareMonitor through its startup task if it has exited.

    LibreHardwareMonitor occasionally crashes on its own. The installer registers a
    "LibreHardwareMonitor" task that runs it elevated; a normal user can trigger that task,
    so it comes back as admin without a UAC prompt.
    """

    TASK = "LibreHardwareMonitor"
    GRACE = 20       # seconds unreachable before acting
    RETRY = 120      # seconds between restart attempts

    def __init__(self):
        self.down_since = None
        self.last_attempt = -self.RETRY

    def update(self, lhm_up):
        now = time.monotonic()
        if lhm_up:
            self.down_since = None
            return
        if self.down_since is None:
            self.down_since = now
        if now - self.down_since < self.GRACE or now - self.last_attempt < self.RETRY:
            return
        self.last_attempt = now
        if any(p.info["name"] == "LibreHardwareMonitor.exe" for p in psutil.process_iter(["name"])):
            # Running but not answering: starting up, or its web server was turned off.
            # Don't fight it; restarting wouldn't change its settings.
            log.info("LibreHardwareMonitor is running but its web server isn't answering on port 8085")
            return
        try:
            res = subprocess.run(["schtasks", "/run", "/tn", self.TASK], capture_output=True, text=True,
                                 timeout=15, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as e:
            log.info(f"LibreHardwareMonitor isn't running; couldn't start its task: {e}")
            return
        if res.returncode == 0:
            log.info("LibreHardwareMonitor isn't running; restarted it through its startup task")
        else:
            log.info("LibreHardwareMonitor isn't running and its startup task couldn't be started "
                     f"({(res.stderr or res.stdout).strip()}). Run install.ps1 again to set it up.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port, e.g. COM5 or /dev/serial/by-id/... (default: auto-detect)")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between updates (default 1)")
    ap.add_argument("--cpu-fan", help="CPU fan name, Linux chip:label, chip:fanN or sysfs path")
    ap.add_argument("--gpu-fan", help="GPU fan name or Linux hwmon selector (AMD only on Linux)")
    gpu = ap.add_mutually_exclusive_group()
    gpu.add_argument("--nvidia-gpu", help="NVIDIA index, UUID or PCI ID (default: first NVIDIA GPU)")
    gpu.add_argument("--drm-card", help="Linux AMD DRM card, e.g. card1; bypasses NVIDIA detection")
    ap.add_argument("--list-sensors", action="store_true", help="print platform sensors and exit")
    ap.add_argument("--dry-run", action="store_true", help="print lines instead of sending them")
    ap.add_argument("--once", action="store_true", help="with --dry-run, print one sample and exit")
    ap.add_argument("--check-config", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--log-file", help="also write status messages to this file (rotates at 500 KB)")
    ap.add_argument("--no-restart-lhm", action="store_true",
                    help="don't restart LibreHardwareMonitor through its startup task if it exits")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every line sent")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args()
    if not math.isfinite(args.interval) or args.interval <= 0:
        ap.error("--interval must be a finite number greater than zero")
    if args.once and not args.dry_run:
        ap.error("--once requires --dry-run")
    is_linux = sys.platform.startswith("linux")
    if args.drm_card and (not is_linux or not re.fullmatch(r"card\d+", args.drm_card)):
        ap.error("--drm-card requires Linux and a card name such as card1")
    if args.check_config:
        return
    linux = LinuxSensors() if is_linux else None
    setup_logging(args.log_file, args.verbose)

    if args.list_sensors:
        if linux is not None:
            linux.describe()
            gpu_stats = (linux.amd_stats(args.drm_card, args.gpu_fan) if args.drm_card else
                         stats_from_nvidia_smi(args.nvidia_gpu))
            if not any(v is not None for v in gpu_stats.values()) and not args.nvidia_gpu:
                gpu_stats = linux.amd_stats(args.drm_card, args.gpu_fan)
            print("GPU:", json.dumps(gpu_stats, sort_keys=True))
            if not args.drm_card:
                print("NVIDIA fans (RPM):", json.dumps(nvidia_fan_rpms(args.nvidia_gpu), sort_keys=True))
            return
        sensors = read_lhm()
        if sensors is None:
            sys.exit(f"LibreHardwareMonitor web server not reachable at {LHM_URL}")
        for sid, stype, name, value in sensors:
            print(f"{stype:12} {name:32} {value!s:>10}   {sid}")
        return

    log.info(f"CYD PC Stats feeder {__version__}")
    psutil.cpu_percent(interval=None)  # prime the counter; the first reading is always 0
    net_monitor = NetSpeed()  # tracks receive throughput; also useful in --dry-run
    lhm_status = StatusLog()
    serial_status = StatusLog()
    watchdog = LhmWatchdog() if sys.platform == "win32" and not (args.no_restart_lhm or args.dry_run) else None
    if linux is not None:
        log.info("Linux sensors: hwmon / powercap + NVIDIA / AMD; unavailable values show --")
    if args.once:
        collect(args, linux)  # prime RAPL energy and CPU load for a meaningful one-shot sample
        time.sleep(args.interval)
    ser = None
    os_text = os_label()
    while True:
        started = time.monotonic()
        stats, lhm_up = collect(args, linux, net_monitor)
        if linux is None:
            lhm_status("LibreHardwareMonitor: " + ("connected" if lhm_up else
                       f"not reachable at {LHM_URL} (no CPU temp/power or fan RPM)"))
        if watchdog:
            watchdog.update(lhm_up)
        line = format_line(stats, os_text)

        if args.dry_run:
            print(line, end="", flush=True)
            if args.once:
                return
        else:
            try:
                if ser is None:
                    port = args.port or find_port()
                    if not port:
                        raise serial.SerialException("CYD not found (no CH340/CP210x serial port)")
                    ser = open_port(port)
                    serial_status(f"Sending to {port}")
                ser.write(line.encode("ascii"))
                log.debug(line.rstrip())
            except serial.SerialException as e:
                serial_status(f"Serial: {e}; retrying every 3 s")
                if ser is not None:
                    ser.close()
                    ser = None
                time.sleep(3)
                continue

        time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
