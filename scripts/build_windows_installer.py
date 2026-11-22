#!/usr/bin/env python3
"""Build a self-contained Windows installer (.ps1) from repo files.

Produces a single .ps1 file that embeds the feeder sources (base64) and runs the
same install.ps1 logic locally — no network access required to run it (the only
expected network use is winget installing LibreHardwareMonitor).
"""

import argparse
import base64
import hashlib
import io
import re
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Files embedded into the installer, with their in-zip layout.
PAYLOAD = {
    "install.ps1": "install.ps1",
    "uninstall.ps1": "uninstall.ps1",
    "feeder/pc_stats_feeder.py": "feeder/pc_stats_feeder.py",
    "feeder/requirements.txt": "feeder/requirements.txt",
    "LICENSE": "LICENSE",
}

HEADER = '''# CYD PC Stats - self-contained Windows installer.
# Embedded feeder sources below. Run with:
#   powershell -ExecutionPolicy Bypass -File <this file>
# Options: -Port COM5, -NoAutostart, -SkipLibreHardwareMonitor
param(
    [string]$Port,
    [switch]$NoAutostart,
    [switch]$SkipLibreHardwareMonitor
)

$ErrorActionPreference = 'Stop'
$tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("cyd-pc-stats-installer-" + [Guid]::NewGuid().ToString('N'))
$payloadMarker = '__CYD_PAYLOAD_BELOW__'

New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
$lines = Get-Content -Path $PSCommandPath
$markerIndex = -1
for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -eq $payloadMarker) { $markerIndex = $i; break }
}
if ($markerIndex -lt 0) { throw 'installer payload marker not found' }
$b64 = ($lines[($markerIndex + 1)..($lines.Count - 1)] -join '')
$zipBytes = [Convert]::FromBase64String($b64)
$zipPath = Join-Path $tempDir 'payload.zip'
[System.IO.File]::WriteAllBytes($zipPath, $zipBytes)
Expand-Archive -LiteralPath $zipPath -DestinationPath $tempDir -Force

Push-Location $tempDir
try {
    & (Join-Path $tempDir 'install.ps1') -Port $Port -NoAutostart:$NoAutostart -SkipLibreHardwareMonitor:$SkipLibreHardwareMonitor
} finally {
    Pop-Location
    Remove-Item -Recurse -Force $tempDir -ErrorAction SilentlyContinue
}
'''

MARKER = "__CYD_PAYLOAD_BELOW__\n"


def build(output):
    m = re.search(r'^__version__ = "([^"]+)"',
                  (ROOT / 'feeder/pc_stats_feeder.py').read_text(), re.M)
    if not m:
        raise SystemExit('could not find __version__ in feeder/pc_stats_feeder.py')
    version = m.group(1)

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname in PAYLOAD:
            data = (ROOT / arcname).read_bytes()
            zf.writestr(arcname, data)

    target = output / f'cyd-pc-stats-{version}-windows.ps1'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(HEADER + MARKER + base64.b64encode(payload.getvalue()).decode('ascii') + "\n",
                      encoding='utf-8')
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix('.ps1.sha256').write_text(f'{digest}  {target.name}\n', encoding='utf-8')
    print(target)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist')
    build(parser.parse_args().output_dir)
