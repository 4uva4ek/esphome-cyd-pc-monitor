from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux administrator setup')
class PowerSetupTests(unittest.TestCase):
    def test_only_package_counters_are_selected(self):
        from configure_cpu_power import package_counters
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for zone, name in (('intel-rapl:0', 'package-0'), ('intel-rapl:0:0', 'core'),
                               ('intel-rapl:0:1', 'dram')):
                path = root / zone
                path.mkdir()
                (path / 'name').write_text(name)
                (path / 'energy_uj').write_text('123')
            self.assertEqual(package_counters(root), [root / 'intel-rapl:0/energy_uj'])

    def test_rule_grants_only_group_read_on_package_energy(self):
        from configure_cpu_power import make_rule
        rule = make_rule(1000)
        self.assertIn('ATTR{name}=="package-*"', rule)
        self.assertIn('/usr/bin/chgrp 1000 /sys%p/energy_uj', rule)
        self.assertIn('/usr/bin/chmod g=r /sys%p/energy_uj', rule)
        self.assertNotIn('power_limit', rule)
        with self.assertRaises(ValueError):
            make_rule('1000; arbitrary command')
