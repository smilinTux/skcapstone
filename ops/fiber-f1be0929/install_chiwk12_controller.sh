#!/usr/bin/env bash
# Native state must prove cutover before the authority's account binding changes.
set -euo pipefail
[[ $(hostname -s) == chiap08 && -f "$HOME/.config/skfleet-fiber/HOLD" ]]
exec 9>"$HOME/.local/state/skfleet-fiber/authority.lock"
flock -n 9 || exit 75
[[ -f "$HOME/.config/skfleet-fiber/HOLD" ]]
migration_stage=${1:?reviewed source directory required}
migration_dest="$HOME/.local/lib/skfleet-fiber"
python3 -c 'import sqlite3; from pathlib import Path; p=Path.home()/".local/state/skfleet-fiber/admission.sqlite3"; db=sqlite3.connect("file:"+str(p)+"?mode=ro",uri=True); assert db.execute("SELECT COUNT(*) FROM runs WHERE host=? AND state!=?",("chiwk12","terminal")).fetchone()[0] == 0'
ssh -o BatchMode=yes -o ConnectTimeout=5 skuser01@chiwk12 /home/skuser01/.skenv/bin/python - <<'PY'
from datetime import datetime, timezone
import json, os, subprocess, sys, time
from pathlib import Path
assert os.getuid() == 1001
sys.path.insert(0, str(Path.home()/'.local/lib/skfleet-fiber'))
import fiber_sandbox
fiber_sandbox.probe()
state = subprocess.run(['systemctl', '--user', 'is-active', 'sknoded.service'], capture_output=True, text=True, timeout=10)
assert state.returncode == 0 and state.stdout.strip() == 'active'
old = subprocess.run(['sudo', '-n', '-H', '-u', 'mrarch', 'env', 'XDG_RUNTIME_DIR=/run/user/1002',
    'systemctl', '--user', 'show', 'sknoded.service', '-p', 'ActiveState', '-p', 'MainPID'], capture_output=True, text=True, timeout=10)
values = dict(line.split('=', 1) for line in old.stdout.splitlines() if '=' in line)
assert old.returncode == 0 and values == {'ActiveState': 'inactive', 'MainPID': '0'}
beat = json.loads((Path.home()/'.skcapstone/fleet/status/node-chiwk12/heartbeat.json').read_text())
stamp = datetime.strptime(beat['ts'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
assert 0 <= time.time() - stamp <= 180
print('skuser01 cutover and sandbox verified; old node service inactive')
PY
for name in fiber_control.py fiber_probe.py; do
  case "$name" in
    fiber_control.py) old=1c6b83bf5f606808df8e9f93230fc42959ad4c4c82717a0708bc89168f389231; new=0582fa4a2e4869a87ff0b720553a180da134934a378357c03e39b25eae09342c;;
    fiber_probe.py) old=743d32761c5d74c82d8a5ac98eb7250e142e2474fbb90fa7addeff2270ae15ba; new=72642ef060ee1ea77a7befc5ed71cb1bb05925e73820738c3e2f03e02175e2ee;;
  esac
  [[ ! -L "$migration_stage/$name" && ! -L "$migration_dest/$name" ]]
  [[ $(sha256sum "$migration_stage/$name" | cut -d' ' -f1) == "$new" ]]
  [[ $(sha256sum "$migration_dest/$name" | cut -d' ' -f1) == "$old" ]]
  [[ ! -e "$migration_dest/$name.fb673cd3-before" && ! -L "$migration_dest/$name.fb673cd3-before" ]]
  [[ ! -e "$migration_dest/$name.fb673cd3-stage" && ! -L "$migration_dest/$name.fb673cd3-stage" ]]
done
for name in fiber_control.py fiber_probe.py; do
  cp -p -- "$migration_dest/$name" "$migration_dest/$name.fb673cd3-before"
  install -m 600 -- "$migration_stage/$name" "$migration_dest/$name.fb673cd3-stage"
  mv -- "$migration_dest/$name.fb673cd3-stage" "$migration_dest/$name"
  cmp -- "$migration_stage/$name" "$migration_dest/$name"
  sha256sum "$migration_dest/$name"
done
