#!/bin/bash
# install-launchd.sh — Install SK launchd plists on macOS
# Usage: ./install-launchd.sh [--all | --skcapstone | --skchat | --skcomm | --cloud9]
#
# Copies plist templates to ~/Library/LaunchAgents/, expands ${HOME},
# and optionally loads them immediately.

set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
    echo "ERROR: This script is for macOS only."
    exit 1
fi

LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCH_AGENTS"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOS_DIR="$(dirname "$SCRIPT_DIR")"  # skcapstone-repos/skcapstone
REPOS_ROOT="$(dirname "$REPOS_DIR")"  # skcapstone-repos/

# All available plists by component
declare -A PLIST_DIRS=(
    [skcapstone]="$REPOS_ROOT/skcapstone/launchd"
    [skchat]="$REPOS_ROOT/skchat/launchd"
    [skcomm]="$REPOS_ROOT/skcomm/launchd"
    [cloud9]="$REPOS_ROOT/cloud9/launchd"
)

install_plists() {
    local component="$1"
    local src_dir="${PLIST_DIRS[$component]}"

    if [[ ! -d "$src_dir" ]]; then
        echo "  SKIP: $src_dir not found"
        return
    fi

    echo "Installing $component plists..."
    for plist in "$src_dir"/*.plist; do
        [[ -f "$plist" ]] || continue
        local name
        name="$(basename "$plist")"
        local dest="$LAUNCH_AGENTS/$name"

        # Expand ${HOME} and $HOME to actual home directory
        sed "s|\${HOME}|$HOME|g; s|\$HOME|$HOME|g" "$plist" > "$dest"
        echo "  -> $dest"
    done
}

load_plists() {
    local component="$1"
    local src_dir="${PLIST_DIRS[$component]}"

    [[ -d "$src_dir" ]] || return
    for plist in "$src_dir"/*.plist; do
        [[ -f "$plist" ]] || continue
        local name
        name="$(basename "$plist")"
        local dest="$LAUNCH_AGENTS/$name"
        local label="${name%.plist}"

        # Unload if already loaded (ignore errors)
        launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
        # Load
        launchctl bootstrap "gui/$(id -u)" "$dest" 2>/dev/null || \
            launchctl load "$dest" 2>/dev/null || true
        echo "  LOADED: $label"
    done
}

uninstall_plists() {
    echo "Uninstalling all SK launchd plists..."
    for plist in "$LAUNCH_AGENTS"/com.skcapstone.*.plist; do
        [[ -f "$plist" ]] || continue
        local label
        label="$(basename "${plist%.plist}")"
        launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
        rm -f "$plist"
        echo "  REMOVED: $label"
    done
    echo "Done."
    exit 0
}

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --all          Install all components"
    echo "  --skcapstone   Install skcapstone plists (daemon, memory-compress, heartbeat, queue-drain)"
    echo "  --skchat       Install skchat plists (daemon, lumina-bridge, opus-bridge)"
    echo "  --skcomm       Install skcomm plists (api server, daemon)"
    echo "  --cloud9       Install cloud9 plists (daemon)"
    echo "  --load         Also load/start services after installing"
    echo "  --uninstall    Remove all SK plists and unload services"
    echo "  -h, --help     Show this help"
    exit 0
}

# Parse args
COMPONENTS=()
DO_LOAD=false

if [[ $# -eq 0 ]]; then
    usage
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)        COMPONENTS=(skcapstone skchat skcomm cloud9) ;;
        --skcapstone) COMPONENTS+=(skcapstone) ;;
        --skchat)     COMPONENTS+=(skchat) ;;
        --skcomm)     COMPONENTS+=(skcomm) ;;
        --cloud9)     COMPONENTS+=(cloud9) ;;
        --load)       DO_LOAD=true ;;
        --uninstall)  uninstall_plists ;;
        -h|--help)    usage ;;
        *)            echo "Unknown option: $1"; usage ;;
    esac
    shift
done

if [[ ${#COMPONENTS[@]} -eq 0 ]]; then
    echo "No components specified. Use --all or pick specific ones."
    exit 1
fi

# Create log directories
mkdir -p "$HOME/.skcapstone/logs" "$HOME/.skchat" "$HOME/.skcomm" "$HOME/.openclaw/logs"

# Install
for comp in "${COMPONENTS[@]}"; do
    install_plists "$comp"
done

# Optionally load
if $DO_LOAD; then
    echo ""
    echo "Loading services..."
    for comp in "${COMPONENTS[@]}"; do
        load_plists "$comp"
    done
fi

echo ""
echo "Done! Plists installed to $LAUNCH_AGENTS"
echo ""
echo "To manage services:"
echo "  launchctl list | grep skcapstone    # See running services"
echo "  launchctl stop com.skcapstone.XXX   # Stop a service"
echo "  launchctl start com.skcapstone.XXX  # Start a service"
echo ""
echo "To load all at next login, they'll start automatically (RunAtLoad=true)."
echo "To load now, re-run with --load flag."
