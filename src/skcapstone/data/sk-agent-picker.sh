#!/usr/bin/env bash
# sk-agent-picker.sh — Sovereign agent picker for AI coding tools
#
# Source this file in ~/.bashrc or ~/.zshrc.  It wraps `claude`, `codex`
# (OpenAI Codex CLI), and `opencode` with an agent-aware launcher that
# shows a numbered menu when multiple SK agents are configured.
#
# Also provides `skswitch` — a fast way to change the active agent for
# the current shell session (updates SKAGENT + legacy vars in one shot).
#
# Behaviour:
#   - Zero agents found       → launch tool normally (no SK home yet)
#   - Exactly one agent       → use it silently, no prompt
#   - Multiple agents         → numbered menu, default highlighted with →
#   - SKAGENT/SKCAPSTONE_AGENT set & valid → honour it silently, no menu
#   - Missing binary          → offer official install command for that tool
#   - SK_CLAUDE_YOLO=1        → claude adds permission bypass globally (opt-in)
#   - SK_CODEX_YOLO=1         → codex adds approval+sandbox bypass globally (opt-in)
#   - SK_OPENCODE_YOLO=1      → opencode allows all tools without approval (opt-in)
#   - Pass --agent <name>     → skip menu, use that agent directly
#   - Print mode (-p / --print) → skip menu (non-interactive by definition)
#   - stdin not a TTY         → skip menu (no way to read user input)
#   - SK_NO_PICKER=1          → skip menu (scripted/CI use)
#   - Any other args          → forwarded to the underlying tool unchanged
#
# Usage:
#   claude                        # picker if multiple agents
#   claude --agent lumina         # direct launch
#   SKAGENT=opus claude           # env override
#   SK_CLAUDE_YOLO=1 claude       # claude with dangerous permission bypass
#   skswitch lumina               # change active agent for this shell
#   skswitch                      # interactive picker
#   codex                         # same picker logic
#   SK_CODEX_YOLO=1 codex         # codex with dangerous bypass enabled
#   opencode                      # same picker logic
#   SK_OPENCODE_YOLO=1 opencode   # opencode with all permissions allowed
#
# To enable globally for all future shell sessions:
#   export SK_CLAUDE_YOLO=1
#   export SK_CODEX_YOLO=1
#   export SK_OPENCODE_YOLO=1
#   source ~/.bashrc
#
# Source in shell config:
#   source ~/.skenv/share/skcapstone/sk-agent-picker.sh
# Dev install:
#   source ~/clawd/skcapstone-repos/skcapstone/scripts/sk-agent-picker.sh

# ---------------------------------------------------------------------------
# Core picker — returns chosen agent name on stdout, menu on stderr
# ---------------------------------------------------------------------------
_sk_pick_agent() {
    local agents_dir="${SKCAPSTONE_HOME:-$HOME/.skcapstone}/agents"
    local -a agents=()

    if [[ -d "$agents_dir" ]]; then
        while IFS= read -r entry; do
            local name
            name=$(basename "$entry")
            # Skip template dirs, dotfiles, and non-directory entries
            if [[ -d "$entry" && "$name" != *-template && "$name" != .* && "$name" != *.* ]]; then
                agents+=("$name")
            fi
        done < <(find "$agents_dir" -mindepth 1 -maxdepth 1 -type d | sort)
    fi

    local count="${#agents[@]}"

    if [[ $count -eq 0 ]]; then
        echo ""; return 0
    fi

    if [[ $count -eq 1 ]]; then
        echo "${agents[0]}"; return 0
    fi

    # Validate SKAGENT against actual agent list.
    # If it's set but not in the list (stale env), fall back to first agent.
    local env_agent="${SKAGENT:-${SKCAPSTONE_AGENT:-}}"
    local default="${agents[0]}"
    local env_match=0
    for agent in "${agents[@]}"; do
        if [[ "$agent" == "$env_agent" ]]; then
            default="$agent"
            env_match=1
            break
        fi
    done

    # If env explicitly selected a real agent, skip the menu entirely.
    # Same if stdin isn't a TTY (we'd hang waiting for input that can't come).
    if [[ $env_match -eq 1 ]] || [[ ! -t 0 ]]; then
        echo "$default"; return 0
    fi

    # Multi-agent menu
    echo "" >&2
    echo "  ╔══════════════════════════════════╗" >&2
    echo "  ║   SKCapstone — Choose an Agent   ║" >&2
    echo "  ╚══════════════════════════════════╝" >&2
    echo "" >&2

    local i=1
    for agent in "${agents[@]}"; do
        local marker="  "
        if [[ "$agent" == "$default" ]]; then
            marker="→ "
        fi
        printf "  %s%2d)  %s\n" "$marker" "$i" "$agent" >&2
        (( i++ ))
    done

    echo "" >&2
    printf "  Agent [1-%d, Enter = %s]: " "$count" "$default" >&2

    local choice
    read -r choice </dev/tty

    # Empty → use default
    if [[ -z "$choice" ]]; then
        echo "$default"; return 0
    fi

    # Numeric
    if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= count )); then
        echo "${agents[$((choice - 1))]}"; return 0
    fi

    # Name typed directly
    for agent in "${agents[@]}"; do
        if [[ "$agent" == "$choice" ]]; then
            echo "$agent"; return 0
        fi
    done

    # Invalid — use list-validated default (not stale env), re-show options
    printf "\n  ⚠  Unknown agent '%s'. Valid agents:\n" "$choice" >&2
    for agent in "${agents[@]}"; do
        printf "       %s\n" "$agent" >&2
    done
    printf "  Using default: %s\n\n" "$default" >&2
    echo "$default"
}

# ---------------------------------------------------------------------------
# Generic launcher used by all wrappers
# ---------------------------------------------------------------------------
_sk_install_command() {
    local tool="$1"

    case "$tool" in
        claude)
            printf '%s' 'npm install -g @anthropic-ai/claude-code'
            ;;
        codex)
            printf '%s' 'npm install -g @openai/codex'
            ;;
        opencode)
            printf '%s' "unset -f opencode _sk_launch _sk_pick_agent claude codex skswitch 2>/dev/null || true; curl -fsSL https://opencode.ai/install | bash -s -- --no-modify-path"
            ;;
        *)
            return 1
            ;;
    esac
}

_sk_find_tool_path() {
    local tool="$1"
    local tool_path=""
    local -a fallback_paths=()

    tool_path=$(type -P "$tool" 2>/dev/null || true)
    if [[ -n "$tool_path" && -x "$tool_path" ]]; then
        printf '%s\n' "$tool_path"
        return 0
    fi

    case "$tool" in
        claude)
            fallback_paths=(
                "$HOME/.npm-global/bin/claude"
                "$HOME/.local/bin/claude"
            )
            ;;
        codex)
            fallback_paths=(
                "$HOME/.npm-global/bin/codex"
                "$HOME/.local/bin/codex"
            )
            ;;
        opencode)
            fallback_paths=(
                "$HOME/.opencode/bin/opencode"
                "$HOME/.local/bin/opencode"
                "$HOME/bin/opencode"
            )
            ;;
    esac

    local candidate
    for candidate in "${fallback_paths[@]}"; do
        if [[ -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    return 1
}

_sk_offer_install() {
    local tool="$1"
    local install_cmd
    install_cmd=$(_sk_install_command "$tool") || return 1

    echo "  ⚠  $tool is not installed." >&2
    echo "  Standard install command:" >&2
    echo "      $install_cmd" >&2

    if [[ ! -t 0 ]]; then
        echo "  Non-interactive shell detected; install it manually and retry." >&2
        return 127
    fi

    printf "  Install %s now? [y/N]: " "$tool" >&2
    local choice
    read -r choice </dev/tty
    if [[ ! "$choice" =~ ^([yY]|[yY][eE][sS])$ ]]; then
        echo "  Skipping install." >&2
        return 127
    fi

    echo "" >&2
    echo "  ▶ Installing $tool..." >&2
    if ! /bin/bash -lc "$install_cmd"; then
        echo "  ✖ Install failed for $tool." >&2
        return 1
    fi

    return 0
}

_sk_launch() {
    local tool="$1"; shift         # the underlying binary (claude / codex / opencode)
    local extra_flags="$1"; shift  # tool-specific flags always appended (pass "" if none)
    # remaining args collected below after parsing --agent

    # Parse --agent <name> / --agent=<name> out of args first.
    # SK_NO_PICKER=1 skips the menu entirely (for scripted/CI use).
    # Also detect print/non-interactive modes (-p, --print, --output-format)
    # so we never hang on the menu when claude/codex/opencode are invoked
    # non-interactively (skill dispatchers, CI, automation).
    local agent=""
    local -a passthrough=()
    local skip_next=0
    local non_interactive=0

    for arg in "$@"; do
        if [[ $skip_next -eq 1 ]]; then
            agent="$arg"; skip_next=0; continue
        fi
        case "$arg" in
            --agent)            skip_next=1 ;;
            --agent=*)          agent="${arg#--agent=}" ;;
            -p|--print)         non_interactive=1; passthrough+=("$arg") ;;
            --output-format|--output-format=*) non_interactive=1; passthrough+=("$arg") ;;
            *)                  passthrough+=("$arg") ;;
        esac
    done

    # --agent flag given → skip picker
    # SK_NO_PICKER=1 → skip picker (scripted/CI use)
    # Print/non-interactive mode → skip picker (no menu can be answered)
    if [[ -z "$agent" && "${SK_NO_PICKER:-0}" != "1" && $non_interactive -eq 0 ]]; then
        agent=$(_sk_pick_agent)
    elif [[ -z "$agent" && $non_interactive -eq 1 ]]; then
        # Non-interactive: take env or first agent silently
        agent="${SKAGENT:-${SKCAPSTONE_AGENT:-}}"
        if [[ -z "$agent" ]]; then
            local agents_dir="${SKCAPSTONE_HOME:-$HOME/.skcapstone}/agents"
            if [[ -d "$agents_dir" ]]; then
                agent=$(find "$agents_dir" -mindepth 1 -maxdepth 1 -type d ! -name '*-template' ! -name '.*' -printf '%f\n' | sort | head -1)
            fi
        fi
    fi

    # Fallback: if picker returned empty (0 agents), just use SKAGENT
    # or launch bare if that's also unset.
    if [[ -z "$agent" ]]; then
        agent="${SKAGENT:-${SKCAPSTONE_AGENT:-}}"
    fi

    local tool_path=""
    tool_path=$(_sk_find_tool_path "$tool" || true)
    if [[ -z "$tool_path" ]]; then
        if ! _sk_offer_install "$tool"; then
            return $?
        fi
        tool_path=$(_sk_find_tool_path "$tool" || true)
        if [[ -z "$tool_path" ]]; then
            echo "  ✖ $tool is still not available on PATH after installation." >&2
            return 127
        fi
    fi

    if [[ -n "$agent" ]]; then
        printf "  ▶ Starting %s as agent: %s\n\n" "$tool" "$agent" >&2
        if [[ -n "$extra_flags" ]]; then
            SKAGENT="$agent" SKCAPSTONE_AGENT="$agent" SKMEMORY_AGENT="$agent" "$tool_path" $extra_flags "${passthrough[@]}"
        else
            SKAGENT="$agent" SKCAPSTONE_AGENT="$agent" SKMEMORY_AGENT="$agent" "$tool_path" "${passthrough[@]}"
        fi
    else
        if [[ -n "$extra_flags" ]]; then
            "$tool_path" $extra_flags "${passthrough[@]}"
        else
            "$tool_path" "${passthrough[@]}"
        fi
    fi
}

# ---------------------------------------------------------------------------
# skswitch — change the active agent for the current shell session
# ---------------------------------------------------------------------------
function skswitch {
    local agent="$1"

    if [[ -z "$agent" ]]; then
        # No argument — show interactive picker
        agent=$(_sk_pick_agent)
        if [[ -z "$agent" ]]; then
            echo "No agents found in ${SKCAPSTONE_HOME:-$HOME/.skcapstone}/agents/" >&2
            return 1
        fi
    fi

    # Validate agent directory exists
    local agent_dir="${SKCAPSTONE_HOME:-$HOME/.skcapstone}/agents/$agent"
    if [[ ! -d "$agent_dir" ]]; then
        echo "Agent not found: $agent" >&2
        echo "Available agents:" >&2
        local agents_dir="${SKCAPSTONE_HOME:-$HOME/.skcapstone}/agents"
        if [[ -d "$agents_dir" ]]; then
            find "$agents_dir" -mindepth 1 -maxdepth 1 -type d ! -name '*-template' ! -name '.*' -printf '  %f\n' | sort >&2
        fi
        return 1
    fi

    export SKAGENT="$agent"
    export SKCAPSTONE_AGENT="$agent"
    export SKMEMORY_AGENT="$agent"
    echo "Switched to agent: $agent"
}

# ---------------------------------------------------------------------------
# Per-tool wrapper functions
# Must unalias first — an active alias with the same name causes bash to
# expand it during function-definition parsing, producing a syntax error.
# ---------------------------------------------------------------------------
unalias claude   2>/dev/null || true
unalias codex    2>/dev/null || true
unalias opencode 2>/dev/null || true

# claude (Claude Code CLI)
function claude {
    local extra_flags=""
    if [[ "${SK_CLAUDE_YOLO:-0}" == "1" ]]; then
        extra_flags="--dangerously-skip-permissions"
    fi
    _sk_launch claude "$extra_flags" "$@"
}

# codex (OpenAI Codex CLI — https://github.com/openai/codex)
function codex {
    local extra_flags=""
    if [[ "${SK_CODEX_YOLO:-0}" == "1" ]]; then
        extra_flags="--dangerously-bypass-approvals-and-sandbox"
    fi
    _sk_launch codex "$extra_flags" "$@"
}

# opencode (opencode.ai)
function opencode {
    if [[ "${SK_OPENCODE_YOLO:-0}" == "1" ]]; then
        OPENCODE_PERMISSION='{"*":"allow"}' _sk_launch opencode "" "$@"
    else
        _sk_launch opencode "" "$@"
    fi
}

# Export so sub-shells (tmux panes, etc.) inherit the functions
export -f _sk_pick_agent 2>/dev/null || true
export -f _sk_install_command 2>/dev/null || true
export -f _sk_find_tool_path 2>/dev/null || true
export -f _sk_offer_install 2>/dev/null || true
export -f _sk_launch     2>/dev/null || true
export -f skswitch       2>/dev/null || true
export -f claude         2>/dev/null || true
export -f codex          2>/dev/null || true
export -f opencode       2>/dev/null || true
