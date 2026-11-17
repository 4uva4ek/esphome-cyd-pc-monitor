#!/usr/bin/env bash
set -euo pipefail

# Works both from the checkout and from the installed copy.
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
install_dir=${XDG_DATA_HOME:-$HOME/.local/share}/cyd-pc-stats
if [[ -f $script_dir/.cyd-pc-stats-install ]]; then
    install_dir=$script_dir
fi
if [[ ! -f $install_dir/.cyd-pc-stats-install ]]; then
    echo "No CYD PC Stats installation found at $install_dir"
    exit 0
fi
[[ $install_dir == /*/cyd-pc-stats ]] || { echo 'Unexpected installation path.' >&2; exit 1; }
IFS= read -r unit_file < "$install_dir/.cyd-pc-stats-install"
[[ $unit_file == /*/systemd/user/cyd-pc-stats.service ]] || {
    echo 'Unexpected service path in installation marker.' >&2; exit 1;
}
if systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user disable --now cyd-pc-stats.service 2>/dev/null || true
fi
rm -f -- "$unit_file"
if systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user daemon-reload
fi
rm -rf -- "$install_dir"
echo 'Removed CYD PC Stats. Display firmware and system packages were kept.'
