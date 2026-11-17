#!/usr/bin/env python3
"""Opt in to CPU package energy access for one existing Linux group.

Run with --dry-run to inspect the exact udev rule and files first. Applying this
requires administrator authentication. The feeder itself keeps running as you.
"""

import argparse
import grp
import os
from pathlib import Path
import shutil
import stat
import subprocess

RULE_PATH = Path('/etc/udev/rules.d/70-cyd-cpu-power.rules')
MARKER = '# CYD PC Stats: read-only CPU package energy access\n'


def package_counters(root=Path('/sys/class/powercap')):
    paths = list(root.glob('*/energy_uj')) + list(root.glob('*/*/energy_uj'))
    result = set()
    for path in paths:
        try:
            if (path.parent / 'name').read_text().strip().startswith('package-'):
                result.add(path.resolve())
        except OSError:
            continue
    return sorted(result)


def make_rule(gid):
    if not isinstance(gid, int) or gid < 0:
        raise ValueError('Expected a numeric group ID')
    return MARKER + (
        'ACTION=="add", SUBSYSTEM=="powercap", ATTR{name}=="package-*", '
        f'RUN+="/usr/bin/chgrp {gid} /sys%p/energy_uj", '
        'RUN+="/usr/bin/chmod g=r /sys%p/energy_uj"\n'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--group', required=True, help='existing group allowed to read package energy')
    parser.add_argument('--dry-run', action='store_true', help='show the changes without applying them')
    parser.add_argument('--remove', action='store_true', help='remove this group rule and restore root-only energy access')
    args = parser.parse_args()
    try:
        gid = grp.getgrnam(args.group).gr_gid
    except KeyError:
        parser.error(f'Group does not exist: {args.group}')
    counters = package_counters()
    if not counters and not args.remove:
        parser.error('No CPU package energy counters found under /sys/class/powercap')
    rule = make_rule(gid)
    print(('Remove access for' if args.remove else 'Allow read access for') + f' group {args.group} (GID {gid}):')
    for path in counters:
        print(path)
    print(f'Persistent rule: {RULE_PATH}\n{rule}', end='')
    if args.dry_run:
        return
    if os.geteuid() != 0:
        parser.error('Administrator rights required; use sudo or pkexec with --group')
    udevadm = shutil.which('udevadm')
    if not udevadm:
        parser.error('udevadm is required for persistent permissions')
    if args.remove:
        if not RULE_PATH.exists():
            print('No managed rule to remove.')
            return
        if RULE_PATH.read_text() != rule:
            parser.error('Existing rule differs; refusing to remove another configuration')
        RULE_PATH.unlink()
        subprocess.run([udevadm, 'control', '--reload-rules'], check=True)
        for path in counters:
            info = path.stat()
            if info.st_gid == gid:
                os.chown(path, -1, 0)
                path.chmod(stat.S_IMODE(info.st_mode) & ~0o070)
        print('Removed rule and restored root-only group permissions.')
        return
    if RULE_PATH.exists() and not RULE_PATH.read_text().startswith(MARKER):
        parser.error(f'Refusing to replace an unrelated file: {RULE_PATH}')
    RULE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RULE_PATH.write_text(rule)
    RULE_PATH.chmod(0o644)
    subprocess.run([udevadm, 'control', '--reload-rules'], check=True)
    # Apply only to package energy counters; never touch power limits or core zones.
    for path in counters:
        mode = stat.S_IMODE(path.stat().st_mode)
        os.chown(path, -1, gid)
        path.chmod((mode & ~0o070) | 0o040)
    print('CPU package energy is readable now and after device creation/reboot.')


if __name__ == '__main__':
    main()
