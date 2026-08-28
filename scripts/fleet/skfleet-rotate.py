#!/usr/bin/env python3
"""SKWorld fleet rotation. Keeps N ephemeral codex workers busy on READY cards.

Fixes two defects found 03:50Z:
  1. Cards with INCOMPLETE DEPENDENCIES were being assigned. Workers correctly
     refused with BLOCKED, burned a slot, and produced no work.
  2. Slot accounting counted legacy persistent TUI panes as busy forever, so the
     rotation deadlocked at busy=8 and NOOPed. Workers launched with -p exit on
     their own, so a slot is simply a live codex-auto-* session. No retire logic.
"""
import json,os,glob,subprocess,sys,time,fcntl,datetime,hashlib,collections,re,importlib.util
from pathlib import Path

# Load this dependency-free module directly so the system Python job does not
# initialize optional skcoord API dependencies such as CapAuth.
_LIFECYCLE_PATH=Path(os.environ.get("SKCOORD_SRC",os.path.join(os.path.expanduser("~"),"work/skcoord/src")))/"skcoord/lifecycle_reassessment.py"
_spec=importlib.util.spec_from_file_location("skcoord_lifecycle_reassessment",_LIFECYCLE_PATH)
# Degrade instead of dying. A host that has not yet checked out skcoord must still
# be able to rotate workers: losing the pre-batch lifecycle report is a downgrade,
# losing the whole rotation is an outage. chiap04 crashed on exactly this the first
# time it ran, before its skcoord checkout existed.
_LIFECYCLE_OK = _spec is not None and _spec.loader is not None and _LIFECYCLE_PATH.exists()
if _LIFECYCLE_OK:
    try:
        _lifecycle=importlib.util.module_from_spec(_spec)
        sys.modules[_spec.name]=_lifecycle
        _spec.loader.exec_module(_lifecycle)
        assess,write_report=_lifecycle.assess,_lifecycle.write_report
    except Exception as _e:
        _LIFECYCLE_OK=False
        print("  WARN lifecycle reassessment unavailable (%s): rotating without the pre-batch report" % _e)
if not _LIFECYCLE_OK:
    assess=write_report=None

HOST=os.uname().nodename
ROTATION_HOSTS=("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")
SKC=os.path.expanduser("~/.skenv/bin/skcapstone")
TARGET=8
MAX_LAUNCH=int(os.environ.get("SKFLEET_MAX_LAUNCH","11"))
DRY = "--go" not in sys.argv
HOME=os.path.expanduser("~")
CARDS=os.path.join(HOME,".skcapstone/cards")
EVID=os.path.join(HOME,".skcapstone/evidence/fleet-rotation")
PI="/home/skuser01/.npm-global/bin/pi"
ESC_MODEL=os.environ.get("SKFLEET_ESC_MODEL","gpt-5.6-sol")
PRI={"critical":0,"high":1,"medium":2,"low":3}
STAMP=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def sh(*a): return subprocess.run(a,capture_output=True,text=True).stdout

_rows={}
def event_rows(cid):
    if cid in _rows: return _rows[cid]
    ev=os.path.join(CARDS,cid,"events"); out=[]
    if os.path.isdir(ev):
        for f in os.listdir(ev):
            try:
                for l in open(os.path.join(ev,f),encoding="utf-8",errors="replace"):
                    try: out.append(json.loads(l))
                    except: pass
            except OSError: pass
    out.sort(key=lambda e: (e.get("ts", ""), str(e.get("writer", "")), str(e.get("event_id", ""))))
    _rows[cid]=out; return out

def acts(cid):
    return [e.get("action") for e in event_rows(cid)]

def _dependency_value(event):
    payload=event.get("payload") if isinstance(event.get("payload"),dict) else {}
    for key in ("dependency_id","depends_on","dependency","target_card_id","target"):
        value=event.get(key,payload.get(key))
        if isinstance(value,str) and value:
            return value
    return None

def folded_dependencies(cid,core=None,fresh=False):
    if core is None:
        try: core=json.load(open(os.path.join(CARDS,cid,"core.json")))
        except Exception: core={}
    deps=[str(x) for x in (core.get("dependencies") or [])]
    rows=_acts_fresh(cid) if fresh else event_rows(cid)
    if fresh:
        rows.sort(key=lambda e: (e.get("ts", ""), str(e.get("writer", "")), str(e.get("event_id", ""))))
    for event in rows:
        dep=_dependency_value(event)
        if not dep: continue
        if event.get("action")=="add_dependency" and dep not in deps:
            deps.append(dep)
        elif event.get("action")=="remove_dependency":
            deps=[item for item in deps if item!=dep]
    return deps

def log(d,msg):
    os.makedirs(d,exist_ok=True)
    with open(os.path.join(d,"actions.log"),"a") as f: f.write(msg+"\n")
    print("  "+msg)

os.makedirs(os.path.join(HOME,".skcapstone/fleet"),exist_ok=True)
lock=open(os.path.join(HOME,".skcapstone/fleet/rotate.lock"),"w")
try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError:
    print("  rotation already running on %s"%HOST); sys.exit(0)

d=os.path.join(EVID,STAMP)

if HOST not in ROTATION_HOSTS:
    log(d,"NOOP|%s|host is outside the authorized chiap01-chiap03 worker fleet"%HOST)
    sys.exit(0)

# Mandatory read-only graph validation precedes slot and assignment decisions.
# The report is the exact machine-readable assignment exclusion contract.
try:
    assessment=assess(Path(CARDS),[Path(EVID)])
    report_path=Path(d)/"lifecycle-reassessment.json"
    write_report(assessment,report_path)
    # The lifecycle report's unclaimable_cards class is computed from HOST-LOCAL
    # worker logs, which ~/.skcapstone/.stignore excludes from Syncthing. Every
    # host therefore derives a DIFFERENT set from the same shared cards.
    # Measured 2026-08-27: chiap01 and chiap08 reported unclaimable_cards=37 and
    # excluded=91, while chiap03 reported 5 and 59, against identical card data.
    # Hosts disagreeing about what is workable is how the pool fragments.
    #
    # This rotation computes unclaimable(cid) itself, from the SHARED launch
    # record, immediately below. So drop that one class here and keep the rest,
    # which are all derived from shared data and on which every host agrees.
    _classes = assessment.get("classes", {}) or {}
    _local_only = {r.get("card_id") for r in _classes.get("unclaimable_cards", []) if r.get("card_id")}
    # The volatile identity class names its repair card as tracking_card. That
    # card is the work that removes the defect and must remain assignable; only
    # the generated drift records belong in the exclusion set.
    _tracking = {r.get("card_id") for r in _classes.get("volatile_ci_identity", [])
                 if r.get("card_id") and r.get("reason")=="tracking_card"}
    excluded=set(assessment["excluded_card_ids"]) - _local_only - _tracking
    log(d,"LIFECYCLE|%s|report=%s sha256=%s counts=%s excluded=%d"
        %(HOST,report_path,assessment["content_sha256"],json.dumps(assessment["counts"],sort_keys=True,separators=(",",":")),len(excluded)))
except Exception as exc:
    log(d,"BLOCKED|%s|lifecycle reassessment failed: %s"%(HOST,exc))
    sys.exit(2)

# a slot IS a live ephemeral worker; -p workers exit when finished
sessions=sh("tmux","ls","-F","#{session_name}").split()
GLM_HOLD_PATH=os.path.join(HOME,".skcapstone/evidence/fleet-glm-dispatch-hold.json")
glm_held=False
try:
    with open(GLM_HOLD_PATH,encoding="utf-8") as _fh:
        glm_held=bool(json.load(_fh).get("active"))
except (OSError,ValueError,TypeError):
    pass
# Two worker lanes. GLM sat unused for six hours because the rotation only managed
# codex-auto-* sessions, so the z.ai account received no traffic at all while nine
# idle legacy glm panes did nothing. A lane is a prefix, a model alias, a target.
LANES=[
    {"name":"codex","prefix":"codex-auto-","model":"sk-codex",
     "target":8},
    {"name":"glm","prefix":"glm-auto-","model":os.environ.get("SKFLEET_GLM_MODEL","glm-4.6"),
     "target":0 if glm_held else 3},
    # Restored. needs_escalation() still exists and still marks a card whose
    # worker reported blocked_on=capability, but the lane it routes to had been
    # dropped, so those cards were marked for a destination that did not exist
    # and could never be placed at all. 13 cards were in that state.
    #
    # It takes ONLY escalation cards and escalation cards go ONLY here, so the
    # stronger model is never spent on work the cheap lanes can do.
    {"name":"escalate","prefix":"esc-auto-",
     "model":os.environ.get("SKFLEET_ESC_MODEL", ESC_MODEL if "ESC_MODEL" in dir() else "gpt-5.6-sol"),
     "target":int(os.environ.get("SKFLEET_ESC_TARGET","2"))},
]
if glm_held:
    log(d,"GLM_HOLD|%s|new GLM dispatch disabled by %s"%(HOST,GLM_HOLD_PATH))
for _L in LANES:
    _L["busy"]=[s for s in sessions if s.startswith(_L["prefix"])]
    _L["free"]=max(0,_L["target"]-len(_L["busy"]))
free=sum(_L["free"] for _L in LANES)
log(d,"SLOTS|%s|%s|total_free=%d"%(HOST,
    " ".join("%s=%d/%d"%(L["name"],len(L["busy"]),L["target"]) for L in LANES),free))

# ---- worker liveness -------------------------------------------------------
# A claim used to be reaped from elapsed time alone, which is wrong in both
# directions at once. skcoord's detector will not look at a claim younger than
# 24h, so when workers were killed on 2026-08-27 the board sat frozen behind 89
# claims held by processes that no longer existed: ready=0 with 146 open cards
# and every slot free. Lowering that constant only trades the failure over, since
# a two hour floor yanks a card out from under a worker that is still running it.
#
# Time was only ever standing in for the question that actually matters, which is
# whether the process is still there. Each host can answer that for its own
# workers by reading tmux, and no host could see any other host's answer. So each
# run now publishes the cards it is running, into evidence/ where Syncthing shares
# it (.stignore excludes logs/, metrics/ and heartbeats/, not evidence/).
LIVE = os.path.join(HOME, ".skcapstone/evidence/fleet-live")
# Cards whose claim cannot be released because the stores disagree about it. The
# CardStore fold reads `claimed` while the legacy coordination/tasks store already
# considers it released, so `coord release-claim` answers "Already released",
# exits 0, and writes nothing. The reaper then sees a claimed card again next tick
# and tries again, forever.
#
# Measured 2026-08-28: 2b614910 had been reaped 455 times, ec202fdc 435 and
# d9552c4c 434, none of which ever produced a release_claim event. 2b614910 has
# exactly three events in its whole history: describe, claim, move.
#
# Recorded in the SHARED evidence dir so one host discovering it spares the other
# four, and so the divergence is visible rather than buried in a repeating log
# line that reads like successful work.
# NOT inside LIVE: the quorum check counts *.json there as reporting hosts, and a
# bookkeeping file dropped in that directory is counted as a sixth host, which
# pushes reporting below known and disables reaping fleetwide. Observed within
# one tick of adding it. It lives one level up.
_INEFFECTIVE_PATH = os.path.join(HOME, ".skcapstone/evidence/reap-ineffective.json")
LIVE_FRESH = 30 * 60      # a report older than this says nothing about now
CLAIM_GRACE = 300         # one full rotation period, so every host has reported
# Reaping needs a quorum, because a card running on chiap04 is invisible in
# chiap08's report. During a rollout the first host to publish is the ONLY
# reporting host, and without this floor it would read every other host's live
# worker as absent and reap all of them. Below quorum the reaper does nothing.
REAP_QUORUM = 3
KNOWN_HOST_TTL = 24 * 3600   # a host silent this long has left the fleet

def publish_live(sessions):
    """Record which cards this host is running, for every other host to read."""
    cards = sorted({s[len(L["prefix"]):] for L in LANES
                    for s in sessions if s.startswith(L["prefix"])})
    try:
        os.makedirs(LIVE, exist_ok=True)
        p = os.path.join(LIVE, HOST + ".json")
        tmp = p + ".new"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"host": HOST, "ts": time.time(), "cards": cards}, fh)
        os.replace(tmp, p)          # atomic, so a reader never sees a half file
    except OSError as exc:
        log(d, "WARN|%s|could not publish liveness: %s" % (HOST, exc))
    return cards

def live_report():
    """Return (oldest_recent_report, cards_running, reporting_host_count).

    The first value is the OLDEST report among currently reporting hosts, not the
    newest, and that choice is the whole safety property. A claim may only be
    reaped once EVERY reporting host has published since it was made, because a
    card running on chiap04 is invisible in chiap08's report. Taking the newest
    would let the first host to start publishing reap every other host's live
    workers, which is precisely the outage this code exists to prevent.
    """
    hosts = {}
    running = set()
    now = time.time()
    for p in glob.glob(os.path.join(LIVE, "*.json")):
        try:
            with open(p, encoding="utf-8") as fh:
                snap = json.load(fh)
            ts = float(snap.get("ts") or 0)
        except (OSError, ValueError, TypeError):
            continue
        if now - ts > LIVE_FRESH:
            continue
        hosts[str(snap.get("host") or p)] = ts
        running.update(str(c) for c in (snap.get("cards") or ()))
    return (min(hosts.values()) if hosts else 0.0), running, len(hosts)

publish_live(sessions)

if free==0:
    log(d,"NOOP|%s|all slots busy"%HOST); sys.exit(0)

# ---- assignable pool: unclaimed, not human, not drift, DEPENDENCIES SATISFIED
# Cards that were launched before and never produced a claim event cannot be
# claimed by a worker (closed ITIL incident, id namespace the board rejects, or
# already assigned elsewhere). Without this, the same card is relaunched every
# cycle forever: measured 78 of 162 launches wasted, 48 percent, before this gate.
_launched=collections.Counter()
_launched_at={}
_strong_launched_at={}
for _f in glob.glob(os.path.join(EVID,"*","actions.log")):
    try:
        _launch_epoch=datetime.datetime.strptime(Path(_f).parent.name,"%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        _launch_epoch=0
    try:
        for _l in open(_f,encoding="utf-8",errors="replace"):
            if _l.startswith("LAUNCHED|"):
                _p=_l.strip().split("|")
                if len(_p)>=4:
                    _launched[_p[3]]+=1
                    _launched_at[_p[3]]=max(_launched_at.get(_p[3],0),_launch_epoch)
                    if "model=%s"%ESC_MODEL in _p:
                        _strong_launched_at[_p[3]]=max(_strong_launched_at.get(_p[3],0),_launch_epoch)
    except OSError: pass
# A card claimed and then RELEASED is open again and must be assignable. The prior
# filter excluded any card with a claim action anywhere in history, making
# release_claim a one-way door: every card a worker released became permanently
# unassignable. Derive the LAST lifecycle state instead.
# unassign and archive also clear a claim. Omitting them reports an unassigned
# card as still claimed, which hides it from the pool permanently.
# TERMINAL states are sticky. complete and void END a card. Later assign,
# unassign or claim events do NOT resurrect it: unassigning a finished card
# clears an assignee, it does not un-finish the work. A naive last-write-wins
# fold gets this wrong and re-offers completed cards forever.
# Measured: 4d98b588 has claim, move, claim, complete, assign, unassign, claim
# and 92bd87a3 has claim, complete, assign, unassign. Both were being handed to
# workers, which then spent ~80 seconds each discovering the card was already
# done and correctly refusing. That was the real "stale pool" cost, and the race
# was a symptom rather than the cause.
_LC = {"claim": "claimed", "release_claim": "open", "unassign": "open",
       "archive": "void", "complete": "complete", "void": "void"}


def lifecycle_state(cid):
    """Last lifecycle state, honouring the kanban column as well as the actions.

    A card can be finished two ways: a `complete` action, or a `move` into the
    `done` column. This fold used to read only the actions, so anything finished
    via the column still looked OPEN and got launched again and again. Every
    worker then correctly refused it, because the coordination CLI does know:

        Card 128ce1c2 is already marked as DONE and cannot be claimed.
        Error: Task 128ce1c2 already done by unknown owner

    The worker burns a slot, records that, and stops exactly as instructed.
    Three of those and the card is banned by the launch-count backoff, so the
    rotation ends up punishing a card for being finished. Measured 2026-08-27:
    322 cards read as not-finished here while their column said done.

    The column is folded as a LAST VALUE, not as a sticky terminal, so moving a
    card back out of `done` reopens it. An explicit `complete` or `void` action
    still wins over the column, which keeps those genuinely terminal.
    """
    st = "open"
    col = None
    for e in event_rows(cid):
        a = e.get("action")
        if a == "move":
            c = str(e.get("column") or "").strip().lower()
            if c:
                col = c
            continue
        if a in _LC:
            if st == "void":
                continue
            if st == "complete" and a not in ("void", "archive"):
                continue
            st = _LC[a]
    if st not in ("complete", "void") and col == "done":
        return "complete"
    return st


# ---- BLOCKED backoff ---------------------------------------------------------
# A card that concludes BLOCKED releases its claim and returns to the pool, so it
# is re-picked minutes later and re-derives the identical verdict. Measured in one
# 70 minute window: 98da2f1a nine times, d3235a9b eight, f13ee9b1 and 572e5d4c
# seven each. That is full inference cost per repeat for no new information.
#
# Two signals, because one is not enough:
#  1. The recorded outcome. Read through the controlled vocabulary, never by
#     matching link_key literally: verdict has 41 spellings in this store.
#     A card whose latest outcome is BLOCKED stays out until something CHANGES,
#     where "changes" means one of its dependencies reached complete after that
#     verdict was written. That makes a dependency completing the natural wake
#     signal, so class-(b) cards revive on their own and class-(a) cards do not.
#  2. Launch history, for cards that report BLOCKED by skmail and write no
#     evidence event at all. 98da2f1a is exactly this: nine BLOCKED mails, zero
#     outcome events. Evidence-only logic would never see it.
_EVID_DIR = os.path.join(HOME, ".skcapstone/coordination/card_events")
_OUTCOME_KEYS = ("verdict", "result", "disposition", "review_decision")

def _fold_key(k):
    k = str(k or "").strip().lower().replace("-", "_")
    k = re.sub(r"_?20\d{6}t?\d{0,6}z?", "", k)
    k = re.sub(r"_[0-9a-f]{8,64}$", "", k)
    return re.sub(r"__+", "_", k).strip("_")

_outcomes = None
_label_events = None
def _load_outcomes():
    global _outcomes
    if _outcomes is not None: return _outcomes
    _outcomes = {}
    if os.path.isdir(_EVID_DIR):
        for f in sorted(glob.glob(os.path.join(_EVID_DIR, "*.jsonl"))):
            try:
                for l in open(f, encoding="utf-8", errors="replace"):
                    try: e = json.loads(l)
                    except Exception: continue
                    if e.get("action") != "link": continue
                    fk = _fold_key(e.get("link_key"))
                    if not any(o in fk for o in _OUTCOME_KEYS): continue
                    cid = e.get("card_id")
                    ts = str(e.get("ts") or "")
                    val = str(e.get("link_value") or "")
                    prev = _outcomes.get(cid)
                    if prev is None or ts > prev[0]:
                        _outcomes[cid] = (ts, val)
            except OSError: pass
    return _outcomes

def _ts_epoch(value):
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z","+00:00")).timestamp()
    except (TypeError,ValueError):
        return 0

def _label_value(event):
    payload=event.get("payload") if isinstance(event.get("payload"),dict) else {}
    for key in ("label","label_value","value"):
        value=event.get(key,payload.get(key))
        if isinstance(value,str) and value:
            return value
    return None

def _load_label_events():
    global _label_events
    if _label_events is not None: return _label_events
    _label_events={}
    for f in sorted(glob.glob(os.path.join(_EVID_DIR,"*.jsonl"))):
        try:
            for line in open(f,encoding="utf-8",errors="replace"):
                try: event=json.loads(line)
                except Exception: continue
                if event.get("action") not in ("add_label","remove_label"): continue
                cid=event.get("card_id")
                if cid: _label_events.setdefault(cid,[]).append(event)
        except OSError: pass
    for rows in _label_events.values():
        rows.sort(key=lambda e:(e.get("ts",""),str(e.get("writer","")),str(e.get("event_id",""))))
    return _label_events

def folded_labels(cid,core):
    labels=[str(x) for x in (core.get("initial_labels") or [])]
    for event in _load_label_events().get(cid,[]):
        label=_label_value(event)
        if not label: continue
        if event.get("action")=="add_label" and label not in labels:
            labels.append(label)
        elif event.get("action")=="remove_label":
            labels=[item for item in labels if item!=label]
    return labels

_NON_IMPLEMENTATION_LABELS = {
    "planning-only-container",
    "do-not-claim-as-implementation",
    "human-gate",
    "human-decision-recorded-no-action",
    "no-action-authorized",
}

def non_implementation(core, labels):
    folded={str(item).strip().lower().replace("_", "-") for item in labels}
    if folded & _NON_IMPLEMENTATION_LABELS:
        return True
    blob=(str(core.get("title") or "")+" "+json.dumps(labels)).upper()
    return "[HUMAN]" in blob

def _latest_material_change(cid, verdict_ts):
    threshold=_ts_epoch(verdict_ts); latest=0
    for event in event_rows(cid):
        if event.get("action") in ("add_dependency","remove_dependency"):
            latest=max(latest,_ts_epoch(event.get("ts")))
    for dep in folded_dependencies(cid):
        if lifecycle_state(dep)!="complete": continue
        for event in event_rows(dep):
            if event.get("action")=="complete":
                latest=max(latest,_ts_epoch(event.get("ts")))
    for event in _load_label_events().get(cid,[]):
        latest=max(latest,_ts_epoch(event.get("ts")))
    return latest if latest>threshold else 0


# A dependency is satisfied only if it completed AND did not complete BLOCKED.
# "complete" is a lifecycle fact; the outcome lives in the evidence store. Checking
# only the lifecycle is the joined-truth error this estate keeps making. Measured
# case: a7220561 appeared assignable because its dependency e5d5748f was complete,
# while that dependency's recorded verdict reads "BLOCKED. No independently
# qualifiable shared Qwen and Codex profile exists on the exact candidate." The
# foundation it depends on does not exist, so working it can only waste a slot.
def _dep_satisfied(dep):
    if lifecycle_state(dep) != "complete":
        return False
    ts, val = _load_outcomes().get(dep, (None, None))
    if ts and re.match(r"^\s*BLOCKED", val, re.I):
        return False
    return True

# ---- stale-pool race -------------------------------------------------------
# The pool is built by scanning ~1700 cards, then workers launch two seconds
# apart. Between the scan and a given launch, another host or a named agent can
# finish the card: ~/.skcapstone is one Syncthing folder and five hosts plus
# jarvis write to it continuously. The worker then spends roughly 80 seconds of
# real inference discovering the card is done. Observed on 4d98b588 and 92bd87a3:
# "Claim failed: card is already marked done by an unknown owner."
#
# Two things are needed, and the second is the one that is easy to miss:
#   1. Re-check lifecycle state immediately before launching, not only at build.
#   2. Do it WITHOUT the acts() cache. acts() memoizes per run, so a naive
#      re-check returns the same stale answer the pool was built from and buys
#      nothing at all.
def _acts_fresh(cid):
    """Read a card's actions straight from disk, bypassing the run-level cache."""
    ev = os.path.join(CARDS, cid, "events")
    out = []
    if os.path.isdir(ev):
        for f in os.listdir(ev):
            try:
                for l in open(os.path.join(ev, f), encoding="utf-8", errors="replace"):
                    try: out.append(json.loads(l))
                    except Exception: pass
            except OSError: pass
    return out

def _still_assignable(cid):
    """True if the card is still open right now, re-read from disk."""
    rows = _acts_fresh(cid)
    # seq is per-writer-file, so cross-writer ordering must use ts
    rows.sort(key=lambda e: (e.get("ts", ""), str(e.get("writer", "")), str(e.get("event_id", ""))))
    st = "open"
    for e in rows:
        a = e.get("action")
        if a in _LC:
            if st == "void":
                continue
            if st == "complete" and a not in ("void", "archive"):
                continue
            st = _LC[a]
    if st != "open": return False
    try: core=json.load(open(os.path.join(CARDS,cid,"core.json")))
    except Exception: return False
    labels=folded_labels(cid,core)
    if non_implementation(core,labels): return False
    if "foreign-project" in {str(item).strip().lower() for item in labels}: return False
    pin=host_pin(core,labels)
    if pin and pin != HOST: return False
    return not any(not _dep_satisfied(dep) for dep in folded_dependencies(cid,core,fresh=True))

_BLOCKED_CATEGORIES = ("dependency", "card", "human", "capability")
_BLOCKED_ON_RE = re.compile(r"blocked[_\s-]?on", re.I)
_BLOCKED_CAT_RE = re.compile(r"\b(%s)\b" % "|".join(_BLOCKED_CATEGORIES), re.I)
_BLOCKED_TOK_RE = re.compile(r"[A-Za-z0-9][\w:.\-/@]{2,}")
_BLOCKED_FILLER = set(_BLOCKED_CATEGORIES) | {
    "referent", "value", "is", "to", "on", "the", "a", "an", "of", "for",
    "blocked", "blocked_on", "none", "null", "unknown", "tbd", "pending",
    "true", "false",
}

_PIPE_VERDICT_RE = re.compile(
    r"^\s*BLOCKED\s*\|\s*(dependency|card|human|capability)\s*\|\s*([^|]+)", re.I)
_CAPABILITY_BLOCK_RE = re.compile(
    r"blocked[_\s-]?on[=: |]+\s*capability\b|^\s*BLOCKED\s*\|\s*capability\b", re.I)

def _verdict_is_actionable(val):
    """True if a BLOCKED verdict names a category AND the thing it refers to.

    A bare BLOCKED buys the card an exemption while telling nobody how to lift
    it, which is the worst possible trade: out of the pool and un-actionable.
    Measured 2026-08-27: of 39 open cards whose latest outcome was BLOCKED, 18
    were the literal word and 20 more named no blocked_on at all. That pool did
    not drain all day.

    An unexplained refusal therefore earns NO backoff here. The card returns to
    the pool. This is not a licence to churn: skcapstone now refuses to WRITE a
    bare BLOCKED at all, so a worker must either explain itself or record
    nothing, and recording nothing is still caught by the launch-attempt count
    below after three tries.
    """
    text = str(val or "")
    # Workers also write the verdict pipe-delimited, as BLOCKED|category|referent.
    # That names a category AND a referent, which is everything this function is
    # asking for, and it was being rejected purely on punctuation. Measured
    # 2026-08-27: BLOCKED|card|ac:1|bf3ffd12 read as unexplained, so the card got
    # no backoff, went round again, and re-derived the identical verdict. A
    # validator that refuses a truthful verdict on format teaches workers to
    # fight the validator instead of to explain themselves.
    pipe = _PIPE_VERDICT_RE.match(text)
    if pipe and pipe.group(2).strip():
        return True
    anchor = _BLOCKED_ON_RE.search(text)
    if not anchor:
        return False
    tail = text[anchor.end():]
    for cat in _BLOCKED_CAT_RE.finditer(tail):
        window = tail[cat.end(): cat.end() + 80]
        for tok in _BLOCKED_TOK_RE.finditer(window):
            cand = tok.group(0).strip().strip(".,;:\"'")
            if cand and cand.lower() not in _BLOCKED_FILLER:
                return True
    return False

_PASS_RE = re.compile(r"^\s*PASS(_FOR_REVIEW)?\b", re.I)

def blocked_backoff(cid):
    """True if this card should stay out of the pool for now."""
    ts, val = _load_outcomes().get(cid, (None, None))
    # An UNEXPLAINED refusal earns no verdict-based protection, but it must NOT
    # short-circuit out of this function. Returning False here would skip the
    # launch-attempt fallback below and let the card be relaunched forever,
    # burning a slot every cycle with nothing accumulating to stop it. That is a
    # black hole, and it was briefly live on 2026-08-27 until Chef spotted it.
    #
    # So an unactionable BLOCKED is treated as NO VERDICT and falls through. The
    # card returns to the pool, gets a real attempt, and if three attempts pass
    # with nothing recorded the launch-attempt rule below parks it anyway. There
    # is always a counter that ends the loop.
    if ts and re.match(r"^\s*BLOCKED", val, re.I) and _verdict_is_actionable(val):
        # Capability is a routing signal, not a dependency or approval hold. It
        # must reach needs_escalation() below, which preferentially assigns it to
        # Codex. After that stronger route has also returned capability, park the
        # card until authored state changes so the fleet does not loop forever.
        if _CAPABILITY_BLOCK_RE.search(str(val)):
            strong_at=_strong_launched_at.get(cid,0)
            if strong_at and _ts_epoch(ts)>=strong_at:
                change=_latest_material_change(cid,ts)
                if not change: return True
                return strong_at>=change
            return False
        change=_latest_material_change(cid,ts)
        if not change: return True
        return _launched_at.get(cid,0) >= change
    # No recorded outcome: fall back to launch history so mail-only BLOCKED
    # reporters are still caught. Three attempts with nothing to show is enough.
    #
    # But count only launches whose worker ACTUALLY REPORTED. This is the same
    # defect that was fixed in unclaimable(): _launched counts every launch,
    # including ones where the worker was killed seconds after starting and
    # never got as far as saying anything.
    #
    # Measured 2026-08-27, after the KillMode fix: 80 open cards were held here,
    # with launch counts of 19, 18, 20 and 9. Those launches happened while
    # skfleet-rotate.service ran Type=oneshot under the default
    # KillMode=control-group, so systemd tore down the cgroup and killed every
    # worker seconds after launch. The counter was measuring the launcher's bug,
    # then banning the card for it. Nothing was ever wrong with those 80 cards,
    # and the pool sat at ready=0 across all five hosts while 80 workable cards
    # were held out of it.
    #
    # _reporting_launches already skips zero-byte worker logs and ages out
    # evidence past a TTL, which is exactly the right test here.
    #
    # A card that RECORDED A PASS has something to show, and the counter above is
    # explicitly about having nothing to show. Measured 2026-08-27: 8 cards whose
    # latest outcome was PASS_FOR_REVIEW were parked here at exactly 3 launches,
    # reported to the operator inside blocked_backoff as though they had refused.
    # They had not refused, they had succeeded and were waiting on review. Parking
    # them is right, since re-running finished work wastes a slot; calling them
    # blocked is not, because it hides completed candidates in a bucket the
    # operator reads as failures.
    if ts and _PASS_RE.match(str(val or "")):
        return True
    if launch_attempts(cid) >= 3 and lifecycle_state(cid)!="complete":
        # ...unless the world changed since the last attempt. Without this the
        # counter is a one-way door: nothing resets it, so a card parked here is
        # parked forever no matter what happens to it afterwards. That is the
        # same black hole as an infinite relaunch, just pointing the other way,
        # and it is worse because it is silent.
        #
        # Measured 2026-08-27: 24 cards sat here on unexplained refusals. Their
        # blocker had since been removed (the NIGHT-HANDOFF freeze was lifted)
        # and the estate had started REFUSING to write a bare BLOCKED at all, so
        # a re-run would now be forced to explain itself. Neither fact could
        # reach them, because the only path back was through a verdict they were
        # not able to record in the first place.
        #
        # A material change is a real, authored event: a dependency added,
        # removed or completed, or a label applied. It is not free: something
        # must actually happen to the card to buy it another attempt.
        change = _material_change_since(cid, _launched_at.get(cid, 0))
        if not change:
            return True
        return _launched_at.get(cid, 0) >= change
    return False


def _material_change_since(cid, epoch):
    """Latest material change strictly after `epoch`, or 0 if there is none."""
    latest = 0
    for event in event_rows(cid):
        if event.get("action") in ("add_dependency", "remove_dependency"):
            latest = max(latest, _ts_epoch(event.get("ts")))
    for dep in folded_dependencies(cid):
        if lifecycle_state(dep) != "complete":
            continue
        for event in event_rows(dep):
            if event.get("action") == "complete":
                latest = max(latest, _ts_epoch(event.get("ts")))
    for event in _load_label_events().get(cid, []):
        latest = max(latest, _ts_epoch(event.get("ts")))
    return latest if latest > epoch else 0


def awaiting_review(cid):
    """True if this card produced a candidate and is waiting on a reviewer.

    Reported separately from blocked_backoff so that work which SUCCEEDED is not
    counted as work that refused.
    """
    ts, val = _load_outcomes().get(cid, (None, None))
    return bool(ts and _PASS_RE.match(str(val or "")))

# ---- host pinning ------------------------------------------------------------
# Some cards only work on the host that holds the asset. The skdashboard-read-only
# signer review failed seven times because private.asc lives ONLY on chiap08 (by
# design: doctor estate confirms Syncthing correctly excludes private keys), while
# the rotation kept handing the card to chiap01 where it can never exist.
#
# THE TRAP, measured before writing this: 83 open cards name a host, but 54 of them
# name a host that runs NO rotation (chiap04 16, chiwk11 18, chiwk12 13, chiap08 7).
# Pinning those would strand every one of them. So pinning NARROWS and never blocks:
#   names exactly one ROTATION host  -> only that host may take it
#   names a non-rotation host        -> unpinned, behaves exactly as before
#   names several hosts, or none     -> unpinned
# The worst case is therefore identical to today's behaviour, never worse.
# chiap08 added 2026-08-27: it is the ONLY host holding the skdashboard-read-only
# private key (Syncthing correctly excludes private keys), so host-sensitive
# custody reviews can only ever pass there. It is also the coordination hub, so
# its worker target is deliberately small.
# chiap04 added 2026-08-27 after provisioning tmux 3.4 and pi 0.84.3 and a
# 30G swapfile. 16 open cards name it, mostly the ChatGPT desktop client work,
# which can only be done on the host running that desktop.
KNOWN_HOSTS = ROTATION_HOSTS + ("chiap04", "chiap08", "chiwk11", "chiwk12", "noroc2027")

def host_pin(core,labels):
    """Host this card must run on, or None to leave it unpinned."""
    blob = (str(core.get("title") or "") + " " +
            json.dumps(labels)).lower()
    named = {h for h in KNOWN_HOSTS if h in blob}
    if len(named) != 1:
        return None                      # ambiguous or unnamed
    only = named.pop()
    return only if only in ROTATION_HOSTS else None   # never strand

# A launch only counts as EVIDENCE that a card is unclaimable if the worker
# actually got far enough to report. Two failure modes were being conflated:
#   - the board genuinely rejects the card (closed ITIL incident, id namespace it
#     will not accept, already assigned elsewhere). The worker writes a rejection
#     and exits. That IS evidence.
#   - the worker was INTERRUPTED before claiming: killed, host restarted, session
#     torn down. pi buffers output and writes at exit, so an interrupted worker
#     leaves a ZERO BYTE log. That is not evidence of anything.
# Measured: b0c8489a, the single card standing between the fleet and the SKLEGAL
# human gate, was excluded permanently on two launches that both left empty logs.
# 46 cards were held this way. Two guards now:
#   1. Only count a launch whose worker log is non-empty.
#   2. Age launches out after a TTL, so exclusion SELF-HEALS and a card gets
#      retried instead of being banned for the lifetime of the estate.
_LAUNCH_TTL_H = float(os.environ.get("SKFLEET_LAUNCH_TTL_H", "6"))
_LOGDIR = os.path.join(HOME, ".skcapstone/fleet/logs")

def _reporting_launches(cid):
    """Launches whose worker actually produced output, within the TTL."""
    n = 0
    cutoff = time.time() - _LAUNCH_TTL_H * 3600
    try:
        for f in os.listdir(_LOGDIR):
            if not f.startswith(cid + "-") or not f.endswith(".log"):
                continue
            fp = os.path.join(_LOGDIR, f)
            try:
                stt = os.stat(fp)
            except OSError:
                continue
            if stt.st_mtime < cutoff:
                continue          # aged out: exclusion self-heals
            if stt.st_size == 0:
                continue          # interrupted, never reported: not evidence
            n += 1
    except OSError:
        pass
    return n

_ROTATION_EVID = os.path.join(HOME, ".skcapstone/evidence/fleet-rotation")
_shared_launch_cache = None

def _shared_launch_attempts(cid):
    """Launch attempts for this card across EVERY host, within the TTL.

    Worker logs live under ~/.skcapstone/fleet/logs, and ~/.skcapstone/.stignore
    excludes `logs` and `**/logs/`, so those logs are HOST-LOCAL BY DESIGN and
    Syncthing never carries them. _reporting_launches therefore answers a
    different question on every host: it sees only what THIS host launched.

    Combined with the hash partition, that produced a fleet-wide deadlock.
    Every card is owned by exactly one host, so that host is the only one that
    accumulates worker logs for it, so that host is the only one that bans it.
    Each host ends up banning precisely its own partition while seeing every
    other host's partition as workable. Measured 2026-08-27: 12 workable cards
    fleet-wide, owned 5/4/2 across chiap01/chiap03/chiap04, and all four live
    hosts logged NOOP for twenty minutes while Jarvis alerted workers=0.

    The rotation's own actions.log under evidence/fleet-rotation IS synced (it
    has no `logs/` component in its path), and already carries LAUNCHED lines
    from every host. Counting that makes every host compute the same answer.
    """
    global _shared_launch_cache
    if _shared_launch_cache is None:
        _shared_launch_cache = {}
        cutoff = time.time() - _LAUNCH_TTL_H * 3600
        try:
            for stamp in os.listdir(_ROTATION_EVID):
                d = os.path.join(_ROTATION_EVID, stamp)
                log = os.path.join(d, "actions.log")
                try:
                    if os.stat(d).st_mtime < cutoff:
                        continue          # aged out: exclusion self-heals
                    with open(log, encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            if not line.startswith("LAUNCHED|"):
                                continue
                            parts = line.strip().split("|")
                            if len(parts) >= 4:
                                _shared_launch_cache[parts[3]] = (
                                    _shared_launch_cache.get(parts[3], 0) + 1
                                )
                except OSError:
                    continue
        except OSError:
            pass
    return _shared_launch_cache.get(cid, 0)

def launch_attempts(cid):
    """The strictest honest count: local reporting evidence OR the shared record.

    A host that holds worker logs keeps its stricter local signal, which can tell
    an interrupted worker from one that reported. A host without those logs still
    sees the shared count, so no card is banned on one host and workable on
    another purely because of where its logs happen to live.
    """
    return max(_reporting_launches(cid), _shared_launch_attempts(cid))

def unclaimable(cid):
    return launch_attempts(cid) >= 2 and "claim" not in acts(cid)

# ITIL records keep state in events under kind=="status" with the target in "to",
# NOT in core.json and NOT under the card vocabulary ("action"). A card-oriented
# reader sees no state at all and treats a closed incident as assignable, which is
# why workers kept reporting "incident is already closed". Note "to" is overloaded:
# kind=="assignment" also uses it, for a person. Only kind=="status" sets state.
ITIL=os.path.join(HOME,".skcapstone/coordination/itil")
TERMINAL={"closed","resolved","rejected","cancelled"}
def itil_terminal(cid):
    for sub in ("incidents","problems","changes"):
        d=os.path.join(ITIL,sub,cid,"events")
        if not os.path.isdir(d): continue
        evs=[]
        for f in os.listdir(d):
            try:
                for l in open(os.path.join(d,f),encoding="utf-8",errors="replace"):
                    try: evs.append(json.loads(l))
                    except: pass
            except OSError: pass
        evs.sort(key=lambda e:(e.get("ts",""),e.get("seq",0)))
        state=None
        for e in evs:
            if e.get("kind")=="status" and e.get("to"): state=e["to"]
        return state in TERMINAL
    return False


# ---- reap claims whose worker is provably gone -----------------------------
# The rule is an observation, never an age: some host published a report AFTER
# this claim was made, and no host reports the card as running. With no fresh
# report the reaper does nothing at all, so a Syncthing stall or a stopped fleet
# can never become a mass release. skcoord's 24h detector stays as the slow
# backstop for anything this cannot see.
#
# Only ephemeral one-shot workers are eligible. A named agent (jarvis, lumina) or
# a human may hold a claim deliberately for as long as they like.
_EPHEMERAL_OWNER = re.compile(r"^(pi|codex|glm)[-_]")

def _current_claim(cid):
    """The claim in force now, as (owner, epoch), or (None, 0)."""
    owner, ts, _revision = _claim_identity(event_rows(cid))
    return owner, ts


def _claim_identity(rows):
    """Fold rows into the current owner, timestamp, and claim revision."""
    owner = None
    ts = 0.0
    revision = None
    for e in rows:
        a = e.get("action")
        if a == "claim":
            owner = e.get("agent") or e.get("owner") or e.get("actor") or e.get("by")
            revision = e.get("claim_revision") or e.get("event_id")
            raw = str(e.get("ts") or e.get("timestamp") or "")
            try:
                ts = datetime.datetime.fromisoformat(
                    raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = 0.0
        elif a in ("release_claim", "unassign", "complete", "void", "archive"):
            owner, ts, revision = None, 0.0, None
    return owner, ts, revision

def _load_ineffective():
    try:
        with open(_INEFFECTIVE_PATH, encoding="utf-8") as fh:
            return {str(x) for x in json.load(fh).get("cards") or ()}
    except (OSError, ValueError):
        return set()


def _record_ineffective(cid):
    known = _load_ineffective()
    if cid in known:
        return
    known.add(cid)
    try:
        os.makedirs(os.path.dirname(_INEFFECTIVE_PATH), exist_ok=True)
        tmp = _INEFFECTIVE_PATH + ".new"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"cards": sorted(known)}, fh)
        os.replace(tmp, _INEFFECTIVE_PATH)
    except OSError:
        pass


def _current_claim_fresh(cid):
    """The claim in force RIGHT NOW, read from disk, bypassing the run cache.

    event_rows memoizes per run. A card can be claimed several times in the
    seconds between the pool being built and a release being issued, so the cached
    owner is not necessarily the owner CardStore will accept.

    Observed 2026-08-28 on ec202fdc, which took four claims in eight seconds:
      03:40:33 claim pi-codex   03:40:38 claim pi-codex
      03:40:41 claim pi-glm     03:40:49 claim pi-glm
    The release was refused with "CardStore owner conflict for ec202fdc: expected
    pi-glm-ec202fdc" because the cached read still named the codex owner. This is
    the same defect _acts_fresh exists to solve for the stale pool, applied to the
    claim owner.
    """
    owner, ts, _revision = _current_claim_identity_fresh(cid)
    return owner, ts


def _current_claim_identity_fresh(cid):
    """Return the current claim owner, timestamp, and exact revision."""
    return _claim_identity(_acts_fresh_rows(cid))


def _acts_fresh_rows(cid):
    """Every event for a card, straight from disk."""
    ev = os.path.join(CARDS, cid, "events")
    out = []
    if os.path.isdir(ev):
        for f in os.listdir(ev):
            try:
                for l in open(os.path.join(ev, f), encoding="utf-8", errors="replace"):
                    try: out.append(json.loads(l))
                    except Exception: pass
            except OSError:
                pass
    out.sort(key=lambda e: (str(e.get("ts") or ""), e.get("seq") or 0))
    return out


_fleet_launch_claims = None


def _launch_claim_fields(owner, claim_revision, successful):
    """Render exact claim provenance only for a successful fleet launch."""
    if not successful or not owner or not claim_revision:
        return ""
    return "|owner=%s|claim_revision=%s" % (owner, claim_revision)


def _fleet_launch_provenance(cid, owner, claim_revision):
    """Whether a successful fleet launch recorded this exact claim generation."""
    global _fleet_launch_claims
    if not cid or not owner or not claim_revision:
        return False
    if _fleet_launch_claims is None:
        _fleet_launch_claims = set()
        for path in glob.glob(os.path.join(EVID, "*", "actions*.log")):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if not line.startswith("LAUNCHED|"):
                            continue
                        parts = line.strip().split("|")
                        if len(parts) < 5:
                            continue
                        fields = dict(
                            part.split("=", 1) for part in parts[4:] if "=" in part
                        )
                        launch_owner = fields.get("owner")
                        launch_revision = fields.get("claim_revision")
                        if launch_owner and launch_revision:
                            _fleet_launch_claims.add(
                                (parts[3], launch_owner, launch_revision)
                            )
            except OSError:
                continue
    return (str(cid), str(owner), str(claim_revision)) in _fleet_launch_claims


def reap_dead_claims():
    """Return claimed cards whose worker no host reports running."""
    oldest, running, nhosts = live_report()
    # A host that is merely between runs must still be counted, or the quorum
    # check passes while its workers are invisible. A host that is GONE must
    # eventually stop counting, or one decommissioned machine blocks reaping for
    # the whole fleet forever. KNOWN_HOST_TTL separates the two.
    _cut = time.time() - KNOWN_HOST_TTL
    known = sum(1 for f in glob.glob(os.path.join(LIVE, "*.json"))
                if os.path.getmtime(f) >= _cut)
    if not oldest or nhosts < REAP_QUORUM or nhosts < known:
        log(d, "REAP|%s|below quorum (reporting=%d known=%d need>=%d); reaped nothing"
            % (HOST, nhosts, known, REAP_QUORUM))
        return 0
    freed = 0
    _ineffective = _load_ineffective()
    for cd in sorted(glob.glob(CARDS + "/*")):
        cid = os.path.basename(cd)
        if not os.path.exists(os.path.join(cd, "core.json")):
            continue
        if lifecycle_state(cid) != "claimed":
            continue
        owner, cts = _current_claim(cid)
        if not owner or not _EPHEMERAL_OWNER.match(str(owner)):
            continue
        if cid in running:
            continue                      # a host says this is running right now
        if cid in _ineffective:
            continue                      # releasing it does nothing; see above
        if oldest < cts + CLAIM_GRACE:
            continue                      # some host has not reported since the claim
        # Re-read the owner from disk immediately before releasing. The pool was
        # built seconds to minutes ago and the card may have been re-claimed since.
        fresh_owner, fresh_ts, fresh_revision = _current_claim_identity_fresh(cid)
        if not fresh_owner:
            continue                      # released by someone else in the meantime
        if fresh_owner != owner:
            log(d, "REAP_RECLAIMED|%s|%s|was %s now %s; leaving it alone this tick"
                % (HOST, cid, owner, fresh_owner))
            continue
        if not _fleet_launch_provenance(cid, fresh_owner, fresh_revision):
            log(d, "REAP_UNPROVEN|%s|%s|%s|claim revision %s has no exact successful "
                   "fleet launch record; leaving it for the stale-claim path"
                % (HOST, cid, fresh_owner, fresh_revision or "missing"))
            continue
        r = subprocess.run(
            [SKC, "coord", "release-claim", cid, "--owner", str(fresh_owner),
             "--agent", "fleet-liveness-reaper"],
            capture_output=True, text=True)
        if r.returncode == 0:
            _rows.pop(cid, None)          # the fold below must re-read from disk
            # A zero exit is not proof the claim moved. When the two stores
            # disagree the CLI answers "Already released" and writes nothing, so
            # confirm against the fold rather than trusting the return code.
            if lifecycle_state(cid) == "claimed":
                _record_ineffective(cid)
                log(d, "REAP_INEFFECTIVE|%s|%s|%s|release reported success but the "
                       "card is still claimed; CardStore and the legacy task store "
                       "disagree, needs repair" % (HOST, cid, owner))
                continue
            freed += 1
            log(d, "REAPED|%s|%s|%s|no host reports this card running" % (HOST, cid, owner))
        else:
            # A release that keeps failing is a divergence, not a transient. Record
            # it after the first failure so it does not retry every five minutes
            # forever, which is how 2b614910 accumulated 455 pointless calls.
            _record_ineffective(cid)
            log(d, "REAP_FAILED|%s|%s|%s" % (HOST, cid, (r.stderr or "").strip()[:120]))
    log(d, "REAP|%s|released=%d hosts_reporting=%d cards_running=%d ineffective=%d"
        % (HOST, freed, nhosts, len(running), len(_load_ineffective())))
    return freed

# DRY gates every board MUTATION, not only the launch. Before this, --go gated the
# tmux launch and nothing else, so running the rotation without --go still released
# claims and completed cards. Anyone inspecting what the rotation "would" do was
# silently changing the board, and importing the module for diagnostics ran a full
# mutating pass. Both happened repeatedly on 2026-08-28 while debugging the reaper.
#
# A dry run must be safe to run at any time, from any host, by anyone. That is the
# entire point of having one.
if DRY:
    log(d, "DRY_SKIPPED|%s|reap_dead_claims and close_reviewed_parents skipped; "
           "pass --go to mutate the board" % HOST)
else:
    reap_dead_claims()

# ---- close work that has been reviewed and passed --------------------------
# A card that produced a candidate and had it independently reviewed and PASSED
# is finished, and nothing moved it. Measured 2026-08-28: ac2e387c carried
# PASS_FOR_REVIEW with an independent review recorded PASS and sat open until a
# human joined the two stores by hand. The evidence for "done" was complete and
# spread across two files, and no code ever read them together.
#
# The join is deliberately strict, and every clause is load-bearing because the
# failure mode here is exposing a FAIL as a PASS:
#   - the parent's own latest outcome must start with PASS
#   - a review card must NAME the parent and be COMPLETE
#   - that review's own latest outcome must be exactly PASS, not PASS_FOR_REVIEW,
#     which only means a candidate is ready for someone else to look at
# A BLOCKED review, or a review that recorded nothing, leaves the parent open.
# Measured on the same board: 2 parents qualified, 3 were held by a BLOCKED
# review and 2 by a silent one, which is the discrimination this is for.
_PASS_ONLY_RE = re.compile(r"^\s*PASS(?!_FOR)", re.I)
_PASS_ANY_RE  = re.compile(r"^\s*PASS", re.I)
_REVIEW_TITLE = "[REVIEW"
_ID_RE = re.compile(r"\b([0-9a-f]{8})\b")

def _reviews_by_parent():
    """Map parent card id -> review card ids that name it."""
    out = {}
    for cd in glob.glob(CARDS + "/*"):
        cid = os.path.basename(cd)
        cp = os.path.join(cd, "core.json")
        if not os.path.exists(cp): continue
        try: core = json.load(open(cp))
        except Exception: continue
        title = str(core.get("title") or "")
        if _REVIEW_TITLE not in title.upper(): continue
        blob = title + " " + str(core.get("description") or "")
        for mm in _ID_RE.finditer(blob):
            pid = mm.group(1)
            if pid != cid:
                out.setdefault(pid, set()).add(cid)
    return out

def close_reviewed_parents():
    """Complete cards whose independent review is complete and PASSED."""
    outcomes = _load_outcomes()
    closed = 0
    for parent, reviews in _reviews_by_parent().items():
        if not os.path.isdir(os.path.join(CARDS, parent)): continue
        if lifecycle_state(parent) != "open": continue
        _pts, pval = outcomes.get(parent, (None, None))
        if not (pval and _PASS_ANY_RE.match(str(pval))): continue
        for rev in sorted(reviews):
            if lifecycle_state(rev) != "complete": continue
            _rts, rval = outcomes.get(rev, (None, None))
            rv = str(rval or "")
            if not rv.strip() or not _PASS_ONLY_RE.match(rv):
                continue          # BLOCKED, or said nothing: the parent stays open
            r = subprocess.run(
                [SKC, "coord", "link", parent, "review_join",
                 "closed on joined evidence: own outcome %s; independent review %s "
                 "is complete with verdict %s" % (str(pval)[:40], rev, rv[:40]),
                 "--agent", "fleet-review-closer"],
                capture_output=True, text=True)
            c = subprocess.run(
                [SKC, "coord", "complete", parent, "--agent", "fleet-review-closer"],
                capture_output=True, text=True)
            if c.returncode == 0:
                _rows.pop(parent, None)
                closed += 1
                log(d, "CLOSED_REVIEWED|%s|%s|review=%s|%s" % (HOST, parent, rev, rv[:40]))
            else:
                log(d, "CLOSE_FAILED|%s|%s|%s" % (HOST, parent, (c.stderr or "").strip()[:110]))
            break
    if closed:
        log(d, "CLOSE_REVIEWED|%s|closed=%d" % (HOST, closed))
    return closed

if not DRY:
    close_reviewed_parents()

_NOT_CLAIMABLE = {"not-claimable", "sprint-container"}
_PINNED_IDS=set()
pool=[]; blocked=0; foreign_skipped=0; skipped_unclaimable=0; skipped_terminal=0; skipped_blocked=0; skipped_review=0; not_claimable_skipped=0; pinned_elsewhere=0
for cd in sorted(glob.glob(CARDS+"/*")):
    cid=os.path.basename(cd)
    core_p=os.path.join(cd,"core.json")
    if not os.path.exists(core_p): continue
    if lifecycle_state(cid) != "open": continue   # LAST state, not "ever claimed"
    if cid in excluded: continue
    if unclaimable(cid): skipped_unclaimable+=1; continue
    if itil_terminal(cid): skipped_terminal+=1; continue
    if blocked_backoff(cid):
        # Split the two, because one of them is success. A card that recorded
        # PASS_FOR_REVIEW is waiting on a reviewer, not refusing to work, and
        # reporting it as blocked hides a finished candidate in the failure count.
        if awaiting_review(cid): skipped_review+=1
        else: skipped_blocked+=1
        continue
    try: core=json.load(open(core_p))
    except: continue
    if core.get("kind") not in ("task","incident","problem"): continue
    title=str(core.get("title") or "")
    labels=folded_labels(cid,core)
    blob=(title+" "+json.dumps(labels)).upper()
    # Match the [HUMAN] TAG, not the word anywhere in the title. The loose test
    # excluded any card whose title merely mentioned humans, which on 2026-08-27
    # was 5 cards, 3 of them ordinary agent work that had been silently skipped:
    #   b7668c11 [SKCP-05F8-ACT][M] Activate approved durable human session envelope
    #   a813e6a0 [QWEN38-POOL-CUTOVER-PACKET-01][REVIEW] Prepare no-action human ...
    #   f0940676 [SKGW-REPLICA-ADOPTION-PACKET][REVIEW] Prepare no-action human ...
    # Those three PREPARE a packet for a human. They are not themselves gates, and
    # a starved pool cannot afford to skip work because of a word in its title.
    if non_implementation(core,labels): continue
    # Cards belonging to a different project that merely share this board. Casey's
    # GREG-BREAK-HOUSE tree is 35 cards and our fleet spent 33 claims on it between
    # 2026-08-26 and 2026-08-27 before anyone noticed it was not our work. The label
    # is deliberately generic rather than a GBH string match, so the next foreign
    # tree can be fenced by labelling it instead of by editing this file.
    if "foreign-project" in {str(item).strip().lower() for item in labels}: foreign_skipped+=1; continue
    # A card the board has explicitly marked unworkable. Sprint containers say so
    # in their own descriptions, e.g. 9535bc80: "Planning and review container
    # only. Do not claim as implementation work." The tags existed and the rotation
    # had never read them. 28 cards carry one.
    #
    # 2b614910 is the consequence: a sprint-container tagged not-claimable that a
    # worker claimed on 2026-08-22 and never released. The Board claim store has no
    # record of it, correctly, so `coord release-claim` answers "Already released"
    # and the CardStore claim cannot be cleared through a supported command at all.
    # The reaper then retried it 455 times.
    #
    # Skipping these stops the fleet spending slots on work it has been told not to
    # do, and stops it manufacturing claims that nothing can release.
    _nc = {str(x).strip().lower() for x in (labels or ())} | \
          {str(x).strip().lower() for x in (core.get("tags") or ())}
    if _NOT_CLAIMABLE & _nc:
        not_claimable_skipped += 1; continue
    if title.startswith("CMDB drift"): continue
    deps=folded_dependencies(cid,core)
    if any(not _dep_satisfied(str(dp)) for dp in deps):
        blocked+=1; continue          # <-- the defect fixed here
    # narrow to the owning host when the card names one that actually runs workers
    _pin = host_pin(core,labels)
    if _pin and _pin != HOST:
        pinned_elsewhere += 1; continue
    if _pin == HOST:
        _PINNED_IDS.add(cid)
    up=title.upper().lstrip("[")
    # lane 0 SKLEGAL (Chef priority, Casey funded and waiting), 1 other engineering,
    # 2 business cards that need founder decisions an agent cannot supply.
    ENG=("SKGW","SKCP","SKCOORD","SKHARNESS","SKMEM","CAPAUTH","FLEET","INC",
         "SKW","QWEN38","CARD-CORE","CARD-EVENT","SKDASH","SKGATEWAY","SKSEC","SKL-")
    if up.startswith("SKLEGAL") or "SKLEGAL" in blob: lane=0
    elif any(up.startswith(e) for e in ENG): lane=1
    else: lane=2
    pool.append([lane,PRI.get(str(core.get("initial_priority")),4),cid,core])

# How many OTHER cards would this card unblock if it completed? A card sitting at
# the head of a dependency chain is worth far more than an isolated one, because
# finishing it converts dependency_blocked cards into assignable work.
unblocks={}
for cd in glob.glob(CARDS+"/*"):
    ocid=os.path.basename(cd)
    cp=os.path.join(cd,"core.json")
    if not os.path.exists(cp): continue
    if lifecycle_state(ocid) in ("complete","void"): continue
    try: oc=json.load(open(cp))
    except: continue
    for dep in folded_dependencies(ocid,oc):
        unblocks[str(dep)]=unblocks.get(str(dep),0)+1
for row in pool: row.append(unblocks.get(row[2],0))
# lane, then most-unblocking first, then priority, then stable id
pool.sort(key=lambda x:(x[0],-x[4],x[1],x[2]))
lc={0:0,1:0,2:0}
for x in pool: lc[x[0]]+=1
top=pool[0][4] if pool else 0
log(d,"POOL|%s|ready=%d sklegal=%d eng=%d biz=%d dep_blocked=%d unclaimable=%d itil_closed=%d blocked_backoff=%d awaiting_review=%d pinned_elsewhere=%d foreign=%d not_claimable=%d top_unblocks=%d"
      %(HOST,len(pool),lc[0],lc[1],lc[2],blocked,skipped_unclaimable,skipped_terminal,skipped_blocked,skipped_review,pinned_elsewhere,foreign_skipped,not_claimable_skipped,top))

# Partition the CARD SPACE by hash, not by pool index. Index striding assumes all
# three hosts see an identical pool at the same instant; ~/.skcapstone is Syncthing
# shared and claims land continuously, so the pools drift and strides collide.
# A hash partition is stable no matter what the local pool looks like.
off={"chiap01":0,"chiap02":1,"chiap03":2}.get(HOST,0)
_NHOST=3
def owns(cid):
    # A host-pinned card is owned by its pinned host, full stop. Letting the hash
    # partition also apply would strand any card whose pin and hash slice disagree:
    # pinned to chiap08 but hashed into chiap02's slice means NO host takes it.
    if cid in _PINNED_IDS:
        return True
    return int(hashlib.sha256(cid.encode()).hexdigest()[:8],16)%_NHOST==off
owned=[x for x in pool if owns(x[2])]

# Never steal another host's hash slice without an authoritative shared lock.
# Syncthing propagation is not a compare-and-swap primitive. Measured 2026-08-28:
# all three hosts stole review card 334b8d63 and claimed it within 350 ms under
# one identity before any claim reached the other hosts. Stable ownership may
# leave capacity idle when the pool is tiny, but it preserves one card, one claim,
# one identity, and one worker. Safe work stealing requires a separate centralized
# admission design.
_ESCALATE_LABEL="needs-stronger-model"

_CAPABILITY_VERDICT_RE = re.compile(
    r"blocked_on[=: |]+\s*capability\b|^\s*BLOCKED\s*\|\s*capability\b", re.I)

def needs_escalation(cid, core=None):
    """True if this card has exhausted the ordinary lanes and needs a stronger model.

    Two ways to qualify, and the second is the one that matters.

    A LABEL is explicit and stays supported, because a human may know a card needs
    the strong model before any worker has tried it.

    A RECORDED CAPABILITY REFUSAL qualifies on its own, with no label. That value
    means "this is solvable and I could not solve it", which is already the exact
    signal the brief asks workers to send, and requiring someone to then hand-apply
    a label to act on it is a routing rule that only works when a person is
    watching. It was not: on 2026-08-28 all 8 capability-blocked cards had been
    handed back to the model that refused them, one of them eight times, because
    nothing converted the signal into a routing decision.
    """
    try: labels=folded_labels(cid, core or {})
    except Exception: labels=[]
    if _ESCALATE_LABEL in {str(x).strip().lower() for x in (labels or [])}:
        return True
    try:
        _ts, _val = _load_outcomes().get(cid, (None, None))
    except Exception:
        return False
    return bool(_ts and _CAPABILITY_VERDICT_RE.search(str(_val or "")))

picks=[]; _i=0
remaining={lane["name"]:lane["free"] for lane in LANES}
lane_order=sorted(LANES,key=lambda lane:0 if lane["name"]=="glm" else 1)
_esc_waiting=0
# Lane affinity, in both directions. An escalation card may go ONLY to the escalate
# lane, because returning it to a lane that already refused it just re-derives the
# same verdict. The escalate lane takes ONLY escalation cards, because the strong
# model is a scarce budget and must not be spent on work the cheap lanes can do.
#
# A card with no compatible free lane is SKIPPED, not allowed to end the loop. The
# earlier version broke out entirely when the head card could not be placed, which
# with lane affinity would let one waiting escalation card starve every ordinary
# card queued behind it.
while _i<len(owned) and len(picks)<MAX_LAUNCH:
    _card=owned[_i]; _i+=1
    _esc=needs_escalation(_card[2], _card[3])
    _lane=None
    card_lane_order=(sorted(LANES,key=lambda lane:0 if lane["name"]=="codex" else 1)
                     if _esc else lane_order)
    for lane in card_lane_order:
        if remaining[lane["name"]]<=0: continue
        _lane=lane; break
    if _lane is None:
        if _esc: _esc_waiting+=1
        continue
    picks.append((_lane,_card)); remaining[_lane["name"]]-=1
if _esc_waiting:
    log(d,"ESCALATE_QUEUED|%s|%d card(s) need the stronger model; escalate lane full"
        %(HOST,_esc_waiting))
if not picks:
    log(d,"NOOP|%s|no dependency-clear cards"%HOST); sys.exit(0)

raced=0
logdir=os.path.join(HOME,".skcapstone/fleet/logs"); os.makedirs(logdir,exist_ok=True)
for _LANE,(_,_,cid,core,_nb) in picks:
    ac="\n".join("  %d. %s"%(i+1,x) for i,x in enumerate(core.get("acceptance_criteria") or []))
    brief=("Work only SKCapstone card %s. The fleet selector has already claimed it "
      "for your exact agent identity. Verify that ownership before working and never "
      "claim or substitute another card. If ownership is absent, or a dependency is "
      "incomplete, say so and stop rather than working it anyway.\n\n"
      "CARD %s (%s)\nTITLE: %s\nDESCRIPTION: %s\n\nACCEPTANCE CRITERIA:\n%s\n\n"
      "CONSTRAINTS (standing rails, non-negotiable):\n"
      "- CardStore is append-only. Build JSON with a serializer and parse every line before appending. Never concatenate strings into JSON.\n"
      "- Join structural CardStore events with separate evidence events. Never infer a verdict from lifecycle state or from links alone.\n"
      "- Return exact PASS, PASS_FOR_REVIEW, or BLOCKED with a real hashed artifact, and notify jarvis and lumina by skmail.\n"
      "- PUBLISH YOUR WORK. Commit to a feature branch, push it, and open a PR. This is\n"
      "  required, not optional: a candidate that exists only in your worktree cannot be\n"
      "  reviewed, is one pull away from being erased, and does not count as done.\n"
      "- Do NOT commit or push to main, and do NOT merge. Landing is a separate decision\n"
      "  a human makes on a reviewed PR.\n"
      "- No deploy, restart, live gateway or config mutation, credential disclosure,\n"
      "  WAKE-02 enablement, live_execution, or automerge.\n"
      "- You may not write a human_signoff or flip repository visibility.\n"
      "- Clean up a worktree or branch ONLY after its work is pushed to a remote.\n"
      "- Start repository work by creating a card-specific branch and git worktree "
      "inside SKFLEET_WORKSPACE. Never modify a shared or live checkout.\n"
      "\n"
      "\n"
      "BLOCKED VERDICT CONTRACT, mandatory whenever your verdict is BLOCKED:\n"
      "Measured on this fleet: 79 of 103 BLOCKED verdicts carried NO machine-readable\n"
      "reason, so nothing could tell a card blocked on a human decision (never re-run)\n"
      "from one blocked on a dependency (re-run when it clears) from one where the\n"
      "worker simply was not capable enough (re-run with a stronger model). Cards were\n"
      "therefore re-assigned every few minutes to re-derive the identical verdict.\n"
      "So a BLOCKED verdict MUST record, as its own evidence link with key blocked_on,\n"
      "exactly one of these four values, plus a referent:\n"
      "  dependency  referent card:<id>      another card must complete first\n"
      "  human       referent approval:<what> only a person can supply this\n"
      "  capability  referent ac:<n> or free  the card is solvable, you could not do it\n"
      "  card        referent ac:<n>          a criterion is unsatisfiable AS WRITTEN\n"
      "Rules that make the value trustworthy:\n"
      "- Cite a referent that EXISTS. A dependency you name is checked against the\n"
      "  card graph; naming one that does not exist or is already complete is treated\n"
      "  as a signal about you, not about the card.\n"
      "- Choose capability honestly when you ran out of context, depth or tool reach.\n"
      "  That is not a failure, it is the signal that routes the card to a stronger\n"
      "  model. Claiming human when you simply could not solve it hides solvable work\n"
      "  forever, which is the worst outcome available to you.\n"
      "- Choose card only when you can quote the criterion and state the contradiction.\n"
      "- Say what you attempted, so the next attempt does not re-pay your discovery.\n"
      "\n"
      "IF YOUR WORK PRODUCES A CANDIDATE SOMEONE ELSE MUST REVIEW, PUBLISH THE BYTES:\n"
      "Recording a commit SHA is NOT publishing. Reviews in this estate are often\n"
      "deliberately run on a DIFFERENT host than the producer, to prove independence.\n"
      "A reviewer on another host cannot see your worktree. Measured consequence: two\n"
      "review cards are permanently unverifiable because commits 229336b2 and 22a36166\n"
      "exist in NO repository on ANY host. The worktree was removed and the candidate\n"
      "went with it, while the recorded SHA survived and points at nothing. Those cards\n"
      "can now only be voided.\n"
      "So if a downstream card must verify what you produced:\n"
      "- Write the candidate itself somewhere durable and SHARED, under\n"
      "  ~/.skcapstone/evidence/work/<card_id>/, not only into a worktree. A patch, a\n"
      "  tarball or the diff text are all fine; the test is whether another host can\n"
      "  read it after your worktree is gone.\n"
      "- Record its sha256 alongside it, so the reviewer verifies identity rather than\n"
      "  trusting a path.\n"
      "- If you push a branch, say so and name it, because a pushed branch IS durable\n"
      "  and reachable from any host. That is the cheapest way to satisfy this.\n"
      "A SHA with no reachable bytes is not evidence. It is a promise that expired.\n"
      "DEFINITION OF DONE, applies to every card that touches a repository:\n"
      "Work is NOT done until it is an open pull request. An edit left uncommitted,\n"
      "or a commit left unpushed, is not delivered: a later pull or checkout by any\n"
      "session sharing that checkout destroys it without trace. Two entire repositories\n"
      "were found emptied on disk this way, and a night of agent work was found sitting\n"
      "untracked in live checkouts. So, in this exact order:\n"
      "1. Branch first. NEVER commit to main or master. Use fix/, feat/ or chore/.\n"
      "2. Commit as soon as the code is written and a fast check passes. Do NOT wait for\n"
      "   a long test run: commit, then run it, then amend or add a follow-up commit.\n"
      "   Never gate a commit on a job you are waiting on.\n"
      "3. Push the branch and open a PR with gh pr create.\n"
      "4. Put the PR URL in your verdict AND in your skmail. A verdict claiming work was\n"
      "   done with no PR URL is incomplete and will be treated as unverified.\n"
      "5. Attribute the commit to yourself, the agent that did the work. Never claim\n"
      "   co-authorship you cannot evidence.\n"
      "6. Clean up: remove scratch files and temp worktrees. Scratch belongs outside the repo.\n"
      "If the card needs no repository change, say so explicitly in your verdict so the\n"
      "absence of a PR is a recorded decision rather than an omission.\n"
      "- Never use an em dash or en dash.\n") % (cid,cid,core.get("kind"),core.get("title"),core.get("description"),ac)
    name="pi-%s-%s-%s"%(_LANE["name"],HOST,cid); sess="%s%s"%(_LANE["prefix"],cid)
    model=ESC_MODEL if _LANE["name"]=="codex" and needs_escalation(cid,core) else _LANE["model"]
    workspace=os.path.join(HOME,".skcapstone/fleet/workspaces",name)
    os.makedirs(workspace,exist_ok=True)
    bf=os.path.join(logdir,"brief-%s.txt"%cid); open(bf,"w").write(brief)
    lf=os.path.join(logdir,"%s-%s.log"%(cid,STAMP))
    # A worker can be terminated by tmux, SSH, or a service cgroup before Pi
    # returns normally. Releasing only after the Pi command leaves a dead claim
    # in that case and drains the assignable pool. Keep worker output separate
    # from lifecycle cleanup so an interrupted zero-output launch does not become
    # false reporting evidence merely because release-claim printed something.
    inner=('release_claim() { %s coord release-claim %s --owner %s --agent %s '
           '>/dev/null 2>&1 || true; }; '
           'trap "release_claim; exit 143" HUP INT TERM; trap release_claim EXIT; '
           'env SKAGENT=%s SKCAPSTONE_AGENT=%s SKFLEET_WORKSPACE=%s %s --approve --name %s '
           '--provider skgateway --model %s --thinking off -p "$(cat %s)" >%s 2>&1; '
           'rc=$?; trap - EXIT HUP INT TERM; release_claim; exit $rc'
           % (SKC,cid,name,name,name,name,workspace,PI,name,model,bf,lf))
    if DRY:
        log(d,"WOULD_LAUNCH|%s|%s|%s|lane=%s|model=%s|%s"%(HOST,sess,cid,_LANE["name"],model,str(core.get("title"))[:40])); continue
    # last-moment re-check: the pool may be a minute old by now
    if not _still_assignable(cid):
        raced += 1
        log(d,"SKIPPED_RACED|%s|%s|%s|another writer finished or claimed it since the pool was built"%(HOST,sess,cid))
        continue
    claim=subprocess.run([SKC,"coord","claim",cid,"--agent",name],capture_output=True,text=True)
    claimed_owner,_claimed_at,claimed_revision=_current_claim_identity_fresh(cid)
    if claim.returncode!=0 or claimed_owner!=name:
        raced += 1
        detail=(claim.stderr or claim.stdout or "claim not visible in CardStore fold").strip()[:140]
        log(d,"CLAIM_FAILED|%s|%s|%s|owner=%s|%s"%(HOST,sess,cid,claimed_owner,detail))
        continue
    r=subprocess.run(["tmux","new-session","-d","-s",sess,"-c",workspace,"bash","-lc",inner])
    ok = r.returncode==0 and sess in sh("tmux","ls","-F","#{session_name}").split()
    launch_identity=_launch_claim_fields(name,claimed_revision,ok)
    launch_action="LAUNCHED" if ok else "LAUNCH_FAILED"
    log(d,"%s|%s|%s|%s|lane=%s|model=%s%s"%
        (launch_action,HOST,sess,cid,_LANE["name"],model,launch_identity))
    if not ok:
        subprocess.run([SKC,"coord","release-claim",cid,"--owner",name,"--agent",name],
                       capture_output=True,text=True)
    time.sleep(2)

# republish after launching, because the first publish is a snapshot of the
# sessions that existed when this tick STARTED. Publishing only there means a host
# never reports the workers it just launched until its next tick, five minutes
# later, so for those five minutes those cards are invisible to every other host.
#
# The reaper reaps on "no host reports this card running". A card nobody reports is
# a card that looks dead, and CLAIM_GRACE alone was carrying the whole burden of
# not acting on that. Observed 2026-08-28: every host published cards=0 while
# chiap01 ran 3 workers and chiap03 ran 2, purely because each had launched them
# after its own publish.
#
# Re-reading tmux here costs one subprocess and closes the window.
try:
    publish_live(sh("tmux","ls","-F","#{session_name}").split())
except Exception as _exc:
    log(d,"WARN|%s|could not republish liveness after launching: %s"%(HOST,_exc))

if raced:
    log(d,"RACED|%s|%d card(s) finished between pool build and launch"%(HOST,raced))
