import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from build_linux_installer import build, FILES, HEADER


class InstallerTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith('linux') and os.environ.get('CYD_INSTALLER_SMOKE'),
                         'opt in with CYD_INSTALLER_SMOKE=1; installs dependencies in a temp directory')
    def test_install_upgrade_service_quoting_and_uninstall(self):
        with tempfile.TemporaryDirectory(prefix='cyd smoke $% ') as directory:
            root = Path(directory)
            stub = root / 'bin'
            stub.mkdir()
            systemctl = stub / 'systemctl'
            systemctl.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$CYD_TEST_SYSTEMCTL_LOG"\n')
            systemctl.chmod(0o755)
            env = dict(os.environ, PATH=str(stub) + os.pathsep + os.environ['PATH'],
                       XDG_DATA_HOME=str(root / 'data $%'), XDG_CONFIG_HOME=str(root / 'config $%'),
                       CYD_TEST_SYSTEMCTL_LOG=str(root / 'systemctl.log'))
            installer = build(root)
            options = ['--port', '/dev/serial/by-id/usb-example', '--cpu-fan', 'CPU Fan $%']
            def run(command):
                result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout
            run(['bash', str(installer), *options])
            app = Path(env['XDG_DATA_HOME']) / 'cyd-pc-stats'
            unit = Path(env['XDG_CONFIG_HOME']) / 'systemd/user/cyd-pc-stats.service'
            self.assertTrue(unit.exists())
            self.assertIn('CPU Fan $$%%', unit.read_text())
            self.assertIn('--user restart cyd-pc-stats.service', (root / 'systemctl.log').read_text())
            self.assertIn('PCS,', run([str(app / 'run.sh'), '--dry-run', '--once', '--interval', '0.01']))
            run(['bash', str(installer), '--no-autostart', *options])
            self.assertIn('--user disable cyd-pc-stats.service', (root / 'systemctl.log').read_text())
            run(['bash', str(app / 'uninstall.sh')])
            self.assertFalse(unit.exists())
            self.assertFalse(app.exists())

    def test_package_is_reproducible_complete_and_checksummed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            first = build(output).read_bytes()
            target = build(output)
            self.assertEqual(first, target.read_bytes())
            expected = hashlib.sha256(first).hexdigest()
            self.assertEqual(target.with_suffix('.run.sha256').read_text(), f'{expected}  {target.name}\n')
            with tarfile.open(fileobj=io.BytesIO(first[len(HEADER):]), mode='r:gz') as archive:
                self.assertEqual(set(archive.getnames()), set(FILES))
                for name in FILES:
                    self.assertEqual(archive.extractfile(name).read(), (ROOT / name).read_bytes())
                self.assertEqual(archive.getmember('install.sh').mode, 0o755)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux installer')
    def test_self_extracting_help_and_shell_syntax(self):
        with tempfile.TemporaryDirectory(prefix='cyd package ') as directory:
            target = build(Path(directory))
            result = subprocess.run(['bash', str(target), '--help'], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--no-autostart', result.stdout)
            subprocess.run(['bash', '-n', str(ROOT / 'install.sh'), str(ROOT / 'uninstall.sh')], check=True)


if __name__ == '__main__':
    unittest.main()
