#!/bin/bash
# install.sh — Sovereign Agent Suite Installer
#
# Installs all SK* packages into a dedicated virtualenv at ~/.skenv.
# This keeps the system Python clean and avoids --break-system-packages.
#
# Usage:
#   bash scripts/install.sh                  # Standard install
#   bash scripts/install.sh --dev             # Include dev/test tools
#   bash scripts/install.sh --force           # Recreate venv from scratch
#   bash scripts/install.sh --non-interactive # venv + pip install only; never
#                                              # prompt, never touch systemd
#   bash scripts/install.sh --repair-path      # re-link entry points and undo a
#                                              # legacy ~/.skenv/bin PATH export
#
# PATH policy (see sk-standards TOOLCHAIN_PATH_ISOLATION_STANDARD):
# This installer does NOT put ~/.skenv/bin on PATH. That directory holds ~128
# entries, of which only ~20 are sk* commands; the rest (python3, pip, pytest,
# virtualenv, wheel, ansible*) would shadow the system binaries of the same name
# for every process started from your shell. A source build that probes for an
# interpreter would then silently pick the venv Python, succeed, and produce a
# binary linked against a libpython inside $HOME.
#
# Instead the sk* entry points are symlinked into ~/.local/bin. Console scripts
# carry an absolute shebang, so they still run on the venv interpreter.

set -euo pipefail

SKENV="$HOME/.skenv"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DEV_MODE=false
FORCE=false
NON_INTERACTIVE=false
REPAIR_PATH=false
USER_BIN="$HOME/.local/bin"

for arg in "$@"; do
    case "$arg" in
        --dev)  DEV_MODE=true ;;
        --force) FORCE=true ;;
        --non-interactive) NON_INTERACTIVE=true ;;
        --repair-path) REPAIR_PATH=true ;;
    esac
done

# True if $1 collides with a system binary, or is an interpreter/pip we must
# never expose. Such a name is refused even if it starts with sk/capauth.
_is_shadow_hazard() {
    case "$1" in
        python|python[0-9]*|pip|pip[0-9]*|activate*|Activate*|wheel|easy_install*|f2py|virtualenv)
            return 0 ;;
    esac
    [[ -e "/usr/bin/$1" ]] && return 0
    return 1
}

expose_entry_points() {
    mkdir -p "$USER_BIN"
    local linked=0 refused=0 kept=0 name target

    for path in "$SKENV"/bin/*; do
        [[ -f "$path" && -x "$path" ]] || continue
        name="$(basename "$path")"
        case "$name" in sk*|capauth*) ;; *) continue ;; esac

        if _is_shadow_hazard "$name"; then
            echo "  refused  $name (collides with a system binary)"
            refused=$((refused + 1)); continue
        fi

        target="$USER_BIN/$name"
        if [[ -L "$target" && "$(readlink -f "$target")" == "$(readlink -f "$path")" ]]; then
            kept=$((kept + 1)); continue          # already correct; idempotent
        fi
        if [[ -e "$target" && ! -L "$target" ]]; then
            # A real file from an older install would shadow the new venv copy.
            mv "$target" "$target.pre-skenv.$(date +%Y%m%d%H%M%S)"
            echo "  backed up  $name (stale real file from a previous install)"
        fi
        ln -sfn "$path" "$target"
        linked=$((linked + 1))
    done

    echo "  entry points: $linked linked, $kept already correct, $refused refused"
}

# Comment out a legacy `export PATH=.../.skenv/bin:$PATH` line written by an
# older installer. Never deletes the line, so the change is auditable and the
# operator can revert by uncommenting.
retire_legacy_path_export() {
    local rcfile changed=0
    for rcfile in "$HOME/.bashrc" "$HOME/.zshrc"; do
        [[ -f "$rcfile" ]] || continue
        grep -qE '^[[:space:]]*export PATH=.*\.skenv/bin' "$rcfile" || continue
        cp "$rcfile" "$rcfile.bak.$(date +%Y%m%d%H%M%S)"
        sed -i -E 's|^([[:space:]]*export PATH=.*\.skenv/bin.*)$|# [skcapstone] retired -- ~/.skenv/bin on PATH shadows system binaries.\n# Entry points are symlinked into ~/.local/bin instead. Re-enable by uncommenting:\n#\1|' "$rcfile"
        echo "  retired the legacy ~/.skenv/bin PATH export in $rcfile (backup written)"
        changed=1
    done
    [[ "$changed" == "0" ]] && echo "  no legacy ~/.skenv/bin PATH export found"
    return 0
}

# --repair-path: fix an existing install's PATH wiring and exit. Runs the same
# two functions a fresh install runs, so there is one code path, not two.
if [[ "$REPAIR_PATH" == "true" ]]; then
    echo ""
    echo "=== --repair-path ==="
    if [[ ! -d "$SKENV/bin" ]]; then
        echo "No venv at $SKENV. Run the installer without --repair-path first." >&2
        exit 1
    fi
    expose_entry_points
    retire_legacy_path_export
    echo ""
    echo "Done. Open a new shell, then verify:"
    echo "  command -v python3     # expect /usr/bin/python3, NOT ~/.skenv/bin"
    echo "  command -v skcapstone  # expect ~/.local/bin/skcapstone"
    exit 0
fi

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
install_pkg "cloud9" ""                    "$PILLAR/cloud9 $PARENT/cloud9 $PILLAR/cloud9-python $PARENT/cloud9-python"
install_pkg "skmemory"   ""                         "$PILLAR/skmemory $PARENT/skmemory"
install_pkg "skcomms"     "cli,crypto,discovery,api" "$PILLAR/skcomms $PARENT/skcomms"
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
for cmd in skcomms skcapstone capauth skmemory; do
    if "$SKENV/bin/$cmd" --version &>/dev/null; then
        echo "  $cmd OK"
    else
        echo "  $cmd FAILED"
        failures=$((failures + 1))
    fi
done

echo ""

# ---------------------------------------------------------------------------
# Expose entry points WITHOUT putting the venv bin/ on PATH.
#
# ~/.skenv/bin holds ~128 entries; only ~20 are sk* commands. Putting it on PATH
# shadows every same-named system binary (python3, pip, pytest, virtualenv,
# wheel, ansible*) for every child process, including package builds. Console
# scripts carry an absolute shebang, so a symlink still runs on the venv
# interpreter -- exposure and isolation are not in tension.
#
# See sk-standards TOOLCHAIN_PATH_ISOLATION_STANDARD.
# ---------------------------------------------------------------------------

echo "Exposing entry points in $USER_BIN ..."
expose_entry_points
retire_legacy_path_export

# ~/.local/bin is the standard user bin dir and shadows nothing, so it is the
# only PATH entry this installer will add.
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$USER_BIN"; then
    for rcfile in "$HOME/.bashrc" "$HOME/.zshrc"; do
        if [[ -f "$rcfile" ]] && ! grep -q '\.local/bin' "$rcfile"; then
            {
                echo ""
                echo '# SK* sovereign suite — entry points symlinked here from ~/.skenv/bin.'
                echo '# The venv bin/ is deliberately NOT on PATH; it would shadow system binaries.'
                echo 'export PATH="$HOME/.local/bin:$PATH"'
            } >> "$rcfile"
            echo "  added ~/.local/bin to PATH in $rcfile"
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

# ---------------------------------------------------------------------------
# Wire Codex global agent bootstrap.
#
# Codex reads ~/.codex/AGENTS.md into the prompt, so SKAGENT environment
# variables alone are not enough. The helper below creates an idempotent
# loader script and AGENTS.md guidance; `skcapstone doctor --fix` repairs the
# same files later if needed.
# ---------------------------------------------------------------------------
echo ""
if "$SKENV/bin/python" - <<'PY' 2>/dev/null
from skcapstone.codex_setup import ensure_codex_setup

actions = ensure_codex_setup()
for action in actions:
    print(f"  {action}")
PY
then
    echo "Codex SK agent bootstrap verified"
else
    echo "Codex SK agent bootstrap skipped — run 'skcapstone doctor --fix' later"
fi

echo ""
if [[ "$failures" -eq 0 ]]; then
    echo "=== Installation complete ==="
else
    echo "=== Installation complete with $failures warning(s) ==="
fi
echo ""
echo "Commands available: skcomms, skcapstone, capauth, skchat, skseal, skmemory, skskills, sksecurity, skseed"
echo "Venv location:     $SKENV"
echo "To activate:       source $SKENV/bin/activate"

# ---------------------------------------------------------------------------
# Linux: Install systemd user services for all SK* pillars
#
# Skipped entirely (no prompts, no systemd touched) under --non-interactive:
# copy-vs-activate callers (e.g. skfleet install's "packages"/"core"
# backends) only want the venv + pip install half of this script and must
# never block on a TTY read or silently enable/start units on EOF-defaults-Y.
# ---------------------------------------------------------------------------
if [[ "$NON_INTERACTIVE" == "true" ]]; then
    echo ""
    echo "=== Linux Systemd Services ==="
    echo ""
    echo "Skipping (--non-interactive): no systemd units installed, enabled, or started."
elif [[ "$(uname)" == "Linux" ]] && command -v systemctl &>/dev/null; then
    echo ""
    echo "=== Linux Systemd Services ==="
    echo ""
    echo "SKCapstone can install systemd user services so your agent starts"
    echo "automatically at login. This includes skcapstone, skchat, and skcomms."
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
                     skcomms-heartbeat.service skcomms-heartbeat.timer; do
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

        # skcomms services (sibling repo)
        _SKCOMMS_DIR="$(dirname "$REPO_ROOT")/skcomms/systemd"
        for _unit in skcomms.service skcomms-daemon.service; do
            _src="$_SKCOMMS_DIR/$_unit"
            if [[ -f "$_src" ]]; then
                sed "s/=lumina/=$_AGENT_NAME/g" "$_src" > "$_UNIT_DIR/$_unit"
                echo "  [OK] $_unit"
                (( _installed++ ))
            fi
        done

        # Syncthing desktop-tuning drop-in (machine-wide, agent-agnostic):
        # keeps the mesh's continuous sync deprioritised so it never starves
        # the active desktop session. Applies to whatever agent was just set up.
        _SYNCTHING_DROPIN="$REPO_ROOT/systemd/syncthing.service.d/nice.conf"
        if [[ -f "$_SYNCTHING_DROPIN" ]]; then
            mkdir -p "$_UNIT_DIR/syncthing.service.d"
            cp "$_SYNCTHING_DROPIN" "$_UNIT_DIR/syncthing.service.d/nice.conf"
            echo "  [OK] syncthing.service.d/nice.conf (desktop-courteous sync)"
            (( _installed++ ))
        fi

        echo ""
        echo "  Installed $_installed service files to $_UNIT_DIR/"

        systemctl --user daemon-reload
        echo "  systemd daemon reloaded"

        read -r -p "Enable and start core services now? [Y/n] " _START_NOW
        _START_NOW="${_START_NOW:-Y}"
        if [[ "$_START_NOW" =~ ^[Yy] ]]; then
            systemctl --user enable --now skcapstone.service 2>/dev/null && echo "  [STARTED] skcapstone" || true
            systemctl --user enable skcapstone-context.timer 2>/dev/null && echo "  [ENABLED] skcapstone-context.timer" || true
            systemctl --user enable skcomms-heartbeat.timer 2>/dev/null && echo "  [ENABLED] skcomms-heartbeat.timer" || true
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
        _DEFAULT_AGENT="${SKAGENT:-${SKCAPSTONE_AGENT:-lumina}}"
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
