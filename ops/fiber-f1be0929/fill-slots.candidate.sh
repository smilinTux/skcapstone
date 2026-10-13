#!/usr/bin/env bash
# Compatibility entry point. All new launches share one managed admission owner.
# Existing Herdr/Pi sessions are preserved. Stage and provider choices live in jobs.
set -euo pipefail
unit=skfleet-fiber-dispatch.service
if ! load_state=$(systemctl --user show "$unit" -p LoadState --value) || [[ "$load_state" != loaded ]]; then
  echo 'BLOCKED: fiber dispatcher is not installed; no new local workers started.' >&2
  exit 78
fi
exec systemctl --user start "$unit"
