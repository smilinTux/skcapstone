#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "run as root" >&2
    exit 1
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
windows_dir=/mnt/c/ProgramData/SKCapstone/TailnetBrowserProxy
powershell=/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe

install -D -m 0755 \
    "$repo_root/scripts/fleet/tailnet-browser-proxy.py" \
    /usr/local/libexec/skcapstone-tailnet-browser-proxy
install -D -m 0644 \
    "$repo_root/systemd/skcapstone-tailnet-browser-proxy.service" \
    /etc/systemd/system/skcapstone-tailnet-browser-proxy.service
install -d -m 0755 "$windows_dir"
install -m 0644 \
    "$repo_root/scripts/windows/Enable-TailscaleBrowserAccess.ps1" \
    "$repo_root/scripts/windows/Disable-TailscaleBrowserAccess.ps1" \
    "$repo_root/scripts/windows/Install-TailscaleBrowserShortcuts.ps1" \
    "$repo_root/scripts/windows/Test-TailscaleBrowserAccess.ps1" \
    "$windows_dir/"

systemctl daemon-reload
systemctl enable --now skcapstone-tailnet-browser-proxy.service

windows_path='C:\ProgramData\SKCapstone\TailnetBrowserProxy'
shortcut_installer=$(wslpath -w "$windows_dir/Install-TailscaleBrowserShortcuts.ps1")
"$powershell" -NoProfile -NonInteractive -ExecutionPolicy Bypass \
    -File "$shortcut_installer" \
    -ScriptDirectory "$windows_path"

for shortcut in \
    '/mnt/c/Users/Public/Desktop/Enable Tailscale Browser Access.lnk' \
    '/mnt/c/Users/Public/Desktop/Disable Tailscale Browser Access.lnk'; do
    if [[ ! -f $shortcut ]]; then
        echo "missing Windows Public Desktop shortcut: $shortcut" >&2
        exit 1
    fi
done

curl --fail --silent --show-error http://127.0.0.1:1056/healthz
echo
echo "tailnet browser proxy installed"
