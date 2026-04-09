#!/usr/bin/env bash
# sk-agent-picker.sh — Sovereign agent picker for AI coding tools
#
# Source this file in ~/.bashrc or ~/.zshrc.  It wraps `claude`, `codex`
# (OpenAI Codex CLI), and `opencode` with an agent-aware launcher that
# shows a numbered menu when multiple SK agents are configured.
#
# Behaviour:
#   - Zero agents found       → launch tool normally (no SK home yet)
#   - Exactly one agent       → use it silently, no prompt
#   - Multiple agents         → numbered menu, default highlighted with →
#   - SKCAPSTONE_AGENT is set → honour it, skip menu entirely
#   - Pass --agent <name>     → skip menu, use that agent directly
#   - Any other args          → forwarded to the underlying tool unchanged
#
# Usage:
#   claude                        # picker if multiple agents
#   claude --agent lumina         # direct launch
#   SKCAPSTONE_AGENT=opus claude  # env override (existing behaviour)
#   codex                         # same picker logic
#   opencode                      # same picker logic
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
    local default="${SKCAPSTONE_AGENT:-lumina}"

    if [[ $count -eq 0 ]]; then
        echo ""; return 0
    fi

    if [[ $count -eq 1 ]]; then
        echo "${agents[0]}"; return 0
    fi

    # Multi-agent menu
    echo "" >&2
    echo "  ╔══════════════════════════════════╗" >&2
    echo "  ║   SKCapstone — Choose an Agent   ║" >&2
    echo "  ╚══════════════════════════════════╝" >&2
    echo "" >&2

    local i=1
    local default_idx=1
    for agent in "${agents[@]}"; do
        local marker="  "
        if [[ "$agent" == "$default" ]]; then
            marker="→ "
            default_idx=$i
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

    # Invalid
    printf "\n  ⚠  Unknown choice '%s', using '%s'\n\n" "$choice" "$default" >&2
    echo "$default"
}

# ---------------------------------------------------------------------------
# Generic launcher used by all wrappers
# ---------------------------------------------------------------------------
_sk_launch() {
    local tool="$1"; shift         # the underlying binary (claude / codex / opencode)
    local extra_flags="$1"; shift  # tool-specific flags always appended (pass "" if none)
    # remaining args collected below after parsing --agent

    # Parse --agent <name> / --agent=<name> out of args first.
    # SK_NO_PICKER=1 skips the menu entirely (for scripted/CI use).
    local agent=""
    local -a passthrough=()
    local skip_next=0

    for arg in "$@"; do
        if [[ $skip_next -eq 1 ]]; then
            agent="$arg"; skip_next=0; continue
        fi
        case "$arg" in
            --agent)        skip_next=1 ;;
            --agent=*)      agent="${arg#--agent=}" ;;
            *)              passthrough+=("$arg") ;;
        esac
    done

    # --agent flag given → skip picker
    # SK_NO_PICKER=1 → skip picker (scripted/CI use)
    if [[ -z "$agent" && "${SK_NO_PICKER:-0}" != "1" ]]; then
        agent=$(_sk_pick_agent)
    fi

    # Fallback: if picker returned empty (0 agents), just use SKCAPSTONE_AGENT
    # or launch bare if that's also unset.
    if [[ -z "$agent" ]]; then
        agent="${SKCAPSTONE_AGENT:-}"
    fi

    if [[ -n "$agent" ]]; then
        printf "  ▶ Starting %s as agent: %s\n\n" "$tool" "$agent" >&2
        if [[ -n "$extra_flags" ]]; then
            SKCAPSTONE_AGENT="$agent" command "$tool" $extra_flags "${passthrough[@]}"
        else
            SKCAPSTONE_AGENT="$agent" command "$tool" "${passthrough[@]}"
        fi
    else
        if [[ -n "$extra_flags" ]]; then
            command "$tool" $extra_flags "${passthrough[@]}"
        else
            command "$tool" "${passthrough[@]}"
        fi
    fi
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
    _sk_launch claude "--dangerously-skip-permissions" "$@"
}

# codex (OpenAI Codex CLI — https://github.com/openai/codex)
function codex {
    _sk_launch codex "--full-auto" "$@"
}

# opencode (opencode.ai)
function opencode {
    _sk_launch opencode "" "$@"
}

# Export so sub-shells (tmux panes, etc.) inherit the functions
export -f _sk_pick_agent 2>/dev/null || true
export -f _sk_launch     2>/dev/null || true
export -f claude         2>/dev/null || true
export -f codex          2>/dev/null || true
export -f opencode       2>/dev/null || true
