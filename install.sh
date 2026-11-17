#!/usr/bin/env bash
# Install from this checkout, so forks install their own feeder sources.
set -euo pipefail

usage() {
    cat <<'HELP'
Usage: ./install.sh [--no-autostart] [feeder options]
Installs in ${XDG_DATA_HOME:-$HOME/.local/share}/cyd-pc-stats.
Options: --port PATH, --interval SECONDS, --cpu-fan NAME, --gpu-fan NAME,
         --nvidia-gpu ID, --drm-card cardN, --verbose
Autostart uses a systemd user service; run as your normal user, without sudo.
HELP
}

no_autostart=0
feeder_args=()
while (($#)); do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --no-autostart) no_autostart=1; shift ;;
        --port|--interval|--cpu-fan|--gpu-fan|--nvidia-gpu|--drm-card)
            if (($# < 2)); then echo "Missing value for $1" >&2; exit 2; fi
            feeder_args+=("$1" "$2"); shift 2 ;;
        --verbose|-v) feeder_args+=("$1"); shift ;;
        *) echo "Unknown installer option: $1" >&2; usage; exit 2 ;;
    esac
done
[[ $(uname -s) == Linux ]] || { echo 'This installer requires Linux.' >&2; exit 1; }
((EUID != 0)) || { echo 'Run as your normal user, without sudo.' >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || {
    echo 'Install Python 3.9+ with venv and pip support (see docs/linux.md).' >&2; exit 1;
}
if (( ! no_autostart )); then
    if ! systemctl --user show-environment >/dev/null 2>&1; then
        echo 'No systemd user session. Use --no-autostart and start the feeder manually.' >&2
        exit 1
    fi
fi

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
install_dir=${XDG_DATA_HOME:-$HOME/.local/share}/cyd-pc-stats
unit_dir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
[[ $install_dir == /* && $unit_dir == /* ]] || {
    echo 'XDG_DATA_HOME and XDG_CONFIG_HOME must be absolute paths.' >&2; exit 1;
}
mkdir -p -- "$install_dir"
python3 -m venv "$install_dir/venv"
"$install_dir/venv/bin/python" -m pip install -r "$source_dir/feeder/requirements.txt"
"$install_dir/venv/bin/python" "$source_dir/feeder/pc_stats_feeder.py" "${feeder_args[@]}" --check-config
# Stop an existing instance before replacing its files (including --no-autostart upgrades).
if systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user stop cyd-pc-stats.service 2>/dev/null || true
    if ((no_autostart)); then
        systemctl --user disable cyd-pc-stats.service 2>/dev/null || true
    fi
fi
cp -- "$source_dir/feeder/pc_stats_feeder.py" "$source_dir/feeder/linux_sensors.py" \
      "$source_dir/feeder/requirements.txt" "$source_dir/uninstall.sh" "$install_dir/"
# Store the exact launch arguments, including spaces, without eval or shell parsing.
"$install_dir/venv/bin/python" - "$install_dir" "$unit_dir" "${feeder_args[@]}" <<'PY'
import json
from pathlib import Path
import shlex
import sys

root, units = map(Path, sys.argv[1:3])
args = [str(root / 'venv/bin/python'), str(root / 'pc_stats_feeder.py'), *sys.argv[3:]]
launcher = root / 'run.sh'
launcher.write_text('#!/usr/bin/env bash\nexec ' + shlex.join(args) + ' "$@"\n')
launcher.chmod(0o755)
# systemd quoting differs from shell quoting. Escape literal specifiers and dollars.
def quote(value):
    return json.dumps(value, ensure_ascii=False).replace('%', '%%').replace('$', '$$')
unit = '''[Unit]
Description=CYD PC Stats USB feeder

[Service]
Type=simple
ExecStart=''' + ' '.join(quote(v) for v in args) + '''
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
'''
(root / 'cyd-pc-stats.service').write_text(unit)
(root / '.cyd-pc-stats-install').write_text(str(units / 'cyd-pc-stats.service') + '\n')
PY
if (( ! no_autostart )); then
    mkdir -p -- "$unit_dir"
    cp -- "$install_dir/cyd-pc-stats.service" "$unit_dir/cyd-pc-stats.service"
    systemctl --user daemon-reload
    systemctl --user enable cyd-pc-stats.service
    systemctl --user restart cyd-pc-stats.service
    echo 'Started cyd-pc-stats.service. Logs: journalctl --user -u cyd-pc-stats -f'
else
    printf 'Installed. Start manually: %q\n' "$install_dir/run.sh"
fi
cat <<'INFO'
USB permission denied? Add your user to the serial port's group (uucp on
Arch/CachyOS, usually dialout on Debian/Fedora), then log out and back in.
See docs/linux.md for setup, sensor limitations and diagnostics.
INFO
