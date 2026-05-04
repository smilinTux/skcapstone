#!/bin/bash
# install.sh — Sovereign Agent Suite Installer
#
# Installs all SK* packages into a dedicated virtualenv at ~/.skenv.
# This keeps the system Python clean and avoids --break-system-packages.
#
# Usage:
#   bash scripts/install.sh           # Standard install
#   bash scripts/install.sh --dev     # Include dev/test tools
#   bash scripts/install.sh --force   # Recreate venv from scratch
#
# After install, add to your shell profile:
#   export PATH="$HOME/.skenv/bin:$PATH"

set -euo pipefail

SKENV="$HOME/.skenv"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DEV_MODE=false
FORCE=false

for arg in "$@"; do
    case "$arg" in
        --dev)  DEV_MODE=true ;;
        --force) FORCE=true ;;
    esac
done

echo "=== Sovereign Agent Suite Installer ==="
echo ""

# ---------------------------------------------------------------------------
# Step 1: Check prerequisites
# ---------------------------------------------------------------------------
PYTHON=""
for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major="${ver%%.*}"
        minor="${ver##*.}"
        if [[ "$major" -ge 3 ]] && [[ "$minor" -ge 10 ]]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "ERROR: Python 3.10+ required. Found none."
    exit 1
fi

echo "[1/6] Using $PYTHON ($($PYTHON --version 2>&1))"

# ---------------------------------------------------------------------------
# Step 2: Create virtualenv
# ---------------------------------------------------------------------------
if [[ "$FORCE" == "true" ]] && [[ -d "$SKENV" ]]; then
    echo "[2/6] Removing existing venv (--force)..."
    rm -rf "$SKENV"
fi

if [[ ! -d "$SKENV" ]]; then
    echo "[2/6] Creating virtualenv at $SKENV..."
    "$PYTHON" -m venv "$SKENV"
else
    echo "[2/6] Virtualenv exists at $SKENV"
fi

PIP="$SKENV/bin/pip"
$PIP install --upgrade pip -q 2>/dev/null

# ---------------------------------------------------------------------------
# Step 3: Install SK* packages
# ---------------------------------------------------------------------------
echo "[3/6] Installing SK* packages..."

# Helper: install editable if local dir exists, else from PyPI
install_pkg() {
    local name="$1"
    local extras="${2:-}"
    local paths="${3:-}"

    for path in $paths; do
        if [[ -d "$path" ]]; then
            if [[ -n "$extras" ]]; then
                $PIP install -e "${path}[${extras}]" -q 2>/dev/null && echo "  $name (editable: $path)" && return 0
                # Retry without extras if they fail
                $PIP install -e "$path" -q 2>/dev/null && echo "  $name (editable, no extras: $path)" && return 0
            else
                $PIP install -e "$path" -q 2>/dev/null && echo "  $name (editable: $path)" && return 0
            fi
        fi
    done

    # Fall back to PyPI
    if [[ -n "$extras" ]]; then
        $PIP install "${name}[${extras}]" -q 2>/dev/null && echo "  $name (PyPI)" && return 0
        $PIP install "$name" -q 2>/dev/null && echo "  $name (PyPI, no extras)" && return 0
    else
        $PIP install "$name" -q 2>/dev/null && echo "  $name (PyPI)" && return 0
    fi

    echo "  $name (FAILED — skipping)" && return 1
}

# Parent dir of skcapstone (where sibling repos might live)
PARENT="$(dirname "$REPO_ROOT")"
PILLAR="$PARENT/pillar-repos"

# Core packages (in dependency order)
install_pkg "capauth"    "all"                      "$PILLAR/capauth $PARENT/capauth"
install_pkg "cloud9-protocol" ""                    "$PILLAR/cloud9 $PARENT/cloud9 $PILLAR/cloud9-python $PARENT/cloud9-python"
install_pkg "skmemory"   ""                         "$PILLAR/skmemory $PARENT/skmemory"
install_pkg "skcomm"     "cli,crypto,discovery,api" "$PILLAR/skcomm $PARENT/skcomm"
install_pkg "skcapstone" ""                         "$REPO_ROOT"
install_pkg "skchat-sovereign" "all"                "$PARENT/skchat"
install_pkg "skseal"     ""                         "$PARENT/skseal"
install_pkg "skskills"   ""                         "$PARENT/skskills"
install_pkg "sksecurity" ""                         "$PARENT/sksecurity $PILLAR/SKSecurity $PARENT/SKSecurity"
install_pkg "skseed"     ""                         "$PILLAR/skseed $PARENT/skseed"
install_pkg "skwhisper"  ""                         "$PARENT/skwhisper-dev $PILLAR/skwhisper $PARENT/skwhisper"

# ---------------------------------------------------------------------------
# Step 4: Dev tools (optional)
# ---------------------------------------------------------------------------
if [[ "$DEV_MODE" == "true" ]]; then
    echo "[4/6] Installing dev tools..."
    $PIP install pytest pytest-cov ruff black -q 2>/dev/null
    echo "  pytest, pytest-cov, ruff, black"
else
    echo "[4/6] Skipping dev tools (use --dev to include)"
fi

# ---------------------------------------------------------------------------
# Step 5: Register skills & MCP servers
# ---------------------------------------------------------------------------
echo "[5/6] Registering skills and MCP servers..."
"$SKENV/bin/skcapstone" register 2>/dev/null && echo "  Registration complete" || echo "  (registration skipped — run 'skcapstone register' manually)"

# ---------------------------------------------------------------------------
# Step 6: PATH setup
# ---------------------------------------------------------------------------
echo "[6/6] Verifying installation..."

failures=0
for cmd in skcomm skcapstone capauth skmemory; do
    if "$SKENV/bin/$cmd" --version &>/dev/null; then
        echo "  $cmd OK"
    else
        echo "  $cmd FAILED"
        failures=$((failures + 1))
    fi
done

echo ""

# Check if PATH is configured
if echo "$PATH" | grep -q "$SKENV/bin"; then
    echo "PATH already includes $SKENV/bin"
else
    echo "Add this to your ~/.bashrc (or ~/.zshrc):"
    echo ""
    echo "  export PATH=\"\$HOME/.skenv/bin:\$PATH\""
    echo ""

    # Auto-add if not present
    for rcfile in "$HOME/.bashrc" "$HOME/.zshrc"; do
        if [[ -f "$rcfile" ]] && ! grep -q ".skenv/bin" "$rcfile"; then
            echo "" >> "$rcfile"
            echo '# SK* sovereign suite — installed in dedicated venv' >> "$rcfile"
            echo 'export PATH="$HOME/.skenv/bin:$PATH"' >> "$rcfile"
            echo "  (Added to $rcfile)"
        fi
    done
fi

# ---------------------------------------------------------------------------
# Wire the SK agent picker into shell rc files.
#
# The picker (sk-agent-picker.sh) is shipped inside the skcapstone Python
# package as data and discovered via `skcapstone shell-init`, so there is
# nothing to copy here — every install layout (PyPI / editable / install.sh)
# resolves to the same file via importlib.resources.
# ---------------------------------------------------------------------------
_PICKER_SNIPPET=$(cat <<'SNIPPET'

# SKCapstone agent picker + skswitch — sources the picker bundled in the
# skcapstone package via `skcapstone shell-init`. Honours pre-set SKAGENT
# without prompting.
if command -v skcapstone >/dev/null 2>&1; then
    eval "$(skcapstone shell-init 2>/dev/null)" || alias claude='claude --dangerously-skip-permissions'
else
    alias claude='claude --dangerously-skip-permissions'
fi
SNIPPET
)

for rcfile in "$HOME/.bashrc" "$HOME/.zshrc"; do
    [[ -f "$rcfile" ]] || continue

    # Migration: strip a stale legacy picker block that pointed at a hardcoded
    # path (either ~/.skenv/share/skcapstone/sk-agent-picker.sh from older
    # install.sh runs, or the dev-tree path used before the package shipped
    # the picker). The new snippet below replaces it.
    if grep -q '_SK_PICKER=' "$rcfile" && ! grep -q 'skcapstone shell-init' "$rcfile"; then
        # Best-effort: drop the old _SK_PICKER assignment + its `if/else/fi`
        # source block. Done in two passes for portability with BSD/GNU sed.
        sed -i '/^_SK_PICKER=/,/^unset _SK_PICKER$/d' "$rcfile"
        sed -i '/^# SKCapstone agent picker/d' "$rcfile"
        echo "  Removed legacy _SK_PICKER block from $rcfile"
    fi

    if ! grep -q 'skcapstone shell-init' "$rcfile"; then
        # Remove any plain `alias claude=...` that would conflict
        if grep -q "alias claude=" "$rcfile"; then
            sed -i "/alias claude=/d" "$rcfile"
        fi
        echo "$_PICKER_SNIPPET" >> "$rcfile"
        echo "  Agent picker wired → $rcfile"
    fi
done

echo ""
if [[ "$failures" -eq 0 ]]; then
    echo "=== Installation complete ==="
else
    echo "=== Installation complete with $failures warning(s) ==="
fi
echo ""
echo "Commands available: skcomm, skcapstone, capauth, skchat, skseal, skmemory, skskills, sksecurity, skseed"
echo "Venv location:     $SKENV"
echo "To activate:       source $SKENV/bin/activate"

# ---------------------------------------------------------------------------
# Linux: Install systemd user services for all SK* pillars
# ---------------------------------------------------------------------------
if [[ "$(uname)" == "Linux" ]] && command -v systemctl &>/dev/null; then
    echo ""
    echo "=== Linux Systemd Services ==="
    echo ""
    echo "SKCapstone can install systemd user services so your agent starts"
    echo "automatically at login. This includes skcapstone, skchat, and skcomm."
    echo ""
    read -r -p "Install systemd user services? [Y/n] " _SYSTEMD_ANSWER
    _SYSTEMD_ANSWER="${_SYSTEMD_ANSWER:-Y}"

    if [[ "$_SYSTEMD_ANSWER" =~ ^[Yy] ]]; then
        _DEFAULT_AGENT="${SKAGENT:-${SKCAPSTONE_AGENT:-lumina}}"
        read -r -p "Agent name [$_DEFAULT_AGENT]: " _AGENT_NAME
        _AGENT_NAME="${_AGENT_NAME:-$_DEFAULT_AGENT}"

        _UNIT_DIR="${HOME}/.config/systemd/user"
        mkdir -p "$_UNIT_DIR"

        _installed=0

        # skcapstone services
        for _unit in skcapstone.service skcapstone@.service \
                     skcapstone-memory-compress.service skcapstone-memory-compress.timer \
                     skcomm-heartbeat.service skcomm-heartbeat.timer; do
            _src="$REPO_ROOT/systemd/$_unit"
            if [[ -f "$_src" ]]; then
                # Substitute agent name in non-template units
                if [[ "$_unit" != *@* ]]; then
                    sed "s/=lumina/=$_AGENT_NAME/g" "$_src" > "$_UNIT_DIR/$_unit"
                else
                    cp "$_src" "$_UNIT_DIR/$_unit"
                fi
                echo "  [OK] $_unit"
                (( _installed++ ))
            fi
        done

        # skchat services (sibling repo)
        _SKCHAT_DIR="$(dirname "$REPO_ROOT")/skchat/systemd"
        for _unit in skchat-daemon.service skchat-lumina-bridge.service \
                     skchat-opus-bridge.service skchat-bridges.target; do
            _src="$_SKCHAT_DIR/$_unit"
            if [[ -f "$_src" ]]; then
                sed "s/=lumina/=$_AGENT_NAME/g; s/=opus/=$_AGENT_NAME/g" "$_src" > "$_UNIT_DIR/$_unit"
                echo "  [OK] $_unit"
                (( _installed++ ))
            fi
        done

        # skcomm services (sibling repo)
        _SKCOMM_DIR="$(dirname "$REPO_ROOT")/skcomm/systemd"
        for _unit in skcomm.service skcomm-daemon.service; do
            _src="$_SKCOMM_DIR/$_unit"
            if [[ -f "$_src" ]]; then
                sed "s/=lumina/=$_AGENT_NAME/g" "$_src" > "$_UNIT_DIR/$_unit"
                echo "  [OK] $_unit"
                (( _installed++ ))
            fi
        done

        echo ""
        echo "  Installed $_installed service files to $_UNIT_DIR/"

        systemctl --user daemon-reload
        echo "  systemd daemon reloaded"

        read -r -p "Enable and start core services now? [Y/n] " _START_NOW
        _START_NOW="${_START_NOW:-Y}"
        if [[ "$_START_NOW" =~ ^[Yy] ]]; then
            systemctl --user enable --now skcapstone.service 2>/dev/null && echo "  [STARTED] skcapstone" || true
            systemctl --user enable --now skchat-daemon.service 2>/dev/null && echo "  [STARTED] skchat-daemon" || true
            systemctl --user enable --now skchat-bridges.target 2>/dev/null && echo "  [STARTED] skchat-bridges" || true
            systemctl --user enable skcapstone-context.timer 2>/dev/null && echo "  [ENABLED] skcapstone-context.timer" || true
            systemctl --user enable skcomm-heartbeat.timer 2>/dev/null && echo "  [ENABLED] skcomm-heartbeat.timer" || true
        else
            echo "  Skipped. Enable later: systemctl --user enable --now skcapstone.service"
        fi
    else
        echo "  Skipped. Install later by re-running: bash scripts/install.sh"
    fi
fi

# ---------------------------------------------------------------------------
# macOS: Offer launchd service installation
# ---------------------------------------------------------------------------
if [[ "$(uname)" == "Darwin" ]]; then
    echo ""
    echo "=== macOS Auto-Start Services ==="
    echo ""
    echo "SKCapstone can install launchd services so your agent starts"
    echo "automatically at login. You can choose which services to install."
    echo ""
    read -r -p "Install launchd auto-start services? [Y/n] " _LAUNCHD_ANSWER
    _LAUNCHD_ANSWER="${_LAUNCHD_ANSWER:-Y}"

    if [[ "$_LAUNCHD_ANSWER" =~ ^[Yy] ]]; then
        # Ask for agent name
        _DEFAULT_AGENT="${SKAGENT:-${SKCAPSTONE_AGENT:-sovereign}}"
        read -r -p "Agent name [$_DEFAULT_AGENT]: " _AGENT_NAME
        _AGENT_NAME="${_AGENT_NAME:-$_DEFAULT_AGENT}"

        read -r -p "Start services now? [y/N] " _START_NOW
        if [[ "$_START_NOW" =~ ^[Yy] ]]; then
            "$SKENV/bin/skcapstone" daemon install --agent "$_AGENT_NAME" --start
        else
            "$SKENV/bin/skcapstone" daemon install --agent "$_AGENT_NAME"
        fi
    else
        echo "Skipped. Install later: skcapstone daemon install --agent <name>"
    fi
fi
