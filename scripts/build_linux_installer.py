#!/usr/bin/env python3
"""Build a reproducible self-extracting Linux installer using only the stdlib."""

import argparse
import gzip
import hashlib
import io
from pathlib import Path
import re
import tarfile

ROOT = Path(__file__).resolve().parents[1]
FILES = ('install.sh', 'uninstall.sh', 'LICENSE', 'docs/linux.md', 'docs/linux.ru.md',
         'feeder/pc_stats_feeder.py', 'feeder/linux_sensors.py', 'feeder/requirements.txt',
         'scripts/configure_cpu_power.py')
HEADER = b'''#!/usr/bin/env bash
# CYD PC Stats Linux installer. Contains source code; pip downloads dependencies.
set -euo pipefail
archive_line=$(awk '/^__CYD_ARCHIVE_BELOW__$/ {print NR + 1; exit}' "$0")
extract_dir=$(mktemp -d)
trap 'rm -rf -- "$extract_dir"' EXIT
tail -n +"$archive_line" -- "$0" | tar -xz -C "$extract_dir"
"$extract_dir/install.sh" "$@"
exit 0
__CYD_ARCHIVE_BELOW__
'''


def build(output):
    version = re.search(r'^__version__ = "([^"]+)"',
                        (ROOT / 'feeder/pc_stats_feeder.py').read_text(), re.M).group(1)
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for name in sorted(FILES):
            data = (ROOT / name).read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith('.sh') else 0o644
            archive.addfile(info, io.BytesIO(data))
    output.mkdir(parents=True, exist_ok=True)
    target = output / f'cyd-pc-stats-{version}-linux.run'
    target.write_bytes(HEADER + gzip.compress(payload.getvalue(), mtime=0))
    target.chmod(0o755)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix('.run.sha256').write_text(f'{digest}  {target.name}\n')
    print(target)
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist')
    build(parser.parse_args().output_dir)
