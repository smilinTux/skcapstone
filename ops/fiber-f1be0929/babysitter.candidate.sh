#!/usr/bin/env bash
# Compatibility entry point only. The managed timer is the restart loop.
# No direct prompts, claim changes, sleeping loop or local worker launches.
set -euo pipefail
unit=skfleet-fiber-dispatch.timer
if [[ $(hostname -s | tr '[:upper:]' '[:lower:]') != chiap08 ]]; then
  echo 'BLOCKED: only chiap08 owns admission.' >&2
  exit 78
fi
if [[ $(systemctl --user show "$unit" -p LoadState --value) != loaded ]]; then
  echo 'BLOCKED: guarded dispatcher timer is not installed.' >&2
  exit 78
fi
if [[ -e "$HOME/.config/skfleet-fiber/HOLD" ]]; then
  echo 'BLOCKED: admission is held; preserved workers were not prompted.' >&2
  exit 78
fi
exec systemctl --user start "$unit"
