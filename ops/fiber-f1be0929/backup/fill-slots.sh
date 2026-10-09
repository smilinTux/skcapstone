#!/usr/bin/env bash
# fill-slots.sh — staggered estate slot refiller with provider caps.
# Queue file lines: <card-id>|<provider>|<model>|<worktree>|<base-or--->|<prompt-file>
# Caps (edit to taste): ESTATE_MAX=9 GLM_MAX=5 DEEPSEEK_MAX=3 CODEX_MAX=2
# Stagger: 75s between launches (prevents retry-cascade rate-limit storms).
set -u
ORCH_DIR=/home/skuser01/.skcapstone/runtime/llm-orch
QUEUE="$ORCH_DIR/queue.txt"; LOCK="$ORCH_DIR/fill.lock"
ESTATE_MAX=59; GLM_MAX=9; DEEPSEEK_MAX=20; CODEX_MAX=30; STAGGER=45
# host RAM guard: keep ~700MB headroom per projected worker (pi grows to 0.5-2GB)
HOST_MAX=$(( $(free -m | awk '/^Mem:/{print int(($7-1500)/700)}') ))
[ "$HOST_MAX" -lt 1 ] && HOST_MAX=1
if [ "$ESTATE_MAX" -gt "$HOST_MAX" ]; then echo "host RAM guard: estate capped at $HOST_MAX (free MB constrained)"; ESTATE_MAX=$HOST_MAX; fi
PI_PROFILE=$HOME/.pi/ds-orch-20260926

[ -f "$QUEUE" ] || { echo "no queue"; exit 0; }
exec 9>"$LOCK"; flock -n 9 || { echo "another fill running"; exit 0; }

read -r working glm ds codex <<EOF
$(herdr agent list 2>/dev/null | python3 -c "
import json,sys
from collections import Counter
agents=json.load(sys.stdin).get('result',{}).get('agents',[])
w=[a for a in agents if a.get('agent_status')=='working' and (a.get('name') or '').startswith(('glm-','glm2-','ds-','dsw-','codexw-'))]
def fam(n):
    if 'deepseek' in n or n.startswith('ds-'): return 'ds'
    if 'codex' in n: return 'codex'
    return 'glm'
c=Counter(fam(a['name']) for a in w)
print(len(w), c.get('glm',0), c.get('ds',0), c.get('codex',0))")
EOF

launched=0
while IFS='|' read -r cid provider model wt base promptf; do
  [ -z "$cid" ] && continue
  case "$provider" in
    skgw-zai) fam=glm; cap=$GLM_MAX ;;
    skgw-deepseek) fam=ds; cap=$DEEPSEEK_MAX ;;
    skgw-codex) fam=codex; cap=$CODEX_MAX ;;
    *) fam=glm; cap=$GLM_MAX ;;
  esac
  [ $((working+launched)) -ge $ESTATE_MAX ] && break
  famnow=$(herdr agent list 2>/dev/null | python3 -c "
import json,sys
agents=json.load(sys.stdin).get('result',{}).get('agents',[])
w=[(a.get('name') or '') for a in agents if a.get('agent_status')=='working' and (a.get('name') or '').startswith(('glm-','glm2-','ds-','dsw-','codexw-'))]
if '$fam'=='ds': print(sum(1 for n in w if 'deepseek' in n or n.startswith('ds-')))
elif '$fam'=='codex': print(sum(1 for n in w if 'codex' in n))
else: print(sum(1 for n in w if 'deepseek' not in n and 'codex' not in n))" 2>/dev/null || echo 99)
  [ "$famnow" -ge "$cap" ] && continue

  git -C /srv/sklegal-fast/work/glm-crew-20260926/repo fetch --quiet origin main 2>/dev/null
  mkdir -p "$wt"; rm -rf "$wt"
  if [ "$base" = "-" ] || [ -z "$base" ]; then base=origin/main; fi
  git -C /srv/sklegal-fast/work/glm-crew-20260926/repo worktree add --quiet --force "$wt" -B "work/$cid-$(echo $fam)" "$base" 2>/dev/null || { echo "worktree failed: $cid"; continue; }
  name="$(echo $fam)w-$cid"
  out=$(herdr tab create --workspace "${HERDR_WORKSPACE_ID:-w7D}" --cwd "$wt" --env PI_HOME=$PI_PROFILE --label "$name" --no-focus 2>&1)
  pane=$(printf '%s' "$out" | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['root_pane']['pane_id'])" 2>/dev/null)
  [ -z "$pane" ] && { echo "tab failed: $cid"; continue; }
  herdr agent start "$name" --kind pi --pane "$pane" --timeout 60000 -- --provider "$provider" --model "$model" >/dev/null 2>&1 || { echo "agent start failed: $cid"; continue; }
  herdr agent prompt "$name" "$(cat "$promptf")" >/dev/null 2>&1
  echo "launched $name ($provider/$model) pane=$pane"
  launched=$((launched+1))
  grep -v "^$cid|" "$QUEUE" > "$QUEUE.tmp" && mv "$QUEUE.tmp" "$QUEUE"
  [ $((working+launched)) -ge $ESTATE_MAX ] && break
  sleep $STAGGER
done < "$QUEUE"
echo "fill done: launched=$launched estate_before=$working"
