import argparse
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'feeder'))
import pc_stats_feeder as feeder
from linux_sensors import LinuxSensors, read_number


class LinuxSensorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 10.0
        self.reader = LinuxSensors(self.root, lambda: self.now)

    def write(self, path, value):
        dest = self.root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(str(value))
        return dest

    def hw(self, index, name, **values):
        base = f'class/hwmon/hwmon{index}'
        self.write(f'{base}/name', name)
        for key, value in values.items():
            self.write(f'{base}/{key}', value)

    def cpu(self, fan=None):
        return self.reader.cpu_stats(self.reader.hwmon(), fan)

    def test_ryzen_prefers_die_and_does_not_use_nvme(self):
        self.hw(0, 'nvme', temp1_input=99000)
        self.hw(7, 'k10temp', temp1_label='Tctl', temp1_input=65000,
                temp2_label='Tdie', temp2_input=55000)
        self.assertEqual(self.cpu()['cpu_temp'], 55)

    def test_intel_package_preferred_over_core(self):
        self.hw(0, 'coretemp', temp1_label='Package id 0', temp1_input=50000,
                temp2_label='Core 0', temp2_input=61000)
        self.assertEqual(self.cpu()['cpu_temp'], 50)

    def test_missing_cpu_is_unknown_not_disk_temperature(self):
        self.hw(0, 'nvme', temp1_input=99000)
        self.assertNotIn('cpu_temp', self.cpu())

    def test_thermal_fallback(self):
        self.write('class/thermal/thermal_zone0/type', 'cpu-thermal')
        self.write('class/thermal/thermal_zone0/temp', 42000)
        self.assertEqual(self.cpu()['cpu_temp'], 42)

    def test_explicit_fan_selector_and_no_chassis_guess(self):
        self.hw(0, 'nct6799', fan1_input=1200, fan2_input=0)
        self.hw(1, 'amdgpu', fan1_input=2300, fan1_label='CPU fan')
        self.assertIsNone(self.cpu()['cpu_fan'])
        self.assertEqual(self.cpu('nct6799:fan1')['cpu_fan'], 1200)
        self.assertEqual(self.cpu('nct6799:fan2')['cpu_fan'], 0)
        self.assertEqual(self.cpu(str(self.root / 'class/hwmon/hwmon0/fan1_input'))['cpu_fan'], 1200)
        self.assertIsNone(self.cpu('missing')['cpu_fan'])

    def test_named_cpu_fan_auto(self):
        self.hw(0, 'nct6799', fan1_label='CPU Fan', fan1_input=1500)
        self.assertEqual(self.cpu()['cpu_fan'], 1500)

    def test_malformed_or_disappearing_sensor_is_unknown(self):
        for bad in ('no value', 'nan', 'inf', ''):
            path = self.write('sensor', bad)
            self.assertIsNone(read_number(path))
        self.assertIsNone(read_number(self.root / 'missing'))
        with mock.patch.object(Path, 'read_text', side_effect=PermissionError):
            self.assertIsNone(read_number(path))

    def rapl(self, energy, zone='intel-rapl:0', name='package-0'):
        # The reader identifies packages by their name file, not directory name.
        # Windows reserves ':' for drive letters / alternate data streams.
        if sys.platform == 'win32':
            zone = zone.replace(':', '_')
        base = f'class/powercap/{zone}'
        self.write(f'{base}/name', name)
        self.write(f'{base}/energy_uj', energy)
        self.write(f'{base}/max_energy_range_uj', 1000000000)

    def test_rapl_delta_and_ignores_core_subdomain(self):
        self.rapl(100000000)
        self.rapl(900000000, 'intel-rapl:0:0', 'core')
        self.assertIsNone(self.reader.cpu_power())
        self.now += 2
        self.rapl(220000000)
        self.assertEqual(self.reader.cpu_power(), 60)

    def test_rapl_wraparound(self):
        self.rapl(980000000)
        self.reader.cpu_power()
        self.now += 1
        self.rapl(30000000)
        self.assertEqual(self.reader.cpu_power(), 50)

    def test_rapl_multiple_packages_and_permission_loss(self):
        self.rapl(0)
        self.rapl(0, 'intel-rapl:1', 'package-1')
        self.reader.cpu_power()
        self.now += 1
        self.rapl(40000000)
        self.rapl(60000000, 'intel-rapl:1', 'package-1')
        self.assertEqual(self.reader.cpu_power(), 100)
        self.now += 1
        self.rapl('unreadable')
        self.assertIsNone(self.reader.cpu_power())
        self.now += 1
        self.rapl(80000000)
        self.assertIsNone(self.reader.cpu_power())  # re-prime, don't bridge the gap

    @unittest.skipIf(sys.platform == 'win32', 'sysfs symlinks are Linux-specific')
    def test_rapl_class_alias_is_not_double_counted(self):
        self.rapl(0)
        control = self.root / 'class/powercap/intel-rapl'
        control.mkdir()
        (control / 'intel-rapl:0').symlink_to(control.parent / 'intel-rapl:0', target_is_directory=True)
        self.reader.cpu_power()
        self.now += 1
        self.rapl(30000000)
        self.assertEqual(self.reader.cpu_power(), 30)

    def test_hwmon_power_average_and_units(self):
        self.hw(0, 'zenpower', power1_label='CPU Package', power1_average=75000000,
                power1_input=80000000, power2_label='SoC', power2_input=10000000)
        self.assertEqual(self.cpu()['cpu_power'], 75)

    def amd(self, card, vram):
        base = f'class/drm/{card}/device'
        for name, value in {'vendor': '0x1002', 'gpu_busy_percent': 23,
                            'mem_info_vram_used': 2 * 1024**3,
                            'mem_info_vram_total': vram * 1024**3,
                            'hwmon/hwmon1/name': 'amdgpu',
                            'hwmon/hwmon1/temp1_input': 51000,
                            'hwmon/hwmon1/temp1_label': 'edge',
                            'hwmon/hwmon1/temp2_input': 67000,
                            'hwmon/hwmon1/power1_average': 125000000,
                            'hwmon/hwmon1/fan1_input': 1100}.items():
            self.write(f'{base}/{name}', value)

    def test_amd_discrete_preferred_and_units(self):
        self.amd('card0', 1)
        self.amd('card2', 16)
        stats = self.reader.amd_stats()
        self.assertEqual(stats, dict(gpu_load=23, gpu_temp=51, gpu_power=125,
                                    vram_used=2, vram_total=16, gpu_fan=1100))
        self.assertEqual(self.reader.amd_stats('card0')['vram_total'], 1)
        self.assertEqual(self.reader.amd_stats('card9'), {})
        self.assertIsNone(self.reader.amd_stats(gpu_fan='missing')['gpu_fan'])


class FeederTests(unittest.TestCase):
    def args(self, **kwargs):
        return argparse.Namespace(cpu_fan=None, gpu_fan=None, nvidia_gpu=kwargs.get('nvidia_gpu'),
                                  drm_card=kwargs.get('drm_card'))

    def test_wire_format_exactly_twelve_fields(self):
        line = feeder.format_line(dict(cpu_temp=45, gpu_load=0, cpu_power=float('nan'),
                                       gpu_power=float('inf'), cpu_fan=1234))
        self.assertEqual(line, 'PCS,45.0,,,,0.0,,,,,,1234.0,\n')
        self.assertEqual(len(line.strip().split(',')), 13)
        self.assertTrue(line.isascii())

    @mock.patch.object(feeder.shutil, 'which', return_value='/usr/bin/nvidia-smi')
    @mock.patch.object(feeder.subprocess, 'run')
    def test_nvidia_units_na_and_selection(self, run, which):
        run.return_value = subprocess.CompletedProcess([], 0, '41.25, 0, [N/A], 1024, 32768\n')
        stats = feeder.stats_from_nvidia_smi('GPU-example')
        self.assertEqual(stats['gpu_temp'], 41.25)
        self.assertEqual(stats['vram_used'], 1)
        self.assertEqual(stats['vram_total'], 32)
        self.assertIsNone(stats['gpu_power'])
        self.assertIn('--id=GPU-example', run.call_args.args[0])
        run.return_value = subprocess.CompletedProcess([], 9, 'NVIDIA driver failed')
        self.assertEqual(feeder.stats_from_nvidia_smi(), {})
        run.side_effect = subprocess.TimeoutExpired('nvidia-smi', 2)
        self.assertEqual(feeder.stats_from_nvidia_smi(), {})

    @mock.patch.object(feeder, 'read_lhm', side_effect=AssertionError('Linux must not call LHM'))
    @mock.patch.object(feeder, 'stats_from_nvidia_smi', return_value={'gpu_temp': 45, 'gpu_power': None})
    def test_linux_keeps_one_gpu_and_skips_windows_backend(self, nvidia, lhm):
        linux = mock.Mock()
        linux.cpu_stats.return_value = {'cpu_temp': 50}
        stats, _ = feeder.collect(self.args(), linux)
        linux.amd_stats.assert_not_called()
        self.assertEqual(stats['gpu_temp'], 45)
        self.assertIsNone(stats['gpu_power'])

    @mock.patch.object(feeder, 'stats_from_nvidia_smi', return_value={})
    def test_amd_fallback_and_explicit_gpu(self, nvidia):
        linux = mock.Mock()
        linux.cpu_stats.return_value = {}
        linux.amd_stats.return_value = {'gpu_temp': 51}
        self.assertEqual(feeder.collect(self.args(), linux)[0]['gpu_temp'], 51)
        linux.amd_stats.reset_mock()
        feeder.collect(self.args(nvidia_gpu='GPU-missing'), linux)
        linux.amd_stats.assert_not_called()
        nvidia.reset_mock()
        feeder.collect(self.args(drm_card='card2'), linux)
        nvidia.assert_not_called()
        linux.amd_stats.assert_called_once_with('card2', None)

    @mock.patch.object(feeder, 'read_lhm', return_value=[('/intelcpu/0/temperature/0', 'Temperature', 'CPU Package', 55)])
    @mock.patch.object(feeder, 'stats_from_nvidia_smi', return_value={})
    def test_windows_backend_preserved(self, nvidia, lhm):
        stats, online = feeder.collect(self.args())
        self.assertTrue(online)
        self.assertEqual(stats['cpu_temp'], 55)
        self.assertIn('ram_total', stats)

    @mock.patch.object(feeder.list_ports, 'comports')
    def test_port_discovery_does_not_guess_between_devices(self, ports):
        ports.return_value = [argparse.Namespace(vid=0x1A86, device='/dev/ttyUSB0')]
        self.assertEqual(feeder.find_port(), '/dev/ttyUSB0')
        ports.return_value.append(argparse.Namespace(vid=0x10C4, device='/dev/ttyUSB1'))
        with self.assertRaises(feeder.serial.SerialException):
            feeder.find_port()
        ports.return_value = []
        self.assertIsNone(feeder.find_port())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux serial configuration')
    @mock.patch.object(feeder.serial, 'Serial')
    def test_linux_serial_does_not_reset_board(self, serial):
        port = feeder.open_port('/dev/ttyUSB0')
        self.assertFalse(port.dtr)
        self.assertFalse(port.rts)
        self.assertTrue(port.exclusive)
        self.assertEqual(port.baudrate, 115200)
        port.open.assert_called_once()

    def test_invalid_interval_rejected(self):
        for value in ('0', '-1', 'nan', 'inf'):
            with mock.patch.object(sys, 'argv', ['feeder', '--interval', value]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    feeder.main()
                self.assertEqual(error.exception.code, 2)

    def test_windows_import_does_not_need_linux_module(self):
        spec = importlib.util.spec_from_file_location('windows_feeder', feeder.__file__)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, 'platform', 'win32'), mock.patch.dict(sys.modules, {'linux_sensors': None}):
            spec.loader.exec_module(module)
        self.assertEqual(module.__version__, '1.2.0')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'POSIX pseudo-terminal integration')
    def test_real_serial_packet_over_pty(self):
        import pty
        import select
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        # PTYs do not implement modem-control ioctls; real serial transport is used.
        with mock.patch.object(feeder.serial.Serial, '_update_dtr_state'), \
             mock.patch.object(feeder.serial.Serial, '_update_rts_state'):
            port = feeder.open_port(os.ttyname(slave))
        self.addCleanup(port.close)
        line = feeder.format_line({'cpu_temp': 45, 'gpu_temp': 42, 'gpu_power': 50})
        port.write(line.encode('ascii'))
        self.assertTrue(select.select([master], [], [], 2)[0])
        self.assertEqual(os.read(master, 4096).decode('ascii'), line)


if __name__ == '__main__':
    unittest.main()
