#!/usr/bin/env python3
"""SKWorld fleet rotation. Keeps N ephemeral codex workers busy on READY cards.

Fixes two defects found 03:50Z:
  1. Cards with INCOMPLETE DEPENDENCIES were being assigned. Workers correctly
     refused with BLOCKED, burned a slot, and produced no work.
  2. Slot accounting counted legacy persistent TUI panes as busy forever, so the
     rotation deadlocked at busy=8 and NOOPed. Workers launched with -p exit on
     their own, so a slot is simply a live codex-auto-* session. No retire logic.
"""
import json,os,glob,subprocess,sys,time,fcntl,datetime,hashlib,collections,re,importlib.util,shlex
import importlib.metadata
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from skcapstone.card_store import CardStore
from skcapstone.fleet.worker_watchdog import StartupObservation, startup_actuation_fenced
from skcapstone.fleet.worker_liveness_runtime import run_production_cycle
from skcapstone.coord_eligibility import leaf_eligibility_counts
from skcapstone.fleet_lane_health import (
    acquire_lane_snapshot,
    cycle_id as new_cycle_id,
    lane_health,
)
from skcapstone.fleet_route_preflight import resolve_and_preflight
from skcapstone.fleet import builder_dispatch, store as fleet_store
from skcapstone.fleet.review_capacity import (
    acquire_review_route_snapshot,
    aggregate_review_capacity,
    choose_review_route,
    eligible_gateway_routes,
    eligible_review_routes,
    load_route_occupancy,
)
from skcapstone.fleet.paths import default_paths as default_fleet_paths
from skcapstone.fleet.rotation_lock import acquire_rotation_lock
from skcapstone.scheduler_decision import (
    SchedulerFacts,
    classify_scheduler_population,
    pool_v2,
)
from skcapstone.review_admission import (
    governed_review_gate_reasons,
    governed_review_seat,
    qualified_reviewer_seats,
)
from skcapstone.fleet.review_pool import elastic_reviewer_identity, review_fanout_limit
from skcapstone.estate import (
    EstateConfigError,
    estate_authority_host,
    estate_rotation_hosts,
)
from skcapstone.seat_boundaries import BoundaryError
from skcapstone.niobe_fanout import (
    FanoutBoundaryError,
    append_fanout_receipt,
    pending_fanout_request,
    reconcile_fanout_receipt,
)
from skcapstone.seat_runtime import (
    MeroObservation,
    append_review_launch_receipt,
    authorize_review_launch,
    recommend_reviewer,
    review_state_revision,
)

def _required_lane_target(name, env=None, default=None):
    values = os.environ if env is None else env
    try:
        value = int(values.get(name, default))
    except (TypeError, ValueError):
        value = -1
    if value < 0:
        raise SystemExit(
            "BLOCKED|%s|missing or invalid non-negative integer" % name
        )
    return value


def _estate_rotation_hosts(home=None):
    """Return the worker fleet this estate declares, or None when it declares none.

    An estate with no record at all is an ordinary state (a fresh checkout, a
    test host, a node whose synced tree has not landed yet), so silence is
    silence and the caller keeps its own default. A record that exists and is
    WRONG is not silence: it is refused here rather than quietly partitioning
    the fleet across a roster nobody declared.
    """
    try:
        return estate_rotation_hosts(home)
    except EstateConfigError as exc:
        raise SystemExit("BLOCKED|rotation_hosts|%s" % exc)


def _resolve_rotation_hosts(env=None, declared=None,
                            default=("chiap01","chiap02","chiap03","chiap04","chiap08")):
    """Return the worker hosts this rotation may dispatch to, in partition order.

    Ownership is a hash of the card id modulo this tuple, so the tuple's order
    is as load bearing as its contents. Resolution is most explicit first:
    SKFLEET_ROTATION_HOSTS for a host bootstrapping before its estate record
    has synced, then the estate's declared roster, then the default. The
    default is the chi fleet exactly as it was when it was a literal in this
    file, so an estate that declares nothing partitions every card to the same
    host it did before this was configurable.
    """
    values = os.environ if env is None else env
    raw = str(values.get("SKFLEET_ROTATION_HOSTS", "") or "").strip()
    if raw:
        hosts = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
        source = "SKFLEET_ROTATION_HOSTS"
    elif declared:
        hosts = tuple(str(part).strip().lower() for part in declared)
        source = "estate rotation_hosts"
    else:
        return tuple(default)
    if (not hosts or len(set(hosts)) != len(hosts)
            or not all(re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", host) for host in hosts)):
        raise SystemExit(
            "BLOCKED|%s|rotation hosts must be unique well formed host names" % source
        )
    return hosts


def _estate_authority_host(home=None):
    """Return the reconciliation publisher this estate declares, or None.

    Same contract as :func:`_estate_rotation_hosts`, for the same reason: an
    estate with no record at all is an ordinary state, so silence is silence
    and the caller keeps its own default, while a record that exists and is
    WRONG is refused here rather than quietly electing the wrong publisher.
    """
    try:
        return estate_authority_host(home)
    except EstateConfigError as exc:
        raise SystemExit("BLOCKED|authority_host|%s" % exc)


def _resolve_authority_host(env=None, declared=None, default="chiap08"):
    """Return the one host that may publish this estate's shared reports.

    Several fleet outputs are shared, single-writer files: the full lifecycle
    reassessment report, automatic parent closeout, and finished review claim
    release. Every rotation host computes the same answer from the same shared
    cards, so letting all of them write is N-way contention over identical
    bytes, and on a Syncthing folder that produces conflict copies rather than
    content. Exactly one host publishes and the rest read.

    Resolution is most explicit first, matching _resolve_rotation_hosts:
    SKFLEET_AUTHORITY_HOST for a host bootstrapping before its estate record
    has synced, then the estate's declared authority_host, then the default.
    The default is the chi estate's publisher exactly as it was when it was a
    literal in this file, so an estate that declares nothing publishes from the
    same host it did before this was configurable.
    """
    values = os.environ if env is None else env
    raw = str(values.get("SKFLEET_AUTHORITY_HOST", "") or "").strip()
    if raw:
        host, source = raw.lower(), "SKFLEET_AUTHORITY_HOST"
    elif declared:
        host, source = str(declared).strip().lower(), "estate authority_host"
    else:
        return default
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", host):
        raise SystemExit(
            "BLOCKED|%s|authority host must be one well formed host name" % source
        )
    return host


def _slot_summary(lanes):
    slots = " ".join(
        "%s=%d/%d" % (lane["name"], len(lane["busy"]), lane["target"])
        for lane in lanes
    )
    return "%s|total_free=%d" % (slots, sum(lane["free"] for lane in lanes))


def _bounded_ids(card_ids, limit=12):
    """Return deterministic, bounded card IDs suitable for one log record."""
    values = sorted({str(card_id) for card_id in card_ids})
    shown = values[:limit]
    return ",".join(shown) or "-", max(0, len(values) - len(shown))


def _full_reassessment_path(host, evidence_root, authority_host=None):
    """Keep exactly one shared full report, written only by its authority host.

    Every rotation host assesses the same shared card store, so N hosts writing
    one Syncthing-backed file is N-way write contention over identical bytes.
    Exactly one host publishes it and the rest read it. WHICH host that is, is
    estate configuration and not a property of this code, so it is passed in
    rather than compared against a literal hostname.

    Args:
        host: Name of the host running this rotation.
        evidence_root: Directory holding the estate's shared evidence files.
        authority_host: Host authorized to publish the shared report. ``None``
            means the estate named no publisher, so no host writes it.

    Returns:
        The report path on the authority host, otherwise ``None``.
    """
    if not authority_host or host != authority_host:
        return None
    return Path(evidence_root) / "lifecycle-reassessment.json"


def _validate_reassessment(report):
    """Fail closed if the lifecycle assessor did not return its safety contract."""
    if not isinstance(report, dict):
        raise ValueError("lifecycle reassessment is not an object")
    if report.get("read_only") is not True:
        raise ValueError("lifecycle reassessment is not read only")
    if not isinstance(report.get("classes"), dict):
        raise ValueError("lifecycle reassessment classes are absent")
    if not isinstance(report.get("counts"), dict):
        raise ValueError("lifecycle reassessment counts are absent")
    if not isinstance(report.get("excluded_card_ids"), list):
        raise ValueError("lifecycle reassessment exclusions are absent")
    if not re.fullmatch(r"[0-9a-f]{64}", str(report.get("content_sha256") or "")):
        raise ValueError("lifecycle reassessment hash is absent")
    return report


def _reassessment_summary(host, report, report_path, authority_host=None):
    """Return one bounded log line naming where the full report actually lives."""
    destination = (
        str(report_path) if report_path is not None
        else "authority:%s" % (authority_host or "unset")
    )
    counts = json.dumps(report["counts"], sort_keys=True, separators=(",", ":"))
    return "REASSESSMENT|%s|report=%s sha256=%s counts=%s excluded=%d" % (
        host, destination, report["content_sha256"], counts,
        len(report["excluded_card_ids"]),
    )


def _write_bounded_report(report, report_path, limit=2 * 1024 * 1024):
    """Atomically replace the authority report with the exact bounded bytes."""
    payload = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()
    json.loads(payload)
    if len(payload) > limit:
        raise ValueError("lifecycle reassessment exceeds %d bytes" % limit)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_bytes(payload)
    json.loads(temporary.read_bytes())
    temporary.replace(report_path)


def _partition_owner(card_id, hosts, pinned_host=None):
    """Return the unique stable owner for one card across host snapshots."""
    if pinned_host:
        return pinned_host
    index = int(hashlib.sha256(str(card_id).encode()).hexdigest()[:8], 16) % len(hosts)
    return hosts[index]


def _selection_diagnostic(pool, owned, lanes, owner_for, host_capacity=None):
    """Classify why an authoritative pool produced no local selection.

    This is diagnostic only. It never changes ownership or claimability, so the
    authoritative claim, post-claim readback, and duplicate guards remain the
    admission mechanism.
    """
    pool_ids = [row[2] for row in pool]
    owned_ids = [row[2] for row in owned]
    total_target = sum(int(lane.get("target", 0)) for lane in lanes)
    total_free = sum(int(lane.get("free", 0)) for lane in lanes)
    if not pool:
        reason, ids = "empty-pool", []
    elif total_target == 0:
        reason, ids = "zero-target", owned_ids or pool_ids
    elif not owned:
        reason, ids = "foreign-hash-partition", pool_ids
    else:
        reason, ids = "no-compatible-lane", owned_ids
    bounded, omitted = _bounded_ids(ids)
    owners = collections.Counter(owner_for(card_id) for card_id in pool_ids)
    owner_counts = ",".join(
        "%s:%d" % (owner, owners[owner]) for owner in sorted(owners)
    ) or "-"
    capacity = host_capacity or {}
    owner_free = ",".join(
        "%s:%d" % (owner, int(capacity.get(owner, 0))) for owner in sorted(owners)
    ) or "-"
    return (
        "reason=%s pool=%d owned=%d target=%d free=%d ids=%s omitted=%d "
        "owners=%s owner_free=%s"
        % (reason, len(pool), len(owned), total_target, total_free, bounded,
           omitted, owner_counts, owner_free)
    )


def _card_process_snapshot(cid):
    """Return a fresh bounded same-card local process snapshot."""
    suffix = "-" + str(cid)
    units = []
    try:
        result = subprocess.run(
            ["systemctl", "--user", "list-units", "--type=service", "--state=running",
             "--no-legend", "--plain"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise OSError("systemd unit query failed")
        units = sorted(
            fields[0] for line in result.stdout.splitlines()
            if (fields := line.split()) and fields[0].endswith(suffix + ".service")
        )
    except (OSError, subprocess.TimeoutExpired):
        # Failure to query systemd is not proof that a worker is absent.
        units = ["systemd-query-failed"]
    return {
        "sessions": sorted(
            session
            for session in sh("tmux", "ls", "-F", "#{session_name}").split()
            if session.endswith(suffix)
        ),
        "units": units,
    }


def _governed_review_metadata(core, labels):
    """Return complete producer evidence for an explicitly labeled review."""
    if "review" not in {str(label).strip().lower() for label in labels}:
        return None
    links = core.get("links") if isinstance(core.get("links"), dict) else {}
    meta = core.get("meta") if isinstance(core.get("meta"), dict) else {}
    typed_producer = links.get("producer_identity") or meta.get("producer_identity")
    typed_evidence = links.get("candidate_evidence_sha256") or meta.get(
        "candidate_evidence_sha256"
    )
    if typed_producer is not None or typed_evidence is not None:
        producer = str(typed_producer or "").strip()
        evidence = str(typed_evidence or "").strip().lower()
        if not producer or not re.fullmatch(r"[0-9a-f]{64}", evidence):
            return None
    else:
        description = str(core.get("description") or "")
        producer_match = re.search(r"Producer identity:\s*([^.]*)\.", description)
        evidence_match = re.search(r"sha256=([0-9a-f]{64})(?:\.|\s|$)", description)
        if not producer_match or not producer_match.group(1).strip() or not evidence_match:
            return None
        producer = producer_match.group(1).strip()
        evidence = evidence_match.group(1)
    source = str(links.get("link_source_card") or meta.get("link_source_card") or "").strip()
    head = str(links.get("link_head_revision") or meta.get("link_head_revision") or "").strip()
    if not source or not re.fullmatch(r"[0-9a-f]{40}", head):
        return None
    return producer, evidence


def _review_assignment(cid, core, labels, reviewer):
    """Return Link's governed reviewer and recommendation for a review card."""
    from skcapstone.seat_boundaries import canonical_principal

    if "review" not in {str(label).strip().lower() for label in labels}:
        return reviewer, None, None
    metadata = _governed_review_metadata(core, labels)
    if metadata is None:
        raise BoundaryError("review card lacks complete producer evidence metadata")
    producer, evidence = metadata
    if canonical_principal(producer) == canonical_principal(reviewer):
        raise BoundaryError("producer and reviewer resolve to the same principal")
    card = CardStore(Path(HOME) / ".skcapstone").fold(cid)
    if card is None:
        raise BoundaryError("review card is missing")
    state_revision = review_state_revision(card)
    observed_process = _card_process_snapshot(cid)
    if any(observed_process.values()):
        raise BoundaryError("review card already has a live same-card process")
    recommendation = recommend_reviewer(
        Path(HOME) / ".skcapstone",
        card_id=cid,
        recommendation_id=None,
        author=producer,
        candidates=[reviewer],
        observed_process=observed_process,
        evidence_sha256=evidence,
        expected_state_revision=state_revision,
    )
    live_claim_revision = str(_current_claim_identity_fresh(cid)[2] or "")
    handoff = authorize_review_launch(
        Path(HOME) / ".skcapstone",
        recommendation,
        actor=reviewer,
        current_process=_card_process_snapshot(cid),
        used_recommendation_ids={
            str(event.get("recommendation_id"))
            for event in event_rows(cid)
            if event.get("action") == "review_assignment_launch"
            and event.get("launched")
            and event.get("recommendation_id")
            and str(event.get("claim_revision") or "") == live_claim_revision
        },
    )
    return handoff.reviewer, recommendation, handoff


# Load this dependency-free module directly so the system Python job does not
# initialize optional skcoord API dependencies such as CapAuth.
_LIFECYCLE_PATH=Path(os.environ.get("SKCOORD_SRC",os.path.join(os.path.expanduser("~"),"work/skcoord/src")))/"skcoord/lifecycle_reassessment.py"
_spec=importlib.util.spec_from_file_location("skcoord_lifecycle_reassessment",_LIFECYCLE_PATH)
# Assessment is a safety input, not optional telemetry. If it cannot be loaded,
# the cycle still emits a BLOCKED summary below but gains no mutation authority.
_LIFECYCLE_OK = _spec is not None and _spec.loader is not None and _LIFECYCLE_PATH.exists()
if _LIFECYCLE_OK:
    try:
        _lifecycle=importlib.util.module_from_spec(_spec)
        sys.modules[_spec.name]=_lifecycle
        _spec.loader.exec_module(_lifecycle)
        assess=_lifecycle.assess
    except Exception as _e:
        _LIFECYCLE_OK=False
        print("  WARN lifecycle reassessment unavailable (%s)" % _e)
if not _LIFECYCLE_OK:
    assess=None

HOST=os.uname().nodename
# The worker fleet is ESTATE configuration, not a property of this script. Card
# ownership is partitioned by hashing across this tuple, so its membership and
# its order together decide which host may claim which card. An estate names its
# own roster once, as rotation_hosts in its estate record (config/estate.json,
# else cluster.json), or through SKFLEET_ROTATION_HOSTS on a host that is
# bootstrapping before that record has synced. Declaring nothing keeps the chi
# fleet this file has always carried, unchanged in value and in order.
ROTATION_HOSTS=_resolve_rotation_hosts(declared=_estate_rotation_hosts())
# The reconciliation publisher is the same kind of estate configuration, so it
# is declared the same way and resolved in the same order: authority_host in the
# estate record, SKFLEET_AUTHORITY_HOST for a host bootstrapping ahead of it.
AUTHORITY_HOST=_resolve_authority_host(declared=_estate_authority_host())
SKC=os.path.expanduser("~/.skenv/bin/skcapstone")
TARGET=_required_lane_target("SKFLEET_TARGET")
GLM_TARGET=_required_lane_target("SKFLEET_GLM_TARGET")
QWEN_TARGET=_required_lane_target("SKFLEET_QWEN_TARGET", default="6")
KIMI_TARGET=_required_lane_target("SKFLEET_KIMI_TARGET", default="0")
MAX_LAUNCH=int(os.environ.get("SKFLEET_MAX_LAUNCH","11"))
MAX_CANDIDATE_SCAN=max(
    MAX_LAUNCH,
    int(os.environ.get("SKFLEET_MAX_CANDIDATE_SCAN",str(MAX_LAUNCH*8))),
)
ONLY_SEAT=os.environ.get("SKFLEET_ONLY_SEAT","").strip().lower()
SEAT_TARGET=_required_lane_target("SKFLEET_SEAT_TARGET", default="0")
CODEX_PHYSICAL_LIMIT=_required_lane_target(
    "SKFLEET_CODEX_PHYSICAL_LIMIT", default=str(TARGET))
REVIEW_MAXIMUM=_required_lane_target("SKFLEET_REVIEW_MAXIMUM", default="2")
_SEAT_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
DRY = "--go" not in sys.argv
HOME=os.path.expanduser("~")
CARDS=os.path.join(HOME,".skcapstone/cards")
EVID=os.path.join(HOME,".skcapstone/evidence/fleet-rotation")
PI="/home/skuser01/.npm-global/bin/pi"
PI_CARDSTORE_GUARD=os.environ.get(
    "SKFLEET_PI_CARDSTORE_GUARD",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi-cardstore-guard.mjs"),
)
if not os.path.isfile(PI_CARDSTORE_GUARD):
    raise SystemExit("BLOCKED|missing Pi CardStore write guard: %s" % PI_CARDSTORE_GUARD)
PI_NATIVE_TOOLS=("read", "bash", "edit", "write", "grep", "find", "ls")
PI_MCP_PROXY_LABEL="mcp-required"
ESC_MODEL=os.environ.get("SKFLEET_ESC_MODEL","gpt-5.6-sol")
PRI={"critical":0,"high":1,"medium":2,"low":3}
STAMP=os.environ.get("SKFLEET_ROTATION_ID", "")
if not re.fullmatch(r"[0-9a-f]{32}", STAMP):
    STAMP=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ensure_runtime_console_path(executable=sys.executable, environ=os.environ):
    """Expose console scripts installed beside the running Python."""
    runtime_bin = os.path.dirname(os.path.abspath(executable))
    entries = [entry for entry in environ.get("PATH", "").split(os.pathsep) if entry]
    if runtime_bin not in entries:
        environ["PATH"] = os.pathsep.join([runtime_bin, *entries])
    return os.path.join(runtime_bin, "skcapstone")


# The timer driven production entrypoint always traverses the liveness decision
# surface. Empty or incomplete evidence still publishes truthful zero metrics
# and grants no assistance, reconciliation, or retirement authority.
_ensure_runtime_console_path()
run_production_cycle(agent=os.environ.get("SKAGENT", "skfleet-rotate"))

def sh(*a): return subprocess.run(a,capture_output=True,text=True).stdout

_WORKER_UNIT_RE = re.compile(
    r"^skfleet-worker-(codex|glm|qwen|kimi|escalate)-([0-9a-f]{8})\.service$"
)


def _worker_unit_name(lane, cid):
    """Return the transient service name for one newly launched worker."""
    if lane not in {"codex", "glm", "qwen", "kimi", "escalate"} or not re.fullmatch(
        r"[0-9a-f]{8}", cid
    ):
        raise ValueError("invalid worker unit identity")
    return "skfleet-worker-%s-%s.service" % (lane, cid)


def _parse_worker_units(output):
    """Return active worker unit identities from systemctl list-units output."""
    found = []
    for line in output.splitlines():
        fields = line.split()
        match = _WORKER_UNIT_RE.fullmatch(fields[0]) if fields else None
        if match:
            found.append({"unit": fields[0], "lane": match.group(1), "card": match.group(2)})
    return found


def active_worker_units():
    """Read systemd-owned workers without disturbing migration-era tmux workers."""
    output = sh(
        "systemctl", "--user", "list-units", "--type=service", "--state=running",
        "--no-legend", "--plain", "skfleet-worker-*.service"
    )
    return _parse_worker_units(output)


def _worker_launch_command(unit, workspace, inner):
    """Build the systemd-supported detached worker launch command."""
    # Keep a legacy string caller compatible, but never round-trip the real
    # wrapper argv through shlex.join/split.  The child script contains shell
    # function declarations and must remain one argument to bash -lc.
    child_argv = ["bash", "-lc", inner] if isinstance(inner, str) else list(inner)
    return [
        "systemd-run", "--user", "--quiet", "--collect", "--service-type=exec",
        "--unit", unit, "--property=KillMode=control-group",
        "--working-directory", workspace, *child_argv,
    ]


def _resolve_workspace_root(root):
    """Resolve a configured workspace root to one unambiguous Git checkout."""
    candidate = Path(root).expanduser()
    if not candidate.is_dir():
        raise ValueError("workspace root is missing or not a directory")
    if (candidate / ".git").exists():
        return str(candidate)
    repositories = sorted(
        child
        for child in candidate.iterdir()
        if child.is_dir() and (child / ".git").exists()
    )
    if len(repositories) != 1:
        raise ValueError(
            "workspace root must contain exactly one Git checkout; "
            f"found {len(repositories)}"
        )
    return str(repositories[0])


def _worker_workspace(default):
    """Use an explicitly configured checkout only when it resolves uniquely."""
    configured = os.environ.get("SKFLEET_WORKSPACE")
    return _resolve_workspace_root(configured) if configured else default


def _source_workspace_spec(core, labels):
    """Return the authenticated source binding required by a source card."""
    normalized = {str(label).strip().lower() for label in labels}
    if "source-only" not in normalized:
        return None
    links = core.get("links") if isinstance(core.get("links"), dict) else {}
    meta = core.get("meta") if isinstance(core.get("meta"), dict) else {}
    link_repository = str(links.get("repository") or "").strip()
    meta_repository = str(meta.get("repository") or "").strip()
    if link_repository and meta_repository and link_repository != meta_repository:
        raise ValueError("source binding conflict: repository")
    repository = link_repository or meta_repository
    link_ref = str(links.get("base_ref") or "").strip()
    meta_ref = str(meta.get("base_ref") or meta.get("named_base_ref") or "").strip()
    link_revision = str(links.get("base_revision") or "").strip().lower()
    meta_revision = str(meta.get("base_revision") or "").strip().lower()
    if link_revision and meta_revision and link_revision != meta_revision:
        raise ValueError("source binding conflict: base_revision")
    base_revision = link_revision or meta_revision
    legacy_revision = link_ref.lower() if re.fullmatch(r"[0-9a-fA-F]{40}", link_ref) else ""
    if legacy_revision:
        if base_revision and base_revision != legacy_revision:
            raise ValueError("legacy SHA base_ref conflicts with base_revision")
        base_revision = legacy_revision
        base_ref = meta_ref if meta_ref and meta_ref.lower() != legacy_revision else ""
    else:
        if link_ref and meta_ref and link_ref != meta_ref:
            raise ValueError("source binding conflict: base_ref")
        base_ref = link_ref or meta_ref
    parsed = urlsplit(repository)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "source card requires a credential-free https repository link"
        )
    if not base_ref and legacy_revision:
        raise ValueError("legacy SHA base_ref requires an available named base_ref")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", base_ref) or base_ref.startswith("-"):
        raise ValueError("source card requires a bounded base_ref link")
    if not re.fullmatch(r"[0-9a-f]{40}", base_revision):
        raise ValueError("source card requires an exact 40-hex base_revision")
    return repository, base_ref, base_revision


def _normalize_credential_free_https_remote(url):
    """Return a normalized credential-free HTTPS remote URL, or None."""
    parsed = urlsplit(str(url or "").strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    path = parsed.path.rstrip("/").removesuffix(".git")
    netloc = parsed.hostname.lower()
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return f"https://{netloc}{path}"


def _select_matching_source_remote(path, repository, runner=subprocess.run):
    """Return the unique remote whose credential-free URL matches repository."""
    expected = _normalize_credential_free_https_remote(repository)
    if expected is None:
        raise ValueError("workspace repository does not match card binding")
    result = runner(
        ["git", "-C", str(path), "config", "--get-regexp", r"^remote\..*\.url$"],
        capture_output=True,
        text=True,
    )
    lines = result.stdout.splitlines() if result.returncode == 0 else []
    matches = []
    credential_matches = False
    for line in lines:
        key, sep, url = line.partition(" ")
        if not sep or not (key.startswith("remote.") and key.endswith(".url")):
            continue
        name = key[len("remote.") : -len(".url")]
        normalized = _normalize_credential_free_https_remote(url)
        if normalized == expected:
            matches.append(name)
            continue
        parsed = urlsplit(str(url or "").strip())
        if parsed.scheme != "https" or not parsed.hostname:
            continue
        if not (parsed.username or parsed.password or parsed.query or parsed.fragment):
            continue
        # Reason: strip only enough to detect a credential-bearing alias of the
        # card binding; never accept that remote as a match.
        host = parsed.hostname.lower()
        if parsed.port:
            host = f"{host}:{parsed.port}"
        stripped = f"https://{host}{parsed.path.rstrip('/').removesuffix('.git')}"
        if stripped == expected:
            credential_matches = True
    if credential_matches:
        raise ValueError("workspace remote URL must be credential-free")
    if not matches:
        raise ValueError("workspace repository does not match card binding")
    if len(matches) > 1:
        raise ValueError("workspace repository remote match is ambiguous")
    return matches[0]


def _verify_source_workspace(path, repository, base_ref, base_revision,
                             checkout=False, runner=subprocess.run):
    """Fetch a named ref and verify one clean checkout at its exact revision."""
    def run(command, name):
        kwargs = {"capture_output": True, "text": True}
        if "fetch" in command:
            kwargs["env"] = dict(
                os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/false"
            )
        result = runner(command, **kwargs)
        if result.returncode != 0:
            raise ValueError(f"workspace {name} verification failed")
        return result.stdout.strip()

    remote = _select_matching_source_remote(path, repository, runner=runner)
    if not checkout and run(
        ["git", "-C", str(path), "status", "--porcelain=v1"], "clean"
    ):
        raise ValueError("workspace contains uncommitted custody state")
    run(
        ["git", "-C", str(path), "fetch", "--quiet", remote, base_ref],
        "fetch",
    )
    run(
        ["git", "-C", str(path), "merge-base", "--is-ancestor",
         base_revision, "FETCH_HEAD"],
        "base_revision reachability",
    )
    if checkout:
        run(
            ["git", "-C", str(path), "checkout", "--quiet", "--detach", base_revision],
            "exact base_revision checkout",
        )
        if run(["git", "-C", str(path), "status", "--porcelain=v1"], "clean"):
            raise ValueError("workspace contains uncommitted custody state")
    head = run(["git", "-C", str(path), "rev-parse", "HEAD^{commit}"], "head")
    exact = run(
        ["git", "-C", str(path), "rev-parse", f"{base_revision}^{{commit}}"],
        "base_revision",
    )
    if not head or head != exact:
        raise ValueError("workspace HEAD does not match exact base_revision")


def _preclaim_source_ref(repository, base_ref, base_revision, runner=subprocess.run):
    """Check reconstructability before creating a reviewer workspace."""
    candidates = [base_ref] if base_ref.startswith("refs/") else [
        f"refs/heads/{base_ref}", f"refs/tags/{base_ref}"
    ]
    result = runner(
        ["git", "ls-remote", "--exit-code", repository, *candidates],
        capture_output=True, text=True,
        env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/false"))
    if result.returncode != 0:
        raise ValueError("reconstructability_blocked: exact source ref absent from credential-free remote")
    refs = {line.split("\t", 1)[1] for line in result.stdout.splitlines() if "\t" in line}
    if len(refs) != 1:
        raise ValueError("reconstructability_blocked: base_ref is missing or ambiguous")


def _materialize_worker_workspace(default, core, labels, runner=subprocess.run):
    """Materialize one source checkout atomically before a worker is claimed."""
    configured = os.environ.get("SKFLEET_WORKSPACE")
    spec = _source_workspace_spec(core, labels)
    if configured:
        checkout = _resolve_workspace_root(configured)
        if spec is not None:
            _verify_source_workspace(checkout, *spec, runner=runner)
        return checkout
    if spec is None:
        os.makedirs(default, exist_ok=True)
        return default
    repository, base_ref, base_revision = spec
    target = Path(default)
    if target.exists() and any(target.iterdir()):
        checkout = _resolve_workspace_root(target)
        _verify_source_workspace(
            checkout, repository, base_ref, base_revision, runner=runner
        )
        return checkout
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.materializing-{os.getpid()}")
    if temporary.exists():
        raise ValueError("workspace materialization temporary path already exists")
    try:
        clone_command = [
            "git", "clone", "--quiet", "--no-checkout", "--single-branch",
            "--branch", base_ref,
        ]
        clone_command.extend(["--", repository, str(temporary)])
        result = runner(
            clone_command,
            capture_output=True,
            text=True,
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/false"),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "git clone failed").strip()
            raise ValueError(f"workspace materialization failed: {detail[:160]}")
        _resolve_workspace_root(temporary)
        _verify_source_workspace(
            temporary, repository, base_ref, base_revision, checkout=True, runner=runner
        )
        if target.exists():
            target.rmdir()
        temporary.replace(target)
        return str(target)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def pi_tool_allowlist(labels):
    """Return the bounded Pi tool surface for one fleet card."""
    tools = list(PI_NATIVE_TOOLS)
    if PI_MCP_PROXY_LABEL in {str(label).strip().lower() for label in labels}:
        tools.append("mcp")
    return ",".join(tools)


def _lane_busy(lane, sessions, units):
    """Count old tmux and new service workers during the migration window."""
    legacy = [s for s in sessions if s.startswith(lane["prefix"])]
    managed = [u["unit"] for u in units if u["lane"] == lane["name"]]
    return legacy + managed


def _worker_cards(sessions, units, lanes):
    """Return cards represented by either migration-era worker form."""
    return sorted(
        {s[len(lane["prefix"]):] for lane in lanes
         for s in sessions if s.startswith(lane["prefix"])}
        | {unit["card"] for unit in units}
    )


def _seat_capacity(seat, seat_target, physical_limit, busy_cards, owner_for):
    """Return isolated seat capacity bounded by seat and physical limits."""
    if not seat:
        return None
    seat_busy = sum(
        str(owner_for(card_id) or "").startswith("pi-%s-" % seat)
        for card_id in busy_cards
    )
    return min(
        max(0, int(seat_target) - seat_busy),
        max(0, int(physical_limit) - len(busy_cards)),
    )


def _last_claim_owner(cid):
    """Return the latest claim owner used by an active worker card."""
    for event in reversed(event_rows(cid)):
        if event.get("action") == "claim":
            return event.get("owner") or event.get("actor")
    return None


def _noop_reason(pool, owned, lane_deferred):
    """Classify a zero-launch cycle without treating an honest no-op as failure."""
    if not pool or not owned:
        return "no_eligible_work"
    if lane_deferred and all(
        reason.startswith(("no-free-lane:", "no-compatible-healthy-lane:"))
        for reason in lane_deferred
    ):
        return "no_available_capacity"
    return "no_eligible_work"


def _worker_mail_routing(environ=os.environ, owning_agent=None):
    """Return Jarvis/configured recipients plus the card's owning agent."""
    raw = environ.get("SKFLEET_MAIL_RECIPIENTS")
    requested = ("jarvis",) if raw is None else tuple(
        value.strip().lower() for value in raw.split(",") if value.strip()
    )
    if not requested:
        requested = ("jarvis",)
    excluded = {
        value.strip().lower()
        for value in environ.get("SKFLEET_EXCLUDED_MAIL_RECIPIENTS", "").split(",")
        if value.strip()
    }
    excluded.add("lumina")
    if owning_agent:
        requested += (str(owning_agent).strip().lower(),)
    allowed = tuple(dict.fromkeys(
        value for value in requested
        if value not in excluded and value != "all"
        and re.fullmatch(r"[a-z0-9][a-z0-9-]*", value)
    ))
    if not allowed:
        raise ValueError("worker mail routing has no allowed recipient")
    return allowed


def _worker_mail_instructions(recipients):
    """Build the worker brief fragment from the exact allowed recipients."""
    allowed = ", ".join(recipients)
    review_contact = recipients[-1]
    return (
        "- Return exact PASS, PASS_FOR_REVIEW, or BLOCKED with a real hashed "
        "artifact, and notify %s by skmail.\n"
        "\n"
        "HOW TO SEND MAIL. This is the ONLY mailbox. Use the command; do not invent a\n"
        "file format or a directory:\n"
        "    skmail send <you> <to> <urgent|normal|fyi> \"<subject>\" \"<body>\"\n"
        "  <you> is your own agent name, the value of SKAGENT, expanded, not the literal.\n"
        "  <to> must be one of: %s. Never broadcast to all.\n"
        "  Read your own mail with:  skmail read <you>     recent traffic:  skmail tail\n"
        "HOW TO CHAT OR GET HELP. SKMail is asynchronous worker chat. Ask %s for\n"
        "coordination, review, evidence, or help. Check for replies with skmail read\n"
        "\"$SKAGENT\" and skmail tail 20, then run skmail ack \"$SKAGENT\" after\n"
        "processing the reply.\n"
        % (allowed, allowed, review_contact)
    )


def _seraph_terminal_noop(
    host, only_seat, dry, pick_count, processed_picks, launch_receipts
):
    """Return one receipt only when every selected live Seraph pick was suppressed."""
    if (
        only_seat == "seraph"
        and not dry
        and pick_count
        and processed_picks == pick_count
        and launch_receipts == 0
    ):
        return "NOOP_RECEIPT|%s|reason=all_candidates_suppressed|seat=seraph" % host
    return None


def _coord_task_claimable(core):
    """Return whether the task-only coord claim command accepts this card kind."""
    return core.get("kind") == "task"

def _classify_claim_outcome(still_assignable, returncode=None,
                            claimed_owner=None, expected_owner=None):
    """Classify a final assignability check and the following claim result."""
    if not still_assignable:
        return "raced"
    if returncode is None:
        return "ready"
    if returncode != 0 or claimed_owner != expected_owner:
        return "claim_refused"
    return "claimed"

# ---- exact-card worker admission (card d11ca7ed) ---------------------------
# Duplicate exact-card admission was observed twice: c32a1001 (two Pi PIDs
# simultaneously ran one card under one owner in one worktree) and 0d7331e7
# (a fleet worker unit launched for a card whose authoritative owner,
# cursor-w105-focus, already existed). rotate.lock serializes whole rotations
# and systemd unit names are unique per lane, but nothing stopped two live
# workers for the SAME card from coexisting across lanes or across launcher
# generations. This is the host-safe atomic admission gate: one receipt per
# exact card, bound to card ID, claim revision, normalized worktree, and
# worker owner, acquired AFTER the claim wins and BEFORE the worker process
# is created. A second launcher fails before process creation and emits a
# deterministic, non-secret receipt naming the live holder. Stale recovery
# requires fresh proof that no matching process, unit, or session identity is
# still alive.
#: An empty or partially written receipt stays "in flight" for this long
#: before a contender may treat it as stale; stealing the create-to-write
#: window admitted both launchers in the d11ca7ef race analysis.
_ADMISSION_WRITE_WINDOW_S = 2.0
_ADMISSION_WRITE_POLL_S = 0.05


def _admission_lock_path(home, cid):
    """Return the per-card admission receipt path for one card id."""
    return os.path.join(home, ".skcapstone", "fleet", "admission", "%s.json" % cid)


def _release_card_admission(home, cid):
    """Remove one admission receipt; a missing file is not an error."""
    try:
        os.unlink(_admission_lock_path(home, cid))
    except OSError:
        pass


def _read_admission_receipt(path):
    """Parse one admission receipt; None when missing or malformed."""
    try:
        with open(path, encoding="utf-8") as handle:
            receipt = json.load(handle)
    except (OSError, ValueError):
        return None
    return receipt if isinstance(receipt, dict) else None


def _pid_alive(pid):
    """True when *pid* names a live process; ambiguous cases count as alive."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _unit_active(unit, runner=subprocess.run):
    """True when the named systemd user unit is active."""
    if not isinstance(unit, str) or not unit:
        return False
    try:
        result = runner(
            ["systemctl", "--user", "is-active", "--quiet", unit],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        return False
    return result.returncode == 0


def _session_active(session, runner=subprocess.run):
    """True when the named tmux session still exists (migration-era workers)."""
    if not isinstance(session, str) or not session:
        return False
    try:
        result = runner(
            ["tmux", "has-session", "-t", session],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        return False
    return result.returncode == 0


def _admission_holder_live(receipt, runner=subprocess.run):
    """Fresh liveness proof for one recorded admission holder."""
    if _pid_alive(receipt.get("pid")):
        return True
    if _unit_active(receipt.get("unit"), runner):
        return True
    if _session_active(receipt.get("session"), runner):
        return True
    return False


def acquire_card_admission(
    home,
    cid,
    *,
    owner,
    claim_revision,
    workspace,
    unit,
    session,
    runner=subprocess.run,
    now=None,
    pid=None,
    sleep=time.sleep,
    monotonic=time.monotonic,
):
    """Atomically admit one worker for an exact card on this host.

    Uses O_CREAT|O_EXCL on one receipt file per card so two launchers racing
    for the same card agree on exactly one holder. An existing receipt is
    authoritative until proven stale: recovery unlinks and retries once only
    when no matching process, unit, or session identity is alive. The receipt
    carries only non-secret deterministic fields and is emitted in refusal
    logs so the conflict is named without widening disclosure.

    Args:
        home: Estate home containing ``.skcapstone/fleet/admission/``.
        cid: Exact card id (also the receipt file name).
        owner: Exact claim owner about to be launched.
        claim_revision: Exact claim generation about to be launched.
        workspace: Worker worktree; stored normalized (realpath).
        unit: Transient worker unit name that will own the process.
        session: Worker session name (migration-era liveness witness).
        runner: Subprocess runner for liveness probes (tests inject fakes).
        now: Receipt timestamp override (tests inject for determinism).
        pid: Holder pid override (tests inject for determinism).
        sleep: Sleep callable used while waiting out an in-flight write.
        monotonic: Clock callable bounding the in-flight write window.

    Returns:
        ``{"admitted": True, "receipt": receipt}`` on admission, otherwise
        ``{"admitted": False, "reason": str, "receipt": holder-or-None}``
        with reason ``live-holder`` or ``contended-recovery``.
    """
    receipt = {
        "schema_version": 1,
        "host": HOST,
        "card_id": cid,
        "owner": owner,
        "claim_revision": claim_revision,
        "workspace": os.path.realpath(str(workspace)),
        "unit": unit,
        "session": session,
        "pid": os.getpid() if pid is None else pid,
        "acquired_at": (
            now or datetime.datetime.now(datetime.timezone.utc).isoformat()
        ),
    }
    path = _admission_lock_path(home, cid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for attempt in (1, 2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            # The winner creates the file before writing it; a contender
            # reading in that window must wait for the write to land, not
            # mistake an empty file for a stale lock and steal it (that
            # interleaving admitted BOTH launchers in the d11ca7ef race
            # analysis). Only a receipt that stays unreadable for the whole
            # window counts as stale.
            deadline = monotonic() + _ADMISSION_WRITE_WINDOW_S
            holder = _read_admission_receipt(path)
            while holder is None and monotonic() < deadline:
                sleep(_ADMISSION_WRITE_POLL_S)
                holder = _read_admission_receipt(path)
            if holder is not None and _admission_holder_live(holder, runner):
                return {"admitted": False, "reason": "live-holder", "receipt": holder}
            # Stale receipt: recover once, then refuse rather than loop
            # against a contended lock.
            try:
                os.unlink(path)
            except OSError:
                pass
            if attempt == 2:
                return {
                    "admitted": False,
                    "reason": "contended-recovery",
                    "receipt": holder,
                }
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, sort_keys=True)
        return {"admitted": True, "receipt": receipt}
    return {"admitted": False, "reason": "contended-recovery", "receipt": None}

_rows={}
def event_rows(cid):
    if cid in _rows: return _rows[cid]
    ev=os.path.join(CARDS,cid,"events"); out=[]
    if os.path.isdir(ev):
        for f in os.listdir(ev):
            try:
                for l in open(os.path.join(ev,f),encoding="utf-8",errors="replace"):
                    try:
                        _o=json.loads(l)
                    except:
                        continue
                    # A worker appended four bare JSON STRINGS into card 7b7c990f's
                    # event log (prose like "Pushed branch to origin and opened PR
                    # #2"). json.loads accepts those, and the sort below then called
                    # .get() on a str, so ONE malformed line crashed the rotation on
                    # ALL FIVE HOSTS for ~40 minutes on 2026-08-30: 46 failures,
                    # zero dispatch, and nothing alerted. ~/.skcapstone is one
                    # Syncthing folder, so the poison reached every host in minutes.
                    # A reader must never let one bad line stop the fleet.
                    if isinstance(_o, dict):
                        out.append(_o)
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


def _log_once_per_hour(d, event, cid, message, state_dir=None, now=None):
    """Emit one repeated per-card diagnostic in each UTC hour bucket.

    The O_EXCL marker makes concurrent rotations agree on the first emitter.
    State failures are fail-open so an observability aid cannot hide a blocker.
    """
    state_dir = state_dir or os.path.join(HOME, ".skcapstone/fleet/log-dedup")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    hour = now.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H")
    safe_event = re.sub(r"[^A-Z0-9_]+", "_", str(event).upper()).strip("_")
    safe_cid = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(cid))
    marker = os.path.join(state_dir, "%s-%s-%s.json" % (safe_event, safe_cid, hour))
    payload = json.dumps(
        {"card_id": str(cid), "event": str(event), "hour_utc": hour},
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    try:
        os.makedirs(state_dir, exist_ok=True)
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except OSError:
        log(d, message)
        return True
    try:
        encoded = payload.encode("utf-8")
        if os.write(fd, encoded) != len(encoded):
            raise OSError("short marker write")
        os.fsync(fd)
        os.close(fd)
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(marker)
        except OSError:
            pass
        log(d, message)
        return True
    log(d, message)
    return True

os.makedirs(os.path.join(HOME,".skcapstone/fleet"),exist_ok=True)
d=os.path.join(EVID,STAMP)
lock=acquire_rotation_lock(
    Path(HOME)/".skcapstone/fleet/rotate.lock",
    seat=ONLY_SEAT or "niobe",
)
if lock is None:
    log(d,"NOOP_RECEIPT|%s|reason=rotation_overlap|seat=%s"%
        (HOST,ONLY_SEAT or "niobe"))
    sys.exit(0)


if HOST not in ROTATION_HOSTS:
    log(d,"NOOP|%s|host is outside this estate's worker fleet: %s"%
        (HOST,",".join(ROTATION_HOSTS)))
    sys.exit(0)

# Mandatory read-only graph validation precedes slot and assignment decisions.
# The report is the exact machine-readable assignment exclusion contract.
try:
    if not _LIFECYCLE_OK:
        raise RuntimeError("lifecycle reassessment module unavailable")
    assessment=_validate_reassessment(assess(Path(CARDS),[Path(EVID)]))
    report_path=_full_reassessment_path(HOST,EVID,AUTHORITY_HOST)
    if report_path is not None:
        _write_bounded_report(assessment,report_path)
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
    log(d,_reassessment_summary(HOST,assessment,report_path,AUTHORITY_HOST))
except Exception as exc:
    log(d,"BLOCKED|%s|lifecycle reassessment failed: %s"%(HOST,exc))
    sys.exit(2)

# a slot IS a live ephemeral worker; -p workers exit when finished
# Migration window: existing tmux workers remain authoritative until they exit;
# new workers are transient user services and never enter this oneshot's cgroup.
sessions=sh("tmux","ls","-F","#{session_name}").split()
worker_units=active_worker_units()
_GATEWAY_ENDPOINT=os.environ.get("SKFLEET_GATEWAY_URL","http://chiap01:18790").rstrip("/")
_review_route_snapshot=None
_review_route_occupancy={}
_review_route_ambiguous=False
if ONLY_SEAT in {"", "link", "mero", "seraph"}:
    _review_route_snapshot=acquire_review_route_snapshot(
        _GATEWAY_ENDPOINT,
        Path(HOME)/".skcapstone/evidence/fleet-review-routes.json",
        new_cycle_id(HOST,STAMP),
    )
    _review_route_occupancy,_review_route_ambiguous=load_route_occupancy(
        Path(HOME)/".skcapstone"
    )
GLM_HOLD_PATH=os.path.join(HOME,".skcapstone/evidence/fleet-glm-dispatch-hold.json")
glm_held=False
try:
    with open(GLM_HOLD_PATH,encoding="utf-8") as _fh:
        glm_held=bool(json.load(_fh).get("active"))
except (OSError,ValueError,TypeError):
    pass

def _prepare_pi_glm_catalog():
    """Install logical GLM metadata before any live alias can be selected."""
    installed=Path(HOME)/".local/bin/skfleet-pi-model-catalog.py"
    bundled=Path(__file__).with_name("skfleet-pi-model-catalog.py")
    helper=installed if installed.exists() else bundled
    if not helper.is_file():
        return False,"catalog reconciler missing"
    try:
        result=subprocess.run(
            [sys.executable,str(helper),"--apply"],
            capture_output=True,text=True,timeout=15,check=False,
        )
    except (OSError,subprocess.TimeoutExpired) as exc:
        return False,"catalog reconciliation failed: %s"%type(exc).__name__
    if result.returncode:
        detail=(result.stderr or result.stdout or "reconciler refused").strip().splitlines()[0]
        return False,detail[:240]
    return True,(result.stdout or "catalog current").strip().splitlines()[0][:240]

glm_catalog_ready=True
if not DRY and GLM_TARGET > 0 and not glm_held:
    glm_catalog_ready,glm_catalog_detail=_prepare_pi_glm_catalog()
    if not glm_catalog_ready:
        log(d,"GLM_CATALOG_BLOCKED|%s|%s"%(HOST,glm_catalog_detail))
    else:
        log(d,"GLM_CATALOG_READY|%s|%s"%(HOST,glm_catalog_detail))
# Two worker lanes. GLM sat unused for six hours because the rotation only managed
# codex-auto-* sessions, so the z.ai account received no traffic at all while nine
# idle legacy glm panes did nothing. A lane is a prefix, a model alias, a target.
def _beat_interval():
    """Wrapper beat interval in seconds. Tunable via env, no redeploy."""
    return os.environ.get("SKFLEET_BEAT_INTERVAL", "60")

LANES=[
    # The lane fallback covers a card carrying no [S]/[M]/[L]/[XL] marker, which
    # the launch path skips anyway; a sized card resolves through _SIZE_MODELS.
    # Configurable so no estate has to edit this file to drop the codex name.
    {"name":"codex","prefix":"codex-auto-",
     "model":os.environ.get("SKFLEET_CODEX_LANE_MODEL","sk-codex-mid"),
     "target":TARGET},
    {"name":"glm","prefix":"glm-auto-","model":os.environ.get("SKFLEET_GLM_MODEL","sk-glm-s"),
     "target":0 if glm_held or not glm_catalog_ready else GLM_TARGET},
    # Restored. needs_escalation() still exists and still marks a card whose
    # worker reported blocked_on=capability, but the lane it routes to had been
    # dropped, so those cards were marked for a destination that did not exist
    # and could never be placed at all. 13 cards were in that state.
    #
    # It takes ONLY escalation cards and escalation cards go ONLY here, so the
    # stronger model is never spent on work the cheap lanes can do.
    {"name":"qwen","prefix":"qwen-auto-",
     "model":os.environ.get("SKFLEET_QWEN_MODEL","qwen3.8-27b-huihui-abliterated-q4_k_m"),
     "target":QWEN_TARGET},
    # Kimi is explicitly opt-in and defaults to zero slots. Kimi-labelled
    # cards never fall back to another lane when this lane is unavailable.
    {"name":"kimi","prefix":"kimi-auto-",
     "model":os.environ.get("SKFLEET_KIMI_MODEL","kimi-for-coding"),
     "target":KIMI_TARGET},
    {"name":"escalate","prefix":"esc-auto-",
     "model":os.environ.get("SKFLEET_ESC_MODEL", ESC_MODEL if "ESC_MODEL" in dir() else "gpt-5.6-sol"),
     "target":int(os.environ.get("SKFLEET_ESC_TARGET","2"))},
]
_GLM_LEVEL_DEFAULTS={"S":"sk-glm-s","M":"sk-glm-m","L":"sk-glm-l","XL":"sk-glm-l"}
_GLM_LEVELS={key:os.environ.get("SKFLEET_GLM_MODEL_"+key,value)
             for key,value in _GLM_LEVEL_DEFAULTS.items()}
_GLM_SIZE_RE=re.compile(r"\[(S|M|XL|L)\]")
_KIMI_SIZE_RE=re.compile(r"\[(S|M|L|XL)\]")
_LOGICAL_ROUTES={"S":"sk-s","M":"sk-m","L":"sk-l","XL":"sk-xl"}
# A card size selects a CAPABILITY BUCKET, never a provider. SKGateway resolves
# sk-s, sk-m, sk-l, and sk-xl to a member that meets the capability floor and the
# trust zone of the request, so one estate can run Claude, an OpenRouter free
# tier, or NIM while another runs a subscription lane, with no edit here and no
# fleet redeploy. The earlier defaults named codex roles directly
# (sk-codex-fast, sk-codex-mid, sk-codex), which pinned every estate installing
# this fleet to one subscription provider.
#
# A provider-pinned id is still a perfectly good operator override: set
# SKFLEET_MODEL_<size> to sk-codex-mid, sk-glm-l, or any advertised route.
# A bucket with no qualifying member fails closed at the gateway, and that gap
# stays visible: nothing here substitutes a smaller bucket, because a silent
# downgrade hides lost capability behind work that quietly got weaker.
_SIZE_MODEL_DEFAULTS=dict(_LOGICAL_ROUTES)
# SKFLEET_CODEX_MODEL_<size> is the deprecated, provider-named spelling of
# SKFLEET_MODEL_<size>. It is read second so an estate configured before the
# rename keeps its exact behaviour across the upgrade. An empty value counts as
# unset, so a blank systemd Environment= line cannot blank a bucket.
_SIZE_MODELS={key:(os.environ.get("SKFLEET_MODEL_"+key)
                   or os.environ.get("SKFLEET_CODEX_MODEL_"+key)
                   or value).strip() or value
              for key,value in _SIZE_MODEL_DEFAULTS.items()}
def _logical_route_for(core):
    """Return the one job-sized gateway bucket without selecting a backend."""
    matches=_GLM_SIZE_RE.findall(str((core or {}).get("title") or ""))
    return _LOGICAL_ROUTES.get(matches[0]) if len(matches)==1 else None
def _size_model_for(core):
    """Return the configured bucket for this card size, or None when unsized."""
    match=_GLM_SIZE_RE.search(str((core or {}).get("title") or ""))
    return _SIZE_MODELS.get(match.group(1)) if match else None
def _glm_model_for(core):
    match=_GLM_SIZE_RE.search(str((core or {}).get("title") or ""))
    return _GLM_LEVELS.get(match.group(1)) if match else None
def _kimi_model_for(core):
    match=_KIMI_SIZE_RE.search(str((core or {}).get("title") or ""))
    return "k3" if match and match.group(1)=="XL" else "kimi-for-coding"


def _producer_routes_for(core, labels, lane=None):
    """Return current gateway routes for one producer card and optional lane pin."""
    match=_GLM_SIZE_RE.search(str((core or {}).get("title") or ""))
    routes=([] if _review_route_ambiguous or match is None else eligible_gateway_routes(
        _review_route_snapshot or {},match.group(1),labels,_review_route_occupancy))
    if lane is None:
        return routes
    token=str(lane).lower()
    return [route for route in routes if token in (
        str(route.get("provider") or "")+" "+str(route.get("logical_route") or "")
    ).lower()]
if glm_held:
    log(d,"GLM_HOLD|%s|new GLM dispatch disabled by %s"%(HOST,GLM_HOLD_PATH))
for _L in LANES:
    _L["busy"]=_lane_busy(_L,sessions,worker_units)
    _L["free"]=max(0,_L["target"]-len(_L["busy"]))
if not ONLY_SEAT:
    _gateway_routes=([] if _review_route_ambiguous else eligible_gateway_routes(
        _review_route_snapshot or {},"S",[],_review_route_occupancy))
    _codex=next(lane for lane in LANES if lane["name"]=="codex")
    _codex["free"]=aggregate_review_capacity(
        _gateway_routes,min(TARGET,CODEX_PHYSICAL_LIMIT,MAX_LAUNCH))
if ONLY_SEAT:
    if not _SEAT_RE.fullmatch(ONLY_SEAT) or SEAT_TARGET < 1:
        raise SystemExit("BLOCKED|SKFLEET_SEAT_TARGET|seat dispatch requires a positive target")
    _codex=next(lane for lane in LANES if lane["name"]=="codex")
    _busy_cards=_worker_cards(sessions,worker_units,[_codex])
    if ONLY_SEAT in {"link","mero","seraph"}:
        _capacity_routes=([] if _review_route_ambiguous else eligible_review_routes(
            _review_route_snapshot or {},"S",[],"producer","pi-seraph-capacity",
            _review_route_occupancy))
        _codex["free"]=aggregate_review_capacity(_capacity_routes,SEAT_TARGET)
    else:
        _codex["free"]=_seat_capacity(
            ONLY_SEAT,SEAT_TARGET,CODEX_PHYSICAL_LIMIT,_busy_cards,_last_claim_owner)
    _codex["target"]=SEAT_TARGET
free=sum(_L["free"] for _L in LANES)
log(d, "SLOTS|%s|%s" % (HOST, _slot_summary(LANES)))
try:
    WORKER_MAIL_RECIPIENTS = _worker_mail_routing()
except ValueError as exc:
    log(d, "BLOCKED|%s|%s" % (HOST, exc))
    sys.exit(2)

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
REAP_RUNTIME_VERSION = importlib.metadata.version("skcoord")
LIVE_FRESH = 30 * 60      # a report older than this says nothing about now
LIVE_TIMER_CYCLE = 6 * 60  # five-minute timer plus transport allowance
CLAIM_GRACE = 300         # one full rotation period, so every host has reported
# Reaping needs a quorum, because a card running on chiap04 is invisible in
# chiap08's report. During a rollout the first host to publish is the ONLY
# reporting host, and without this floor it would read every other host's live
# worker as absent and reap all of them. Below quorum the reaper does nothing.
REAP_QUORUM = 3

STALL_GRACE = 30 * 60     # a zero-byte log younger than this may still be starting
_NO_PROGRESS = os.path.join(HOME, ".skcapstone/evidence/live-no-progress")

def _never_started(cid):
    """Return the zero-byte launch log and age when this card never started.

    Liveness here used to mean only that the tmux session exists. A launch
    wrapper whose child never starts sits in do_wait forever, keeps its session,
    and holds the claim while every reaper tick correctly reports it live.
    Observed 2026-08-31 on b27301c0: elapsed 17:24:15, CPU time 00:00:00, log
    0 bytes, claim held 17.4h until it was killed by hand.

    Emptiness is the only safe discriminator. A log with ANY content keeps the
    card live no matter how old, because a long quiet think is legitimate and
    reaping on mtime staleness would pull cards out from under working agents,
    which is the outage the quorum and oldest-report rules exist to prevent.
    A missing log is also not evidence of death, so it never reaps either.
    """
    try:
        logs = glob.glob(os.path.join(HOME, ".skcapstone/fleet/logs", cid + "-*.log"))
        if not logs:
            return False
        newest = max(logs, key=os.path.getmtime)
        st = os.stat(newest)
    except OSError:
        return False
    if st.st_size > 0:
        return None
    age = time.time() - st.st_mtime
    return (newest, age) if age > STALL_GRACE else None


def _record_live_no_progress(cid, worker, path, age):
    """Record one bounded escalation without converting quietness into death."""
    try:
        folded = CardStore(Path(HOME) / ".skcapstone").fold(cid)
        if isinstance(folded, dict):
            owner, meta = folded.get("owner"), folded.get("meta")
        else:
            owner, meta = getattr(folded, "owner", None), getattr(folded, "meta", None)
        owner = str(owner or "")
        revision = str((meta or {}).get("_claim_revision") or "")
    except Exception:
        owner = revision = ""
    if not owner or not revision:
        return False
    try:
        generation = str(os.stat(path).st_mtime_ns)
        key = "\0".join((cid, owner, revision, generation)).encode()
        digest = hashlib.sha256(key).hexdigest()
        os.makedirs(_NO_PROGRESS, exist_ok=True)
        target = os.path.join(_NO_PROGRESS, digest + ".json")
        payload = json.dumps({
            "age_seconds": int(age),
            "card": cid,
            "claim_revision": revision,
            "log": path,
            "observation_generation": generation,
            "owner": owner,
            "state": "live_no_progress",
            "worker": worker,
        }, sort_keys=True, separators=(",", ":")) + "\n"
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except (FileExistsError, OSError):
        return False
    try:
        os.write(fd, payload.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def publish_live(sessions, units=()):
    """Record legacy tmux and transient-service workers for every other host."""
    cards = _worker_cards(sessions,units,LANES)
    for _cid in cards:
        stalled = _never_started(_cid)
        if stalled:
            path, age = stalled
            worker = next(
                (s for L in LANES for s in sessions
                 if s.startswith(L["prefix"]) and s[len(L["prefix"]):] == _cid),
                next((u["unit"] for u in units if u["card"] == _cid), "unknown"),
            )
            if _record_live_no_progress(_cid, worker, path, age):
                log(d, "LIVE_NO_PROGRESS|%s|%s|worker=%s|log=%s|age_seconds=%d|"
                       "worker remains live; bounded escalation recorded"
                    % (HOST, _cid, worker, path, int(age)))
    try:
        os.makedirs(LIVE, exist_ok=True)
        p = os.path.join(LIVE, HOST + ".json")
        tmp = p + ".new"
        workers = []
        for card in cards:
            try:
                folded = CardStore(Path(HOME) / ".skcapstone").fold(card)
                owner = str(getattr(folded, "owner", "") or "")
                revision = str(getattr(folded, "meta", {}).get("_claim_revision") or "")
                if owner and revision:
                    workers.append({
                        "card_id": card,
                        "owner": owner,
                        "claim_revision": revision,
                    })
            except (OSError, TypeError, ValueError):
                # Unresolved identity remains represented in cards and occupied.
                continue
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({
                "host": HOST,
                "ts": time.time(),
                "cards": cards,
                "workers": workers,
                "lanes": {
                    lane.get("name", lane.get("prefix", "unknown").rstrip("-")): {
                        "target": lane.get("target", 0),
                        "busy": len(lane.get("busy", ())),
                        "free": lane.get("free", 0),
                    }
                    for lane in LANES
                },
            }, fh, sort_keys=True)
        os.replace(tmp, p)          # atomic, so a reader never sees a half file
    except OSError as exc:
        log(d, "WARN|%s|could not publish liveness: %s" % (HOST, exc))
    return cards

def reporting_capacity():
    """Return total free lanes advertised by each currently reporting host."""
    capacity = {}
    now = time.time()
    for path in glob.glob(os.path.join(LIVE, "*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                snap = json.load(fh)
            ts = float(snap.get("ts") or 0)
            lanes = snap.get("lanes") or {}
            if not 0 < ts <= now or now - ts > LIVE_FRESH or not isinstance(lanes, dict):
                continue
            capacity[str(snap.get("host") or Path(path).stem)] = sum(
                max(0, int(lane.get("free", 0)))
                for lane in lanes.values() if isinstance(lane, dict)
            )
        except (OSError, ValueError, TypeError):
            continue
    return capacity


def live_report_health(expected_hosts=None, now=None):
    """Return fleet reports plus per-host transport and freshness faults."""
    now = time.time() if now is None else now
    expected = tuple(expected_hosts or ROTATION_HOSTS)
    stamps = []
    running = set()
    reporting = set()
    faults = []
    for host in expected:
        path = os.path.join(LIVE, host + ".json")
        try:
            with open(path, encoding="utf-8") as fh:
                snap = json.load(fh)
            if not isinstance(snap, dict):
                raise ValueError("report is not an object")
            ts = float(snap.get("ts") or 0)
            report_host = str(snap.get("host") or "")
            if report_host != host:
                raise ValueError("report host=%s" % (report_host or "missing"))
            if not 0 < ts <= now:
                raise ValueError("report timestamp is absent or in the future")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            reason = "missing" if isinstance(exc, FileNotFoundError) else "invalid"
            faults.append({"host": host, "reason": reason, "age_seconds": None,
                           "detail": str(exc)[:120]})
            continue
        age = now - ts
        if age > LIVE_FRESH:
            faults.append({"host": host, "reason": "stale",
                           "age_seconds": int(age), "detail": ""})
            continue
        stamps.append(ts)
        reporting.add(host)
        cards = snap.get("cards") or []
        if isinstance(cards, list):
            running.update(str(card) for card in cards)
        if age > LIVE_TIMER_CYCLE:
            faults.append({"host": host, "reason": "transport_delayed",
                           "age_seconds": int(age), "detail": ""})
    expected_set = set(expected)
    return {"oldest": min(stamps) if stamps else 0, "running": running,
            "reporting": reporting, "expected": expected_set, "faults": faults,
            "authoritative": reporting == expected_set and not faults}


def live_report():
    """Return the authoritative cross-host report health snapshot."""
    return live_report_health()

if not ONLY_SEAT:
    publish_live(sessions, worker_units)

if free==0:
    log(d,"NOOP|%s|all slots busy"%HOST)
    log(d,"NOOP_RECEIPT|%s|reason=no_available_capacity|seat=%s"%
        (HOST,ONLY_SEAT or "generic"))
    sys.exit(0)

# ---- assignable pool: unclaimed, not human, not drift, DEPENDENCIES SATISFIED
# Cards that were launched before and never produced a claim event cannot be
# claimed by a worker (closed ITIL incident, id namespace the board rejects, or
# already assigned elsewhere). Without this, the same card is relaunched every
# cycle forever: measured 78 of 162 launches wasted, 48 percent, before this gate.
_launched=collections.Counter()
_launched_at={}
_wake_launch_times=collections.defaultdict(list)
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
                    if len(_p)==8:
                        _fields=[part.partition("=") for part in _p[4:]]
                        if [(key,sep) for key,sep,_value in _fields]==[
                                ("lane","="),("model","="),("owner","="),
                                ("claim_revision","=")] and all(value for _key,_sep,value in _fields):
                            _wake_launch_times[_p[3]].append(_launch_epoch)
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
# unassign, claim or release_claim events do NOT resurrect it: unassigning a
# finished card clears an assignee, it does not un-finish the work, a late
# claim from a host whose sync lag predates the complete is a race symptom,
# not a revival, and the release trap of such a zombie worker must not fold
# the card back to the assignable pool. A naive last-write-wins fold gets
# this wrong and re-offers completed cards forever.
# Measured: 4d98b588 has claim, move, claim, complete, assign, unassign, claim
# and 92bd87a3 has claim, complete, assign, unassign. Both were being handed to
# workers, which then spent ~80 seconds each discovering the card was already
# done and correctly refusing. That was the real "stale pool" cost, and the race
# was a symptom rather than the cause.
# Measured again on 56f9d32f: complete at 21:26:13Z, late claim at 21:29:11Z
# spawned a worker, and that worker's release_claim at 22:25:28Z folded the
# card to backlog, so the next cycle spawned another worker at 22:29Z. The
# claim branch had no terminal guard and release_claim reset status
# unconditionally. reopen is the one explicit path that clears terminality.
_COLUMNS = {"backlog", "ready", "doing", "review", "done"}
_NOT_CLAIMABLE = {"not-claimable", "sprint-container", "do-not-claim"}
_SENSITIVE_CATEGORY = re.compile(
    r"(capauth|credential|custody|issuer|secret|\bkey\b|rollback|"
    r"deploy|production|release|migrat)", re.I)
_CATEGORY_OPT_IN = "dispatch-approved"
_OVERLAY_ACTIONS = {
    "move": "move", "assign": "assign", "unassign": "unassign",
    "add_label": "add_label", "remove_label": "remove_label",
    "describe": "describe", "amend_criteria": "amend_criteria", "link": "link",
}
_claim_rows = {}
_legacy_claim_rows = None


def _role_seat_metadata(core, seat):
    """Return exact card-bound authority metadata for Tank or ATLAS."""
    links = core.get("links") if isinstance(core.get("links"), dict) else {}
    if seat == "tank":
        digest = str(links.get("approved_artifact_sha256") or "").strip().lower()
        return (digest,) if re.fullmatch(r"[0-9a-f]{64}", digest) else None
    if seat == "atlas":
        target = str(links.get("verification_target") or "").strip()
        digest = str(links.get("verification_evidence_sha256") or "").strip().lower()
        if target and re.fullmatch(r"[0-9a-f]{64}", digest):
            return target, digest
    return None


def _strict_card_events(cid, fresh=False):
    """Read one native CardStore stream, failing closed on malformed data."""
    if not fresh and cid in _claim_rows:
        return _claim_rows[cid]
    path = os.path.join(CARDS, cid, "events")
    rows = []
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if not name.endswith(".jsonl"):
                continue
            with open(os.path.join(path, name), encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("event is not an object")
                    rows.append(event)
    rows.sort(key=lambda e: (str(e.get("ts") or ""),
                             str(e.get("writer") or ""), e.get("seq", 0)))
    if not fresh:
        _claim_rows[cid] = rows
    return rows


def _legacy_claimability_events(fresh=False):
    """Return the sanctioned Board overlay and archive events by card ID."""
    global _legacy_claim_rows
    if _legacy_claim_rows is not None and not fresh:
        return _legacy_claim_rows
    out = {}
    overlay = os.path.join(HOME, ".skcapstone/coordination/card_events")
    for path in sorted(glob.glob(os.path.join(overlay, "*.jsonl"))):
        try:
            lines = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with lines:
            for line in lines:
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if not isinstance(event, dict):
                    continue
                action = _OVERLAY_ACTIONS.get(event.get("action"))
                cid = event.get("card_id")
                if action and isinstance(cid, str) and cid:
                    row = dict(event)
                    row["action"] = action
                    out.setdefault(cid, []).append(row)
    archive = os.path.join(HOME, ".skcapstone/coordination/archive")
    for path in sorted(glob.glob(os.path.join(archive, "*.jsonl"))):
        try:
            lines = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with lines:
            for line in lines:
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                cid = entry.get("id") if isinstance(entry, dict) else None
                if isinstance(cid, str) and cid:
                    out.setdefault(cid, []).append({
                        "ts": entry.get("archived_at", ""),
                        "writer": entry.get("archived_by") or "archive",
                        "seq": 0,
                        "action": "archive",
                    })
    for rows in out.values():
        rows.sort(key=lambda e: (str(e.get("ts") or ""),
                                 str(e.get("writer") or ""), e.get("seq", 0)))
    if not fresh:
        _legacy_claim_rows = out
    return out


def _fold_claimability(core, rows):
    """Fold only fields used by Board.claim_task and scheduler policy."""
    core_links = core.get("links") if isinstance(core.get("links"), dict) else {}
    state = {
        "status": "backlog", "owner": None, "claim_revision": None,
        "archived": False, "voided": False, "terminal": False,
        "review_seen": False,
        "title": str(core.get("title") or ""),
        "description": str(core.get("description") or ""),
        "acceptance_criteria": [
            str(x) for x in (core.get("acceptance_criteria") or [])
        ],
        "links": {
            str(key): str(value).strip()
            for key, value in core_links.items()
            if str(key).strip() and str(value).strip()
        },
        "labels": [str(x) for x in (core.get("initial_labels") or [])],
        "dependencies": [str(x) for x in (core.get("dependencies") or [])],
    }
    review_link_keys = {
        "pr", "pull_request", "open_pr", "candidate_evidence_sha256",
        "evidence", "evidence_sha256",
    }
    # Retain metadata as history while tracking which review markers belong
    # to the current workflow phase. An explicit executable transition starts
    # a new phase; an automatic claim release never does.
    markers = {
        "title": "[REVIEW]" in state["title"].upper(),
        "description": "PASS_FOR_REVIEW" in state["description"].upper(),
        **{"label:" + str(label).strip().lower(): True for label in state["labels"]
           if str(label).strip().lower() in {"review", "review-only"}},
        **{"link:" + key: bool(value) for key, value in state["links"].items()
           if key in review_link_keys},
    }
    state["review_markers"] = markers
    ordered = sorted(rows, key=lambda e: (str(e.get("ts") or ""),
                                          str(e.get("writer") or ""), e.get("seq", 0)))
    for event in ordered:
        action = event.get("action")
        if action == "move":
            column = str(event.get("column") or "").strip().lower()
            if column in _COLUMNS:
                state["status"] = column
                state["review_seen"] = column == "review"
                if column in {"backlog", "ready", "doing"}:
                    markers.clear()
        elif action == "assign":
            state["owner"] = event.get("owner")
            state["claim_revision"] = None
        elif action == "unassign":
            state["owner"] = None
            state["claim_revision"] = None
        elif action == "release_claim":
            owner = event.get("released_owner")
            revision = event.get("expected_claim_revision")
            if (
                owner == state["owner"]
                and revision == state["claim_revision"]
                and owner
                and revision
            ):
                state["owner"] = None
                state["claim_revision"] = None
                if not state["terminal"]:
                    state["status"] = "backlog"
        elif action == "claim":
            owner = event.get("owner")
            if not isinstance(owner, str) or not owner:
                raise ValueError("claim owner is missing")
            if state["terminal"]:
                continue
            if (state["owner"] and state["owner"] != owner and
                    state["status"] in {"ready", "doing", "review"}):
                continue
            state["owner"] = owner
            state["status"] = "doing"
            state["claim_revision"] = event.get("claim_revision") or event.get("event_id")
        elif action == "complete":
            state["status"] = "done"
            state["owner"] = None
            state["claim_revision"] = None
            state["terminal"] = True
        elif action == "void":
            state["voided"] = True
            state["terminal"] = True
        elif action == "archive":
            state["archived"] = True
        elif action == "reopen":
            state["archived"] = False
            state["terminal"] = False
            column = str(event.get("column") or "").strip().lower()
            if column in _COLUMNS:
                state["status"] = column
                state["review_seen"] = column == "review"
                if column in {"backlog", "ready", "doing"}:
                    markers.clear()
        elif action == "add_label":
            label = event.get("label")
            if str(label).strip().lower() in {"review", "review-only"}:
                markers["label:" + str(label).strip().lower()] = True
            if isinstance(label, str) and label and label not in state["labels"]:
                state["labels"].append(label)
        elif action == "remove_label":
            label = event.get("label")
            markers.pop("label:" + str(label).strip().lower(), None)
            state["labels"] = [x for x in state["labels"] if x != label]
        elif action == "describe":
            if event.get("title") is not None:
                state["title"] = str(event.get("title"))
                markers["title"] = "[REVIEW]" in state["title"].upper()
            if event.get("description") is not None:
                state["description"] = str(event.get("description"))
                markers["description"] = "PASS_FOR_REVIEW" in state["description"].upper()
        elif action == "amend_criteria":
            criteria = event.get("criteria")
            if not isinstance(criteria, list) or not criteria or not all(
                isinstance(value, str) and value.strip() for value in criteria
            ):
                raise ValueError("amended acceptance criteria are malformed")
            state["acceptance_criteria"] = list(criteria)
        elif action == "link" and event.get("link_key") in {
            "producer_identity", "candidate_evidence_sha256", "pr",
            "pull_request", "open_pr", "evidence", "evidence_sha256",
            "repository", "base_ref", "base_revision",
            "link_source_card", "link_head_revision",
        }:
            value = event.get("link_value")
            if not isinstance(value, str) or not value.strip():
                if event.get("link_key") in {"pr", "evidence", "evidence_sha256"}:
                    continue
                raise ValueError("typed review metadata is malformed")
            state["links"][str(event["link_key"])] = value.strip()
            if event["link_key"] in review_link_keys:
                markers["link:" + event["link_key"]] = True
        elif action in ("add_dependency", "remove_dependency"):
            dep = _dependency_value(event)
            if action == "add_dependency" and dep and dep not in state["dependencies"]:
                state["dependencies"].append(dep)
            elif action == "remove_dependency" and dep:
                state["dependencies"] = [x for x in state["dependencies"] if x != dep]
    return state


def _claimability_reason(core, state):
    """Return the exact reason Board or scheduler policy rejects this state."""
    folded_core = dict(core)
    folded_core["title"] = state["title"]
    folded_core["description"] = state["description"]
    folded_core["acceptance_criteria"] = state["acceptance_criteria"]
    folded_core["links"] = dict(state["links"])
    labels = state["labels"]
    if not _coord_task_claimable(core):
        return "non-task"
    if state["voided"]:
        return "void"
    if state["archived"]:
        return "archive"
    if state["status"] == "done":
        return "done"
    if state["owner"] and state["status"] in {"ready", "doing", "review"}:
        return "owned-%s" % state["status"]
    # Review work is a separate lane.  An unowned review card must not fall
    # through as executable work after its producer releases the claim.  The
    # explicit markers also cover stale projections whose column is backlog.
    # A dedicated reviewer must reach _review_assignment, which enforces
    # producer separation and exact candidate evidence before launch.
    review_marked = (
        state["status"] == "review"
        or state["review_seen"]
        or any(state["review_markers"].values())
    )
    if non_implementation(folded_core, labels):
        return "human-gate"
    if "foreign-project" in {str(x).strip().lower() for x in labels}:
        return "foreign-project"
    if _NOT_CLAIMABLE & ({str(x).strip().lower() for x in labels} |
                         {str(x).strip().lower() for x in (core.get("tags") or [])}):
        return "not-claimable"
    if (_SENSITIVE_CATEGORY.search(state["title"]) and
            _CATEGORY_OPT_IN not in {str(x).strip().lower() for x in labels}):
        return "sensitive-category"
    if any(not _dep_satisfied(dep) for dep in state["dependencies"]):
        return "dependency"
    pin = host_pin(folded_core, labels)
    if pin and pin != HOST:
        return "host-pin:%s" % pin
    return "review" if review_marked else "claimable"


def _authoritative_card_snapshot(cid, core=None, fresh=False):
    """Read and fold one card from one core and event snapshot."""
    if core is None:
        with open(os.path.join(CARDS, cid, "core.json"), encoding="utf-8") as fh:
            core = json.load(fh)
    if (not isinstance(core, dict) or not isinstance(core.get("id"), str) or
            core.get("id") != cid):
        raise ValueError("core identity mismatch")
    rows = list(_strict_card_events(cid, fresh=fresh))
    rows.extend(_legacy_claimability_events(fresh=fresh).get(cid, []))
    source_revision = hashlib.sha256(json.dumps(
        {"core": core, "events": rows},
        sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return core, _fold_claimability(core, rows), source_revision


def _authoritative_card_state(cid, core=None, fresh=False):
    """Read and fold one card without applying dependency or scheduler policy."""
    core, state, _source_revision = _authoritative_card_snapshot(
        cid, core=core, fresh=fresh
    )
    return core, state


def authoritative_claimability(cid, core=None, fresh=False):
    """Return the one claimability decision used by pool and preclaim."""
    try:
        core, state, source_revision = _authoritative_card_snapshot(
            cid, core=core, fresh=fresh
        )
    except Exception as exc:
        return {"claimable": False, "reason": "malformed:%s" % type(exc).__name__}

    folded_core = dict(core)
    folded_core["title"] = state["title"]
    folded_core["description"] = state["description"]
    folded_core["acceptance_criteria"] = state["acceptance_criteria"]
    folded_core["links"] = dict(state["links"])
    labels = state["labels"]
    reason = _claimability_reason(core, state)
    state.update({"claimable": reason == "claimable", "reason": reason,
                  "core": folded_core, "host_pin": host_pin(folded_core, labels),
                  "source_revision": source_revision})
    return state


def lifecycle_state(cid):
    """Return the scheduler lifecycle derived from the authoritative fold."""
    try:
        _core, decision = _authoritative_card_state(cid)
    except Exception:
        return "ambiguous"
    if decision["status"] == "done":
        return "complete"
    if decision["archived"] or decision["voided"]:
        return "void"
    if decision["owner"] and decision["status"] in {"ready", "doing", "review"}:
        return "claimed"
    return "open"


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
_OUTCOME_VALUE_RE = re.compile(
    r"^\s*(BLOCKED|PASS(?:_FOR_[A-Z_]+)?|FAIL|DENY|HOLD|VOID|WORKER_DIED|APPROVE(?:D)?)"
    r"(?:\b|_)", re.I)
_PIPE_OUTCOME_RE = re.compile(
    r"(?:^|\|)\s*(BLOCKED|PASS(?:_FOR_[A-Z_]+)?|FAIL|DENY|HOLD|VOID|WORKER_DIED|APPROVE(?:D)?)"
    r"\s*(?:\||$)", re.I)
_INVALID_NATIVE_OUTCOME = "BLOCKED native_outcome_invalid=true"

def _fold_key(k):
    k = str(k or "").strip().lower().replace("-", "_")
    k = re.sub(r"_?20\d{6}t?\d{0,6}z?", "", k)
    k = re.sub(r"_[0-9a-f]{8,64}$", "", k)
    return re.sub(r"__+", "_", k).strip("_")

_evidence_events = None
_outcomes = None
_label_events = None

def _load_evidence_events():
    global _evidence_events
    if _evidence_events is not None: return _evidence_events
    _evidence_events={}
    for f in sorted(glob.glob(os.path.join(_EVID_DIR,"*.jsonl"))):
        try:
            for line in open(f,encoding="utf-8",errors="replace"):
                try: event=json.loads(line)
                except Exception: continue
                if not isinstance(event,dict): continue
                cid=event.get("card_id")
                if cid: _evidence_events.setdefault(str(cid),[]).append(event)
        except OSError: pass
    for rows in _evidence_events.values():
        rows.sort(key=lambda e:(e.get("ts",""),str(e.get("writer","")),str(e.get("event_id",""))))
    return _evidence_events

def _native_outcome_value(event):
    """Return one safe outcome value from a native CardStore verdict event."""
    payload=event.get("payload") if isinstance(event.get("payload"),dict) else {}
    # Early BLOCKED writers used action=blocked and put the complete contract in
    # the blocked_on object. Treat that object as evidence only when it carries
    # the category, referent, artifact, and artifact hash together. Lifecycle
    # state and a bare link never manufacture a verdict.
    if event.get("action")=="blocked" and isinstance(event.get("blocked_on"),dict):
        blocked=event["blocked_on"]
        digest=str(blocked.get("evidence_sha256") or "")
        artifact=str(blocked.get("evidence") or "")
        if (str(blocked.get("verdict") or "").upper()=="BLOCKED" and artifact and
                re.fullmatch(r"[0-9a-fA-F]{64}",digest)):
            payload=blocked
            event={"verdict":"BLOCKED","blocked_on":blocked.get("blocked_on"),
                   "referent":blocked.get("referent")}
        else:
            return _INVALID_NATIVE_OUTCOME

    def field(name):
        values=[]
        for source in (event,payload):
            if name not in source: continue
            value=source.get(name)
            if not isinstance(value,str) or not value.strip(): return None,False
            values.append(value.strip())
        if not values: return None,True
        if len({value.lower() for value in values})!=1: return None,False
        return values[0],True

    value,value_ok=field("verdict")
    category,category_ok=field("blocked_on")
    referent,referent_ok=field("referent")
    if not value_ok or not value: return _INVALID_NATIVE_OUTCOME
    match=_OUTCOME_VALUE_RE.match(value) or _PIPE_OUTCOME_RE.search(value)
    if not match: return _INVALID_NATIVE_OUTCOME
    if not _OUTCOME_VALUE_RE.match(value): value=match.group(1)

    is_blocked=bool(re.match(r"^\s*BLOCKED\b",value,re.I))
    has_blocker=any(name in event or name in payload
                    for name in ("blocked_on","referent"))
    if has_blocker and (not category_ok or not referent_ok or not category or not referent):
        return _INVALID_NATIVE_OUTCOME
    if not is_blocked:
        return _INVALID_NATIVE_OUTCOME if has_blocker else value

    embedded=_blocked_reason(value)
    structured=_blocked_reason(
        "BLOCKED blocked_on=%s referent=%s"%(category,referent)) if has_blocker else None
    if has_blocker and not structured: return _INVALID_NATIVE_OUTCOME
    if embedded and structured and embedded!=structured: return _INVALID_NATIVE_OUTCOME
    reason=structured or embedded
    if not reason: return _INVALID_NATIVE_OUTCOME
    return "BLOCKED blocked_on=%s %s"%(
        reason[0]," ".join("referent="+item for item in reason[1]))

def _load_outcomes():
    global _outcomes
    if _outcomes is not None: return _outcomes
    _outcomes = {}
    orders={}

    def record(cid,event,value,source_rank):
        ts=str(event.get("ts") or "")
        order=(_ts_epoch(ts),ts,str(event.get("writer") or ""),
               str(event.get("event_id") or ""),source_rank)
        if cid not in orders or order>orders[cid]:
            orders[cid]=order
            _outcomes[cid]=(ts,value)

    for cid,rows in _load_evidence_events().items():
        blocked_parts={}
        has_independent_review=any(
            e.get("action")=="link"
            and _fold_key(e.get("link_key"))=="independent_review"
            and str(e.get("link_value") or "").strip()
            for e in rows)
        for e in rows:
            if e.get("action") != "link": continue
            fk = _fold_key(e.get("link_key"))
            val = str(e.get("link_value") or "")
            # A consumer may copy its dependency's result for audit. That is
            # not the consumer's own outcome and must not park it in review.
            if fk=="review_verdict" and has_independent_review:
                continue
            if any(o in fk for o in _OUTCOME_KEYS):
                blocked_parts.clear()
                # A link named verdict_artifact is not an outcome. Several such
                # paths and hashes sort after the real verdict and used to erase it.
                match=_OUTCOME_VALUE_RE.match(val) or _PIPE_OUTCOME_RE.search(val)
                if not match: continue
                if not _OUTCOME_VALUE_RE.match(val): val=match.group(1)
                record(cid,e,val,0)
                continue
            writer=str(e.get("writer") or "")
            if not writer:
                continue
            if fk=="blocked_on":
                parsed=_parse_blocked_on_link(val)
                if not parsed:
                    blocked_parts.pop(writer,None)
                    continue
                category,referent=parsed
                part={"blocked_on":category}
                if referent:
                    part["referent"]=referent
                blocked_parts[writer]=part
                continue
            field=("referent" if fk=="referent" or "blocked_referent" in fk else
                   "evidence" if fk in ("evidence","blocked_evidence") else
                   "evidence_sha256" if fk in (
                       "evidence_sha256","blocked_evidence_sha256") else None)
            part=blocked_parts.get(writer)
            if not part or field is None or not val:
                if field is not None:
                    blocked_parts.pop(writer,None)
                continue
            if field in part:
                blocked_parts.pop(writer,None)
                continue
            expected_order=("blocked_on","referent","evidence","evidence_sha256")
            next_needed=expected_order[len(part)]
            if field!=next_needed:
                blocked_parts.pop(writer,None)
                continue
            if field=="evidence_sha256" and not re.fullmatch(r"[0-9a-fA-F]{64}",val):
                blocked_parts.pop(writer,None)
                continue
            part[field]=val.lower() if field!="evidence" else val
            if field=="evidence_sha256":
                record(cid,e,"BLOCKED blocked_on=%s referent=%s"%
                       (part["blocked_on"],part["referent"]),1)
                blocked_parts.pop(writer,None)
    native_ids=set(_load_evidence_events())
    native_ids.update(os.path.basename(path) for path in glob.glob(os.path.join(CARDS,"*"))
                      if os.path.isdir(path))
    for cid in sorted(native_ids):
        identities=collections.defaultdict(list)
        for event in event_rows(cid):
            identity=(str(event.get("ts") or ""),str(event.get("writer") or ""),
                      str(event.get("event_id") or ""))
            identities[identity].append(event)
        for rows in identities.values():
            verdicts=[event for event in rows if event.get("action") in ("verdict","blocked")]
            if not verdicts: continue
            signatures={json.dumps({
                "event":{
                    key:event.get(key)
                    for key in ("action","verdict","blocked_on","referent")
                    if key in event
                },
                "payload":{
                    key:event["payload"].get(key)
                    for key in ("verdict","blocked_on","referent")
                    if isinstance(event.get("payload"),dict) and key in event["payload"]
                },
            },sort_keys=True,separators=(",",":")) for event in rows}
            value=(_native_outcome_value(verdicts[0]) if len(signatures)==1
                   else _INVALID_NATIVE_OUTCOME)
            record(cid,verdicts[0],value,1)
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
    for cid,rows in _load_evidence_events().items():
        _label_events[cid]=[event for event in rows
                            if event.get("action") in ("add_label","remove_label")]
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

_SEAT_LABEL_PREFIX = "seat-"
_SEAT_PLACEMENT_PATH = os.environ.get(
    "SKFLEET_SEAT_PLACEMENT",
    os.path.join(HOME, ".skcapstone/coordination/seat-placement.json"),
)


def _load_seat_placement(path=None):
    """Read the synchronized public seat-to-host manifest or fail closed."""
    source = path or _SEAT_PLACEMENT_PATH
    try:
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, "manifest-unavailable:%s" % type(exc).__name__
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return {}, "manifest-schema"
    seats = payload.get("seats")
    if not isinstance(seats, dict):
        return {}, "manifest-seats"
    normalized = {}
    for raw_seat, raw_hosts in seats.items():
        seat = str(raw_seat).strip().lower()
        if not _SEAT_RE.fullmatch(seat):
            return {}, "manifest-seat:%s" % seat
        if not isinstance(raw_hosts, list) or not raw_hosts:
            return {}, "manifest-hosts:%s" % seat
        hosts = tuple(str(host).strip().lower() for host in raw_hosts)
        if len(set(hosts)) != len(hosts) or any(host not in ROTATION_HOSTS for host in hosts):
            return {}, "manifest-hosts:%s" % seat
        normalized[seat] = tuple(host for host in ROTATION_HOSTS if host in hosts)
    return normalized, None


_SEAT_PLACEMENT, _SEAT_PLACEMENT_ERROR = _load_seat_placement()
_ONLY_SEAT = ONLY_SEAT
if _ONLY_SEAT and not _SEAT_RE.fullmatch(_ONLY_SEAT):
    raise SystemExit("BLOCKED|SKFLEET_ONLY_SEAT|invalid seat")

def seat_for(cid, core):
    """Return the named seat this card belongs to, or None.

    A card labelled `seat-<name>` is work belonging to a standing seat rather
    than to whichever lane happened to pick it up. The worker still gets a
    unique per-card name, but it carries the seat instead of the lane after the
    standard ``pi-`` worker prefix,
    so every claim, verdict and skmail this worker writes is attributable to the
    seat that owns the work.

    Card b2fec032 is the motivating case: it is the Integrator seat's triage
    card. Dispatched by lane it would have been claimed by pi-qwen-chiap01, and
    its verdicts would have carried no trace of the seat that owns the trunk.

    The name is validated rather than interpolated blindly. It reaches a shell
    command line, a tmux session, a claim owner and a mailbox name.
    """
    for label in folded_labels(cid, core):
        text = str(label).strip().lower()
        if not text.startswith(_SEAT_LABEL_PREFIX):
            continue
        seat = text[len(_SEAT_LABEL_PREFIX):]
        if not _SEAT_RE.match(seat):
            log(d, "WARN|%s|%s|ignoring malformed seat label %r" % (HOST, cid, text))
            continue
        return seat
    return None


def _seat_is_provisioned(seat):
    """True when public placement metadata provisions this seat somewhere."""
    return not _SEAT_PLACEMENT_ERROR and seat in _SEAT_PLACEMENT


def _is_niobe_builder_host(host, placement=None, placement_error=None):
    """Return whether this host carries the public Niobe dispatch placement."""
    mapping = _SEAT_PLACEMENT if placement is None else placement
    error = _SEAT_PLACEMENT_ERROR if placement_error is None else placement_error
    return not error and host in tuple(mapping.get("niobe", ()))


def _seat_owner(card_id, seat, pinned_host=None, placement=None, placement_error=None):
    """Return one seat host and a diagnostic without falling back to a lane."""
    if not seat:
        return _partition_owner(card_id, ROTATION_HOSTS, pinned_host), "ordinary"
    mapping = _SEAT_PLACEMENT if placement is None else placement
    error = _SEAT_PLACEMENT_ERROR if placement_error is None else placement_error
    if error:
        return None, "seat-manifest:%s" % error
    hosts = tuple(mapping.get(seat, ()))
    if not hosts:
        return None, "seat-unprovisioned:%s" % seat
    if pinned_host:
        if pinned_host not in hosts:
            return None, "seat-pin-conflict:%s:%s" % (seat, pinned_host)
        return pinned_host, "seat-pin:%s:%s" % (seat, pinned_host)
    return _partition_owner(card_id, hosts), "seat:%s" % seat


def _worker_owner(lane, cid, seat=None):
    """Return the reaper-compatible owner for one fleet worker."""
    return "pi-%s-%s-%s" % (seat or lane, HOST, cid)


def _worker_health_snapshot(session_names):
    """Join local tmux workers to exact current claim identities."""
    rows = []
    for session in session_names:
        lane = next(
            (item for item in LANES if session.startswith(item["prefix"])), None
        )
        if lane is None:
            continue
        cid = session[len(lane["prefix"]):]
        try:
            with open(os.path.join(CARDS, cid, "core.json"), encoding="utf-8") as fh:
                seat = seat_for(cid, json.load(fh))
        except (OSError, ValueError):
            seat = None
        expected_owner = _worker_owner(lane["name"], cid, seat)
        owner, _claimed_at, revision = _current_claim_identity_fresh(cid)
        rows.append((session, cid, owner == expected_owner and bool(revision)))
    duplicates = len(rows) - len({cid for _session, cid, _exact in rows})
    return {
        "sessions": len(rows),
        "claims_exact": sum(int(exact) for _session, _cid, exact in rows),
        "mismatched": sum(int(not exact) for _session, _cid, exact in rows),
        "duplicates": duplicates,
    }


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
                    try:
                        _o = json.loads(l)
                    except Exception:
                        continue
                    # Second reader, same hazard as event_rows(). A bare JSON
                    # string parses fine and then kills the sort below. Found
                    # 2026-08-31 when chiap04 kept failing AFTER event_rows()
                    # was guarded: one fix on one reader was not enough.
                    if isinstance(_o, dict):
                        out.append(_o)
            except OSError: pass
    return out

def _still_assignable(cid):
    """Return the fresh result from the same predicate used by the pool."""
    return authoritative_claimability(cid, fresh=True)["claimable"]

_BLOCKED_CATEGORIES = ("dependency", "card", "human", "capability")
_BLOCKED_ON_RE = re.compile(r"blocked[_\s-]?on", re.I)
_BLOCKED_CAT_RE = re.compile(r"\b(%s)\b" % "|".join(_BLOCKED_CATEGORIES), re.I)

_PIPE_VERDICT_RE = re.compile(
    r"^\s*BLOCKED\s*\|\s*(dependency|card|human|capability)\s*\|\s*([^|]+)", re.I)
_REFERENT_RE = re.compile(
    r"\breferent\b[\"']?\s*(?:[=:]\s*|\s+)[\"']?([A-Za-z0-9][\w:./@-]*)", re.I)
_CARD_REFERENT_RE = re.compile(r"^card:([0-9a-f]{8})$", re.I)
_AC_REFERENT_RE = re.compile(r"^ac:(\d+)$", re.I)
_AC_MENTION_RE = re.compile(r"\bac:(\d+)\b", re.I)
_WAKE_RETRY_LIMIT = 1
_COMBINED_BLOCKED_ON_RE = re.compile(
    r"^\s*(dependency|card|human|capability)\s+"
    r"(?:referent\s*[=:]\s*)?(card:[0-9a-f]{8}|approval:[\w./:@-]+|"
    r"ac:\d+|free|capability:[\w./:@-]+)\s*$",
    re.I,
)

def _parse_blocked_on_link(val):
    """Return (category, referent_or_None) from a blocked_on link value.

    Workers often write the category and referent into one link value
    (``dependency card:383a7834``). Accept that shape so a machine-readable
    dependency blocker still parks the exact review generation.
    """
    text=str(val or "").strip()
    if not text: return None
    lower=text.lower()
    if lower in _BLOCKED_CATEGORIES:
        return lower, None
    combined=_COMBINED_BLOCKED_ON_RE.match(text)
    if combined:
        return combined.group(1).lower(), combined.group(2).lower()
    fragment=_blocked_reason("BLOCKED blocked_on="+text)
    if fragment and len(fragment[1])==1:
        return fragment[0], fragment[1][0]
    return None

def _blocked_reason(val):
    """Return one actionable (category, referents), or None.

    The category and referent are a pair. Mixing categories, omitting a
    referent, or using a non-exact card id fails closed instead of buying a
    speculative retry.
    """
    text=str(val or "")
    if not re.match(r"^\s*BLOCKED\b",text,re.I): return None
    pipe=_PIPE_VERDICT_RE.match(text)
    categories=[]; direct=[]
    if pipe:
        categories.append(pipe.group(1).lower())
        direct.append(pipe.group(2).strip())
    for anchor in _BLOCKED_ON_RE.finditer(text):
        match=_BLOCKED_CAT_RE.search(text[anchor.end():anchor.end()+80])
        if match: categories.append(match.group(1).lower())
        window=text[anchor.end():anchor.end()+120].lstrip(" \t=\"':")
        combined=_COMBINED_BLOCKED_ON_RE.match(window)
        if combined:
            categories.append(combined.group(1).lower())
            direct.append(combined.group(2).strip())
    categories=list(dict.fromkeys(categories))
    if len(categories)!=1: return None
    refs=[m.group(1).rstrip(".,;|\"'") for m in _REFERENT_RE.finditer(text)]
    refs.extend(x.rstrip(".,;|\"'") for x in direct)
    # Accept bare card:<id> tokens after the category for combined link values.
    if not refs:
        bare=_CARD_REFERENT_RE.search(text)
        if bare:
            refs=["card:"+bare.group(1).lower()]
    refs=[x.lower() for x in dict.fromkeys(x for x in refs if x)]
    if not refs:
        refs=["ac:"+m.group(1) for m in _AC_MENTION_RE.finditer(text)]
    return (categories[0],tuple(dict.fromkeys(refs))) if refs else None

def _latest_blocked_reason(cid,verdict_ts,val):
    """Fold split blocked_on and referent links for the latest verdict."""
    if str(val or "")==_INVALID_NATIVE_OUTCOME: return None
    reason=_blocked_reason(val)
    if reason: return reason
    category=None; referents=[]
    for event in reversed(_load_evidence_events().get(cid,[])):
        if str(event.get("ts") or "")>=str(verdict_ts or ""): continue
        if event.get("action")!="link": continue
        key=_fold_key(event.get("link_key"))
        value=str(event.get("link_value") or "")
        if any(o in key for o in _OUTCOME_KEYS) and _OUTCOME_VALUE_RE.match(value):
            break
        if key=="referent" or "blocked_referent" in key:
            referents.extend(m.group(1).lower() for m in _REFERENT_RE.finditer("referent="+value))
        elif key=="blocked_on":
            fragment=_blocked_reason("BLOCKED blocked_on="+value)
            if fragment:
                category=fragment[0]
                referents.extend(fragment[1])
            else:
                match=_BLOCKED_CAT_RE.search(value)
                if match: category=match.group(1).lower()
        if category and referents:
            return category,tuple(dict.fromkeys(referents))
    return None

_PASS_RE = re.compile(r"^\s*PASS(?:_FOR_[A-Z_]+)?\b", re.I)

def _completion_epoch(cid):
    latest=0
    for event in event_rows(cid):
        if event.get("action")=="complete" or (
                event.get("action")=="move" and str(event.get("column") or "").lower()=="done"):
            latest=max(latest,_ts_epoch(event.get("ts")))
    return latest

def _material_label(event):
    label=str(_label_value(event) or "").strip().lower().replace("_","-")
    return label in {
        "needs-stronger-model","do-not-claim","not-claimable","human-gate",
        "sprint-container","foreign-project"}

def _authored_change_epoch(cid,threshold):
    latest=0
    for event in event_rows(cid):
        if event.get("action") in (
                "describe","amend_criteria","add_dependency","remove_dependency"):
            latest=max(latest,_ts_epoch(event.get("ts")))
    for event in _load_label_events().get(cid,[]):
        if _material_label(event): latest=max(latest,_ts_epoch(event.get("ts")))
    return latest if latest>threshold else 0

def _human_gate(cid):
    try: core=json.load(open(os.path.join(CARDS,cid,"core.json")))
    except Exception: return False
    labels={str(x).strip().lower().replace("_","-") for x in folded_labels(cid,core)}
    return "human-gate" in labels or "[HUMAN]" in str(core.get("title") or "").upper()

def _human_resolution_epoch(cid,referent,threshold):
    target_match=_CARD_REFERENT_RE.match(referent)
    target=target_match.group(1).lower() if target_match else cid
    needle=re.sub(r"[^a-z0-9]+","",referent.lower())
    latest=0
    for event in event_rows(target):
        if event.get("action")!="void": continue
        actor=str(event.get("writer") or "").lower()
        if actor in ("chef","human") or "human-decision-recorder" in actor:
            latest=max(latest,_ts_epoch(event.get("ts")))
    for event in _load_evidence_events().get(target,[]):
        if event.get("action") not in ("link","add_label"): continue
        blob=" ".join(str(event.get(key) or "") for key in ("link_key","link_value","label"))
        flat=re.sub(r"[^a-z0-9]+","",blob.lower())
        actor=str(event.get("writer") or "").lower()
        authorized=actor in ("chef","human") or "human-decision-recorder" in actor
        key=str(event.get("link_key") or "").lower().replace("-","_")
        direct=bool(re.search(r"\b(APPROVE(?:D)?|VOID)\b",blob,re.I))
        gate_decision=("human" in key and ("approval" in key or "void" in key)
                       and "no_approval" not in blob.lower())
        if authorized and (direct or gate_decision) and (
                target_match is not None or (needle and needle in flat)):
            latest=max(latest,_ts_epoch(event.get("ts")))
    return latest if latest>threshold else 0

def _dependency_state_change_epoch(dep, threshold):
    """Return the latest material change on a dependency card after threshold.

    Publishing reachable candidate bytes, linking evidence, or completing the
    dependency are all wake signals. Exact unresolved dependency blockers must
    stay ineligible until one of those changes lands.
    """
    latest=_completion_epoch(dep)
    material_keys={
        "evidence","evidence_sha256","candidate_evidence_sha256","candidate_path",
        "artifact_path","artifact_sha256","pr","pull_request","open_pr",
        "source_commit","reverse_patch_sha256",
    }
    for event in _load_evidence_events().get(dep,[]):
        if event.get("action")!="link":
            continue
        key=_fold_key(event.get("link_key"))
        if key in material_keys or any(token in key for token in (
                "evidence","candidate","artifact","patch","commit","pr")):
            latest=max(latest,_ts_epoch(event.get("ts")))
    for event in event_rows(dep):
        if event.get("action") in (
                "complete","describe","amend_criteria","add_dependency",
                "remove_dependency") or (
                event.get("action")=="move" and
                str(event.get("column") or "").lower()=="done"):
            latest=max(latest,_ts_epoch(event.get("ts")))
    return latest if latest>threshold else 0

def _blocker_change_epoch(cid,verdict_ts,val):
    """Return the exact blocker generation that can fund one retry."""
    reason=_latest_blocked_reason(cid,verdict_ts,val)
    if not reason: return 0
    category,referents=reason
    threshold=_ts_epoch(verdict_ts)
    if category=="dependency":
        if len(referents)!=1: return 0
        match=_CARD_REFERENT_RE.match(referents[0])
        if not match: return 0
        dep=match.group(1).lower()
        exact_events=[event for event in event_rows(cid)
                      if event.get("action") in ("add_dependency","remove_dependency")
                      and str(_dependency_value(event) or "").lower()==dep]
        removed=max((_ts_epoch(event.get("ts")) for event in exact_events
                     if event.get("action")=="remove_dependency"),default=0)
        folded={str(x).lower() for x in folded_dependencies(cid)}
        if dep not in folded and removed>threshold:
            return removed
        # Named dependency blockers wake when the referent card itself changes,
        # even when the review card never recorded an add_dependency edge.
        # add_dependency alone is not a wake: the unresolved referent must move.
        return _dependency_state_change_epoch(dep,threshold)
    if category=="human":
        if not referents: return 0
        changes=[_human_resolution_epoch(cid,ref,threshold) for ref in referents]
        return max(changes) if changes and all(changes) else 0
    if category=="capability":
        if not all(_AC_REFERENT_RE.match(ref) or ref=="free" for ref in referents):
            return 0
        return _authored_change_epoch(cid,threshold)
    if category!="card": return 0
    card_refs=[]; ac_refs=[]
    for ref in referents:
        match=_CARD_REFERENT_RE.match(ref)
        if match: card_refs.append(match.group(1).lower())
        elif _AC_REFERENT_RE.match(ref): ac_refs.append(ref)
        else: return 0
    criteria=["ac:"+m.group(1) for m in _AC_MENTION_RE.finditer(str(val or ""))]
    if card_refs and all(ref==cid for ref in card_refs) and criteria:
        card_refs=[]; ac_refs.extend(criteria)
    if ac_refs and not card_refs:
        return _authored_change_epoch(cid,threshold)
    if not card_refs or ac_refs: return 0
    generations=[]
    for ref in dict.fromkeys(card_refs):
        if not os.path.exists(os.path.join(CARDS,ref,"core.json")): return 0
        if lifecycle_state(ref)!="complete": return 0
        if _human_gate(ref):
            generation=_human_resolution_epoch(cid,"card:"+ref,threshold)
        else:
            if not _dep_satisfied(ref): return 0
            generation=_completion_epoch(ref)
        if generation<=threshold: return 0
        generations.append(generation)
    return max(generations) if generations else 0

def _wake_retry_available(cid,generation):
    """One exact claim-fenced retry per blocker generation."""
    retries=sum(1 for launched in _wake_launch_times.get(cid,()) if launched>generation)
    return retries<_WAKE_RETRY_LIMIT

def blocked_backoff(cid):
    """True if this card should stay out of the pool for now."""
    ts, val = _load_outcomes().get(cid, (None, None))
    # Missing or mixed blocker metadata fails closed. Guessing at its meaning
    # would turn an unresolved human or dependency hold into execution.
    if ts and re.match(r"^\s*BLOCKED", val, re.I):
        verdict_epoch=_ts_epoch(ts)
        # Reopen is the explicit operator escape hatch. It must be newer than
        # the authoritative BLOCKED evidence, not merely present in history.
        if any(event.get("action")=="reopen" and
               _ts_epoch(event.get("ts"))>verdict_epoch for event in event_rows(cid)):
            return False
        reason=_latest_blocked_reason(cid,ts,val)
        if not reason: return True
        # Capability is a routing signal, not a dependency or approval hold. It
        # must reach needs_escalation() below, which preferentially assigns it to
        # Codex. After that stronger route has also returned capability, park the
        # card until authored state changes so the fleet does not loop forever.
        if reason[0]=="capability":
            if not all(_AC_REFERENT_RE.match(ref) or ref=="free" for ref in reason[1]):
                return True
            strong_at=_strong_launched_at.get(cid,0)
            if not strong_at: return False
            change=_blocker_change_epoch(cid,ts,val)
            return not (change and strong_at<change)
        change=_blocker_change_epoch(cid,ts,val)
        if not change: return True
        return not _wake_retry_available(cid,change)
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
    # A pure pre-agent gateway failure is not card work, but retrying on every
    # timer tick would hammer the same unhealthy lane. Wait one bounded circuit
    # interval, then allow exactly one recovery probe. A failed probe writes a
    # fresh structured failure and starts a new bounded interval.
    if _transport_retry_held(cid):
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
    latest = _authored_change_epoch(cid,epoch)
    for dep in folded_dependencies(cid):
        if lifecycle_state(dep) != "complete":
            continue
        for event in event_rows(dep):
            if event.get("action") == "complete":
                latest = max(latest, _ts_epoch(event.get("ts")))
    return latest if latest > epoch else 0


def awaiting_review(cid):
    """True if this card produced a candidate and is waiting on a reviewer.

    Reported separately from blocked_backoff so that work which SUCCEEDED is not
    counted as work that refused.
    """
    ts, val = _load_outcomes().get(cid, (None, None))
    return bool(ts and _PASS_RE.match(str(val or "")))

def terminal_review_verdict(cid, core=None):
    """True when an independent review card already recorded PASS or FAIL."""
    labels = folded_labels(cid, core or {})
    if "review" not in {str(label).strip().lower() for label in labels}:
        return False
    ts, value = _load_outcomes().get(cid, (None, None))
    return bool(ts and re.match(r"^\s*(PASS|FAIL)\s*(?::|$)", str(value or ""), re.I))


def outcome_lifecycle_bucket(lifecycle, historical_review):
    """Classify outcome accounting without hiding an ambiguous board fold."""
    if lifecycle == "open":
        return "open"
    if lifecycle == "claimed":
        return "historical_review_claimed" if historical_review else "claimed"
    if lifecycle in {"complete", "void"}:
        return "historical_review_terminal" if historical_review else "terminal"
    return "ambiguous"

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
# Hosts a card may NAME, which is a wider set than the hosts it may RUN on:
# workstations and the control node are recognised so a card naming one is read
# as deliberate rather than as noise. chiap04 and chiap08 are listed twice once
# they join the rotation, which has always been harmless because this is only
# ever used for membership, but dict.fromkeys says so rather than leaving a
# reader to work it out.
KNOWN_HOSTS = tuple(dict.fromkeys(
    ROTATION_HOSTS + ("chiap04", "chiap08", "chiwk11", "chiwk12", "noroc2027")))

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
_TRANSPORT_RETRY_COOLDOWN_S = float(
    os.environ.get("SKFLEET_TRANSPORT_RETRY_COOLDOWN_S", "60")
)
_GATEWAY_ERROR_RE = re.compile(r"^\s*(404|408|429|502|504):\s*(\{.*\})\s*$", re.S)


def _structured_transport_failure(text):
    """Return a known pre-agent gateway failure kind, or None.

    The whole report must be one HTTP status plus one JSON object. This keeps
    arbitrary prose, partial agent output, and mixed reports substantive.
    """
    match = _GATEWAY_ERROR_RE.fullmatch(str(text or ""))
    if not match:
        return None
    try:
        payload = json.loads(match.group(2))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("message"), str):
        return None
    status = int(match.group(1))
    code = payload.get("code")
    if status == 404 and code in (404, "404", "not_found", "route_not_found"):
        return "gateway_404"
    if status == 429 and code in (429, "429", "rate_limit", "cooldown"):
        return "gateway_429"
    if status == 502 and code == "invalid_upstream_tool_calls":
        return "invalid_upstream_tool_calls"
    timeout_codes = {
        "first_token_timeout",
        "gateway_timeout",
        "timeout_before_first_token",
        "upstream_timeout",
    }
    if status in (408, 502, 504) and code in timeout_codes:
        return "first_token_timeout"
    return None


def _is_substantive_worker_report(text, card_mutated):
    """Fail closed unless this is a pure, known pre-agent transport failure."""
    if card_mutated:
        return True
    if not str(text or ""):
        return False
    return _structured_transport_failure(text) is None


def _launch_epoch_from_log(cid, filename):
    prefix = cid + "-"
    if not filename.startswith(prefix) or not filename.endswith(".log"):
        return 0
    stamp = filename[len(prefix):-4]
    try:
        return datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=datetime.timezone.utc
        ).timestamp()
    except ValueError:
        return 0


def _card_mutated_during_report(cid, started, finished):
    """Whether card work, rather than wrapper bookkeeping, occurred."""
    ignored = {
        "claim",
        "release_claim",
        "mero_observation",
        "review_assignment_launch",
        "review_assignment_recommendation",
    }
    for event in event_rows(cid):
        stamp = _ts_epoch(event.get("ts"))
        if started <= stamp <= finished + 1 and event.get("action") not in ignored:
            return True
    return False


def _local_launch_evidence(cid):
    """Return (logs seen, substantive reports, latest transport failure)."""
    seen = 0
    reports = 0
    latest_transport = 0
    cutoff = time.time() - _LAUNCH_TTL_H * 3600
    try:
        filenames = os.listdir(_LOGDIR)
    except OSError:
        return seen, reports, latest_transport
    for filename in filenames:
        started = _launch_epoch_from_log(cid, filename)
        if not started:
            continue
        fp = os.path.join(_LOGDIR, filename)
        try:
            stt = os.stat(fp)
            if stt.st_mtime < cutoff:
                continue
            with open(fp, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        seen += 1
        mutated = _card_mutated_during_report(cid, started, stt.st_mtime)
        if _is_substantive_worker_report(text, mutated):
            reports += 1
        elif _structured_transport_failure(text):
            latest_transport = max(latest_transport, stt.st_mtime)
    return seen, reports, latest_transport


_WORKER_EXIT_DIR = os.path.join(HOME, ".skcapstone/evidence/fleet-worker-exits")

def _latest_transport_failure_epoch(cid):
    """Return the latest claim-scoped pre-agent transport failure time."""
    latest = 0.0
    for path in glob.glob(os.path.join(_WORKER_EXIT_DIR, cid + "-*.json")):
        try:
            event = json.load(open(path, encoding="utf-8"))
            if event.get("card_id") == cid and event.get("transport_failure"):
                latest = max(latest, _ts_epoch(event.get("attempted_at")))
        except (OSError, TypeError, ValueError):
            continue
    return latest

def _transport_failure_logs(cid):
    """Return local stdout logs classified as pre-agent transport failures."""
    logs = set()
    for path in glob.glob(os.path.join(_WORKER_EXIT_DIR, cid + "-*.json")):
        try:
            event = json.load(open(path, encoding="utf-8"))
            if event.get("card_id") == cid and event.get("transport_failure"):
                logs.add(str(event.get("stdout_log") or ""))
        except (OSError, TypeError, ValueError):
            continue
    return logs

def _transport_retry_held(cid):
    """Hold a failed transport until the bounded recovery interval opens."""
    failed_at = _latest_transport_failure_epoch(cid)
    return bool(failed_at and time.time() - failed_at < _TRANSPORT_RETRY_COOLDOWN_S)

def _reporting_launches(cid):
    """Launches whose worker actually produced output, within the TTL."""
    n = 0
    transport_logs = _transport_failure_logs(cid)
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
            if f in transport_logs:
                continue          # pre-agent transport failure: not card work
            n += 1
    except OSError:
        pass
    return n

_ROTATION_EVID = os.path.join(HOME, ".skcapstone/evidence/fleet-rotation")
_shared_launch_cache = None
_TRANSPORT_FAILURE_CLASSES = frozenset({
    "rate_limited",
    "model_owner_backend_down",
    "backend_claims_quarantined",
    "invalid_upstream_tool_calls",
    "connection_failure",
})

def _transport_failure_claims():
    """Return exact claim generations that failed before agent work began."""
    failures = set()
    for path in glob.glob(os.path.join(_WORKER_EXIT_DIR, "*.json")):
        try:
            event = json.load(open(path, encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            continue
        claim = (
            str(event.get("card_id") or ""),
            str(event.get("host") or ""),
            str(event.get("owner") or ""),
            str(event.get("claim_revision") or ""),
        )
        failure = event.get("transport_failure")
        if (
            isinstance(failure, str)
            and failure in _TRANSPORT_FAILURE_CLASSES
            and all(claim)
        ):
            failures.add(claim)
    return failures

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
        transport_failures = _transport_failure_claims()
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
                                if len(parts) == 8:
                                    fields = [part.partition("=") for part in parts[4:]]
                                    if [(key, sep) for key, sep, _value in fields] == [
                                        ("lane", "="),
                                        ("model", "="),
                                        ("owner", "="),
                                        ("claim_revision", "="),
                                    ]:
                                        claim = (
                                            parts[3],
                                            parts[1],
                                            fields[2][2],
                                            fields[3][2],
                                        )
                                        if claim in transport_failures:
                                            continue
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
    local_evidence = globals().get("_local_launch_evidence")
    if callable(local_evidence):
        local_seen, local_reports, _latest_transport = local_evidence(cid)
        if local_seen:
            # The partition owner has the exact worker bytes. Prefer them over
            # the synced receipt, which cannot distinguish transport from work.
            return local_reports
    return _shared_launch_attempts(cid)

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
                    try:
                        _o = json.loads(l)
                    except Exception:
                        continue
                    # A bare JSON string parses fine and then kills the sort that
                    # follows. Guarded on 2026-08-31 after the SAME exception took
                    # the fleet down twice from two different readers.
                    if isinstance(_o, dict):
                        evs.append(_o)
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
def _parse_worker_owner(owner, cid, expected_seat=None):
    """Return (kind, lane or seat, host) for one exact worker owner."""
    owner = str(owner or "")
    cid = str(cid or "")
    if not re.fullmatch(r"[0-9a-f]{8}", cid):
        return None
    for host in ROTATION_HOSTS:
        for lane in ("codex", "glm", "qwen", "kimi", "escalate"):
            if owner == "pi-%s-%s-%s" % (lane, host, cid):
                return "lane", lane, host
        for lane in ("codex", "glm"):
            if owner == "%s-%s-%s" % (lane, host, cid):
                return "lane", lane, host
        if (
            expected_seat
            and _SEAT_RE.fullmatch(str(expected_seat))
            and owner == "pi-%s-%s-%s" % (expected_seat, host, cid)
        ):
            return "seat", str(expected_seat), host
    return None

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
            revision = e.get("claim_revision")
            raw = str(e.get("ts") or e.get("timestamp") or "")
            try:
                parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError("claim timestamp has no timezone")
                ts = parsed.timestamp()
            except (ValueError, OverflowError, OSError):
                ts = 0.0
        elif a in ("release_claim", "unassign", "complete", "void", "archive"):
            owner, ts, revision = None, 0.0, None
    return owner, ts, revision

def _load_ineffective():
    try:
        with open(_INEFFECTIVE_PATH, encoding="utf-8") as fh:
            payload = json.load(fh)
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]
    except (OSError, ValueError):
        return []


def _write_ineffective(entries):
    os.makedirs(os.path.dirname(_INEFFECTIVE_PATH), exist_ok=True)
    tmp = _INEFFECTIVE_PATH + ".new"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"schema_version": 1, "entries": entries}, fh, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, _INEFFECTIVE_PATH)


def _ineffective_suppresses(entries, cid, owner, claim_revision):
    return any(
        entry.get("card_id") == cid
        and entry.get("owner") == owner
        and entry.get("claim_revision") == claim_revision
        and entry.get("runtime_version") == REAP_RUNTIME_VERSION
        for entry in entries
    )


def _record_ineffective(cid, owner, claim_revision, failure_class):
    known = _load_ineffective()
    if _ineffective_suppresses(known, cid, owner, claim_revision):
        return
    known.append({
        "card_id": cid,
        "owner": owner,
        "claim_revision": claim_revision,
        "failure_class": failure_class,
        "runtime_version": REAP_RUNTIME_VERSION,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })
    try:
        _write_ineffective(known)
    except OSError:
        pass


def _remove_ineffective(cid, owner, claim_revision):
    known = _load_ineffective()
    retained = [
        entry for entry in known
        if not (
            entry.get("card_id") == cid
            and entry.get("owner") == owner
            and entry.get("claim_revision") == claim_revision
        )
    ]
    if retained == known:
        return
    try:
        _write_ineffective(retained)
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


def _startup_release_ready(report):
    """Recommend release only with current host-local negative proof and fence."""
    if report.get("host") != HOST or report.get("state") == "startup-ready":
        return False
    try:
        cid, owner, revision = (report[key] for key in
                                ("card_id", "owner", "claim_revision"))
        unit = _worker_unit_name(report["lane"], cid)
        cgroup = report["control_group"]
        if (not isinstance(cgroup, str) or not cgroup.startswith("/")
                or ".." in cgroup.split("/") or not cgroup.endswith("/" + unit)):
            return False
        pid = report["child_pid"]
        if not isinstance(pid, int) or pid <= 0 or Path("/proc", str(pid)).exists():
            return False
        status = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "--property=LoadState,ActiveState,MainPID,ControlPID"],
            capture_output=True, text=True, timeout=5)
        fields = dict(line.split("=", 1) for line in status.stdout.splitlines() if "=" in line)
        if fields.get("LoadState") == "not-found":
            if status.returncode not in {0, 1}:
                return False
        elif (status.returncode != 0 or fields.get("ActiveState") not in {"inactive", "failed"}
              or fields.get("MainPID") != "0" or fields.get("ControlPID") != "0"):
            return False
        group_path = Path("/sys/fs/cgroup" + cgroup)
        try:
            events = dict(line.split() for line in (group_path / "cgroup.events").read_text().splitlines())
            if events.get("populated") != "0":
                return False
        except FileNotFoundError:
            if group_path.exists():
                return False
        sessions = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5)
        if sessions.returncode == 0:
            if report["session_id"] in sessions.stdout.splitlines():
                return False
        elif sessions.returncode == 1:
            if "no server running" not in sessions.stderr:
                missing_socket = re.fullmatch(
                    r"error connecting to (/[^\n]+) \(No such file or directory\)",
                    sessions.stderr.strip())
                if not missing_socket:
                    return False
                try:
                    Path(missing_socket.group(1)).lstat()
                except FileNotFoundError:
                    pass
                else:
                    return False
        else:
            return False
        fresh_owner, _, fresh_revision = _current_claim_identity_fresh(cid)
        if (fresh_owner, fresh_revision) != (owner, revision):
            return False
        observation = StartupObservation(
            owner=owner, card_id=cid, session_id=report["session_id"],
            claim_revision=revision, expected_claim_revision=fresh_revision,
            heartbeat_seen=bool(report.get("heartbeat_at")),
            heartbeat_at=report.get("heartbeat_at"),
            executable_evidence_seen=bool(report.get("executable_evidence")),
            executable_evidence=report.get("executable_evidence"),
            process_alive=False, session_alive=False)
        return startup_actuation_fenced(
            observation, owner=fresh_owner, claim_revision=fresh_revision,
            now=datetime.datetime.now(datetime.timezone.utc))
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        return False


def _release_failed_startups():
    """Reconcile exited local startups on the next selector cycle, never kill."""
    if DRY:
        return
    directory = Path(_WORKER_EXIT_DIR).parent / "worker-startup"
    for path in directory.glob("*.json"):
        try:
            report = json.loads(path.read_text())
            if not isinstance(report, dict) or not _startup_release_ready(report):
                continue
            cid, owner, revision = (report[key] for key in
                                    ("card_id", "owner", "claim_revision"))
            log(d, "STARTUP_RELEASE_RECOMMENDED|%s|%s|owner=%s|claim_revision=%s" %
                (HOST, cid, owner, revision))
            result = subprocess.run(
                [SKC, "coord", "release-claim", cid, "--owner", owner,
                 "--expected-claim-revision", revision, "--agent", "jarvis"],
                capture_output=True, text=True, timeout=10)
            fresh_owner, _, fresh_revision = _current_claim_identity_fresh(cid)
            released = (fresh_owner, fresh_revision) != (owner, revision)
            log(d, "STARTUP_RELEASE_RESULT|%s|%s|rc=%s|released=%s" %
                (HOST, cid, result.returncode, released))
        except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
            log(d, "STARTUP_RELEASE_UNAVAILABLE|%s|%s" % (HOST, type(exc).__name__))


def _acts_fresh_rows(cid):
    """Every event for a card, straight from disk."""
    ev = os.path.join(CARDS, cid, "events")
    out = []
    if os.path.isdir(ev):
        for f in os.listdir(ev):
            try:
                for l in open(os.path.join(ev, f), encoding="utf-8", errors="replace"):
                    try:
                        _o = json.loads(l)
                    except Exception:
                        continue
                    # A bare JSON string parses fine and then kills the sort that
                    # follows. Guarded on 2026-08-31 after the SAME exception took
                    # the fleet down twice from two different readers.
                    if isinstance(_o, dict):
                        out.append(_o)
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
    """Whether exactly one strict launch recorded this exact claim generation."""
    global _fleet_launch_claims
    if not cid or not owner or not claim_revision:
        return False
    if _fleet_launch_claims is None:
        _fleet_launch_claims = collections.Counter()
        expected_seats = {}
        for path in glob.glob(os.path.join(EVID, "*", "actions*.log")):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if not line.startswith("LAUNCHED|"):
                            continue
                        parts = line.strip().split("|")
                        if len(parts) != 8 or not all(parts[1:4]):
                            continue
                        expected = ("lane", "model", "owner", "claim_revision")
                        fields = []
                        for part, key in zip(parts[4:], expected):
                            actual, separator, value = part.partition("=")
                            if separator != "=" or actual != key or not value:
                                break
                            fields.append(value)
                        if len(fields) != len(expected):
                            continue
                        lane, _model, launch_owner, launch_revision = fields
                        launch_cid = parts[3]
                        if launch_cid not in expected_seats:
                            try:
                                with open(
                                    os.path.join(CARDS, launch_cid, "core.json"),
                                    encoding="utf-8",
                                ) as core_fh:
                                    expected_seats[launch_cid] = seat_for(
                                        launch_cid, json.load(core_fh)
                                    )
                            except (OSError, ValueError):
                                expected_seats[launch_cid] = None
                        session_prefix = {
                            "codex": "codex-auto-",
                            "glm": "glm-auto-",
                            "qwen": "qwen-auto-",
                            "escalate": "esc-auto-",
                        }.get(lane)
                        parsed_owner = _parse_worker_owner(
                            launch_owner, launch_cid, expected_seats[launch_cid]
                        )
                        if (
                            parts[1] not in ROTATION_HOSTS
                            or session_prefix is None
                            or parts[2] != session_prefix + parts[3]
                            or parsed_owner is None
                            or parsed_owner[2] != parts[1]
                            or (parsed_owner[0] == "lane" and parsed_owner[1] != lane)
                        ):
                            continue
                        _fleet_launch_claims[(parts[3], launch_owner, launch_revision)] += 1
            except OSError:
                continue
    return _fleet_launch_claims[(str(cid), str(owner), str(claim_revision))] == 1


def _record_reap_outcome(cid, owner, claim_revision, claim_ts):
    """Record the dead worker's outcome and exact generation before release.

    All hosts derive the same event IDs and values. If Syncthing lets two hosts
    reach this point before either sees the other's append, their rows are
    byte-equivalent rather than conflicting. A repeated local attempt detects
    both IDs and appends nothing. The two evidence events remain separate from
    the structural ``release_claim`` event by design.
    """
    identity = "%s\0%s\0%s" % (cid, owner, claim_revision)
    verdict_id = hashlib.sha256(("fleet-reap-verdict-v1\0" + identity).encode()).hexdigest()
    evidence_id = hashlib.sha256(("fleet-reap-evidence-v1\0" + identity).encode()).hexdigest()
    # The claim time is part of the immutable generation. Using it here makes
    # independently appended rows byte-identical on every host.
    stamp = datetime.datetime.fromtimestamp(
        claim_ts, tz=datetime.timezone.utc).isoformat()
    rows = [
        {
            "event_id": verdict_id,
            "card_id": str(cid),
            "action": "verdict",
            "verdict": "WORKER_DIED",
            "writer": "fleet-liveness-reaper",
            "ts": stamp,
        },
        {
            "event_id": evidence_id,
            "card_id": str(cid),
            "action": "link",
            "link_key": "worker_died",
            "link_value": "owner=%s claim_revision=%s" % (owner, claim_revision),
            "writer": "fleet-liveness-reaper",
            "ts": stamp,
        },
    ]
    try:
        os.makedirs(_EVID_DIR, exist_ok=True)
        path = os.path.join(_EVID_DIR, "fleet-liveness-reaper.jsonl")
        with open(path, "a+b") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                existing = {}
                for number, line in enumerate(fh.read().splitlines(keepends=True), 1):
                    if not line.endswith(b"\n"):
                        raise ValueError("evidence line %d is partial" % number)
                    if not line.strip():
                        raise ValueError("evidence line %d is blank" % number)
                    parsed = json.loads(line[:-1].decode("utf-8"))
                    if not isinstance(parsed, dict):
                        raise ValueError("evidence line %d is not an object" % number)
                    canonical = (
                        json.dumps(parsed, separators=(",", ":"), sort_keys=True) + "\n"
                    ).encode("utf-8")
                    if line != canonical:
                        raise ValueError("evidence line %d is not canonical" % number)
                    event_id = str(parsed.get("event_id") or "")
                    if not event_id:
                        raise ValueError("evidence line %d has no event ID" % number)
                    if event_id in existing:
                        raise ValueError("evidence line %d duplicates event ID" % number)
                    existing[event_id] = line
                pending = []
                for row in rows:
                    canonical = (
                        json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n"
                    ).encode("utf-8")
                    current = existing.get(row["event_id"])
                    if current is not None and current != canonical:
                        raise ValueError("existing reaper event does not match canonical row")
                    if current is None:
                        pending.append(canonical)
                if pending:
                    fh.seek(0, os.SEEK_END)
                    fh.write(b"".join(pending))
                    fh.flush()
                    os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return True
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        log(d, "REAP_OUTCOME_FAILED|%s|%s|%s|%s" %
            (HOST, cid, owner, str(exc)[:120]))
        return False


def reap_dead_claims():
    """Return claims only after every authoritative host reports absence."""
    report_health = live_report()
    # Preserve the tuple seam used by focused reaper tests and older callers.
    # Production returns the richer mapping and therefore never weakens the
    # fixed all-host visibility gate.
    if isinstance(report_health, dict):
        oldest = report_health["oldest"]
        running = report_health["running"]
        nhosts = len(report_health["reporting"])
        known = len(report_health["expected"])
    else:
        oldest, running, nhosts = report_health
        known = nhosts
        report_health = {"faults": [], "expected": set(), "reporting": set()}
    for fault in report_health["faults"]:
        age = ("unknown" if fault["age_seconds"] is None
               else str(fault["age_seconds"]))
        log(d, "FLEET_LIVE_FAULT|%s|host=%s|reason=%s|age_seconds=%s|detail=%s"
            % (HOST, fault["host"], fault["reason"], age, fault["detail"]))
    health = _worker_health_snapshot(
        sh("tmux", "ls", "-F", "#{session_name}").split()
    )
    log(d, "WORKER_HEALTH|%s|sessions=%d claims_exact=%d mismatched=%d "
        "duplicates=%d" %
        (HOST, health["sessions"], health["claims_exact"], health["mismatched"],
         health["duplicates"]))
    if not oldest or nhosts < REAP_QUORUM:
        log(d, "REAP|%s|quorum_shortage reporting=%d known=%d need>=%d; reaped nothing"
            % (HOST, nhosts, known, REAP_QUORUM))
        return 0
    if not report_health.get("authoritative", nhosts >= known):
        missing = ",".join(sorted(report_health["expected"] -
                                  report_health["reporting"])) or "none"
        log(d, "REAP|%s|known_host_visibility_loss reporting=%d known=%d "
            "need>=%d missing=%s; reaped nothing"
            % (HOST, nhosts, known, REAP_QUORUM, missing))
        return 0
    freed = 0
    _ineffective = _load_ineffective()
    for cd in sorted(glob.glob(CARDS + "/*")):
        cid = os.path.basename(cd)
        if not os.path.exists(os.path.join(cd, "core.json")):
            continue
        if lifecycle_state(cid) != "claimed":
            continue
        owner, cts, claim_revision = _claim_identity(event_rows(cid))
        try:
            with open(os.path.join(cd, "core.json"), encoding="utf-8") as fh:
                expected_seat = seat_for(cid, json.load(fh))
        except (OSError, ValueError):
            expected_seat = None
        if not _parse_worker_owner(owner, cid, expected_seat):
            continue
        if cid in running:
            continue                      # a host says this is running right now
        if _ineffective_suppresses(_ineffective, cid, owner, claim_revision):
            continue                      # exact generation already failed on this runtime
        if not claim_revision:
            _log_once_per_hour(
                d, "REAP_EXCLUDED_CLAIM_REVISION_MISSING", cid,
                "REAP_EXCLUDED|%s|%s|%s|claim revision missing; exact release "
                "fence unavailable" % (HOST, cid, owner))
            continue
        if not cts:
            _log_once_per_hour(
                d, "REAP_EXCLUDED_CLAIM_TIMESTAMP_INVALID", cid,
                "REAP_EXCLUDED|%s|%s|%s|claim timestamp invalid; liveness age "
                "cannot be proved" % (HOST, cid, owner))
            continue
        if cts > time.time():
            log(d, "REAP_CLOCK_SKEW|%s|%s|%s|cached claim timestamp is in the "
                   "future; leaving it alone this tick" % (HOST, cid, owner))
            continue
        if oldest < cts + CLAIM_GRACE:
            continue                      # some host has not reported since the claim
        # Re-read the exact generation from disk immediately before releasing.
        # The pool was built seconds to minutes ago and the same owner may have
        # re-claimed the card since.
        fresh_owner, fresh_ts, fresh_revision = _current_claim_identity_fresh(cid)
        if not fresh_owner:
            continue                      # released by someone else in the meantime
        if fresh_owner != owner or fresh_revision != claim_revision:
            log(d, "REAP_RECLAIMED|%s|%s|was %s revision %s now %s revision %s; "
                   "leaving it alone this tick"
                % (HOST, cid, owner, claim_revision, fresh_owner,
                   fresh_revision or "missing"))
            continue
        if not fresh_ts:
            _log_once_per_hour(
                d, "REAP_EXCLUDED_FRESH_CLAIM_TIMESTAMP_INVALID", cid,
                "REAP_EXCLUDED|%s|%s|%s|fresh claim timestamp invalid; liveness "
                "age cannot be proved" % (HOST, cid, fresh_owner))
            continue
        if fresh_ts > time.time():
            log(d, "REAP_CLOCK_SKEW|%s|%s|%s|fresh claim timestamp is in the "
                   "future; leaving it alone this tick" % (HOST, cid, fresh_owner))
            continue
        if oldest < fresh_ts + CLAIM_GRACE:
            log(d, "REAP_GRACE|%s|%s|%s|fresh claim generation remains inside "
                   "grace; leaving it alone this tick" % (HOST, cid, fresh_owner))
            continue
        # Launch provenance is useful attribution, not a liveness gate. The old
        # code handed every unproven dead worker to a "stale-claim path" that did
        # not exist. Quorum plus absence from every report is the proof required
        # to act, including for hand-dispatched ephemeral workers.
        provenance = (_fleet_launch_provenance(cid, fresh_owner, fresh_revision)
                      and "fleet" or "ephemeral")
        health_evidence = hashlib.sha256(
            ("mero-worker-gone\0%s\0%s\0%s" %
             (cid, fresh_owner, fresh_revision)).encode()
        ).hexdigest()
        try:
            MeroObservation(
                card_id=cid,
                observation_id="mero-worker-gone-" + health_evidence[:32],
                state="worker_absent_after_quorum",
                process={"host": HOST, "sessions": [], "claim_revision": fresh_revision},
                evidence_sha256=health_evidence,
            ).append(Path(HOME) / ".skcapstone")
        except (BoundaryError, OSError, ValueError) as exc:
            log(d, "MERO_OBSERVATION_FAILED|%s|%s|%s" % (HOST, cid, exc))
            continue
        if not _record_reap_outcome(cid, fresh_owner, fresh_revision, fresh_ts):
            continue                    # never release without a durable outcome
        r = subprocess.run(
            [SKC, "coord", "release-claim", cid, "--owner", str(fresh_owner),
             "--expected-claim-revision", str(fresh_revision),
             "--agent", "jarvis"],
            capture_output=True, text=True)
        if r.returncode == 0:
            _rows.pop(cid, None)          # the fold below must re-read from disk
            # A zero exit is not proof the claim moved. When the two stores
            # disagree the CLI answers "Already released" and writes nothing, so
            # confirm against the fold rather than trusting the return code.
            if lifecycle_state(cid) == "claimed":
                _record_ineffective(
                    cid, fresh_owner, fresh_revision, "release_reported_success_noop"
                )
                log(d, "REAP_INEFFECTIVE|%s|%s|%s|release reported success but the "
                       "card is still claimed; CardStore and the legacy task store "
                       "disagree, needs repair" % (HOST, cid, owner))
                continue
            freed += 1
            _remove_ineffective(cid, fresh_owner, fresh_revision)
            log(d, "REAPED|%s|%s|%s|revision=%s provenance=%s; no reporting host "
                   "reports this card running" %
                (HOST, cid, owner, fresh_revision, provenance))
        else:
            # A release that keeps failing is a divergence, not a transient. Record
            # it after the first failure so it does not retry every five minutes
            # forever, which is how 2b614910 accumulated 455 pointless calls.
            _record_ineffective(cid, fresh_owner, fresh_revision, "release_command_failed")
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
    log(d, "DRY_SKIPPED|%s|reap_dead_claims, open_provisional_reviews, and "
           "close_reviewed_parents skipped; "
           "pass --go to mutate the board" % HOST)
else:
    _release_failed_startups()
    reap_dead_claims()

# ---- open provisional outcomes for review, then close reviewed work --------
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
_PROVISIONAL_PASS_RE = re.compile(r"^\s*(PASS_FOR_[A-Z_]+|PASS_READY_[A-Z_]+)\b", re.I)
_REVIEW_TITLE_RE = re.compile(r"\[(?:REVIEW|REREVIEW)\]", re.I)
_ID_RE = re.compile(r"\b([0-9a-f]{8})\b")
_GOVERNOR_REFUSAL_RE = re.compile(
    r"(?:Refusing (?:live review duplicate|third review level)|"
    r"Governed card .* requires exactly one parent-|Review ancestry)", re.I)
_REVIEW_REFUSALS = os.path.join(EVID, "provisional-review-refusals")

def _review_parent_ids(cid, core):
    """Return explicit parent labels, with legacy title text as a fallback."""
    labels = folded_labels(cid, core)
    parents = {
        str(label)[len("parent-"):]
        for label in labels
        if str(label).lower().startswith("parent-")
        and _ID_RE.fullmatch(str(label)[len("parent-"):])
    }
    if parents:
        return parents
    blob = str(core.get("title") or "") + " " + str(core.get("description") or "")
    return {match.group(1) for match in _ID_RE.finditer(blob) if match.group(1) != cid}

def _reviews_by_parent():
    """Map parent card id to governed review cards that name it."""
    out = {}
    for cd in glob.glob(CARDS + "/*"):
        cid = os.path.basename(cd)
        cp = os.path.join(cd, "core.json")
        if not os.path.exists(cp): continue
        try: core = json.load(open(cp))
        except Exception: continue
        title = str(core.get("title") or "")
        if not _REVIEW_TITLE_RE.search(title): continue
        for pid in _review_parent_ids(cid, core):
            out.setdefault(pid, set()).add(cid)
    return out

def _review_card_id(parent, outcome_ts, verdict):
    """Return one cross-host identity for one parent outcome generation."""
    key = "fleet-review-opener-v1\0%s\0%s\0%s" % (parent, outcome_ts, verdict.upper())
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def _outcome_event_value(event):
    """Return a typed outcome carried by one coordination event."""
    if event.get("action") in ("verdict", "blocked"):
        return _native_outcome_value(event)
    if event.get("action") == "evidence":
        return event.get("verdict")
    if event.get("action") == "link" and any(
            key in _fold_key(event.get("link_key")) for key in _OUTCOME_KEYS):
        raw = str(event.get("link_value") or "")
        match = _OUTCOME_VALUE_RE.match(raw) or _PIPE_OUTCOME_RE.search(raw)
        return match.group(1) if match else None
    return None


def _event_sort_key(event):
    return (
        str(event.get("ts") or ""),
        str(event.get("writer") or ""),
        str(event.get("event_id") or ""),
    )


def _event_identity(event):
    """Return the stable identity of one exact CardStore event."""
    return hashlib.sha256(json.dumps(
        event, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def _matching_outcome_events(card_id, outcome_ts, verdict):
    """Return the exact CardStore events carrying one folded outcome."""
    wanted = str(verdict or "").strip().upper()
    return [
        event for event in event_rows(card_id)
        if str(event.get("ts") or "") == str(outcome_ts or "")
        and str(_outcome_event_value(event) or "").strip().upper() == wanted
    ]


def _generation_invalidated(card_id, outcome_event):
    """Whether later source work made an outcome generation stale."""
    boundary = _event_sort_key(outcome_event)
    structural = {"describe", "amend_criteria", "add_dependency", "remove_dependency"}
    for event in event_rows(card_id):
        if _event_sort_key(event) <= boundary:
            continue
        action = str(event.get("action") or "")
        if (
            action == "link"
            and str(event.get("link_key") or "") == "review_join"
            and str(event.get("writer") or "") == "fleet-review-closer"
            and "source_event_sha256=%s" % _event_identity(outcome_event)
            in str(event.get("link_value") or "")
        ):
            continue
        if (
            action == "review_candidate_evidence"
            and str(event.get("source_outcome_ts") or "")
            == str(outcome_event.get("ts") or "")
            and str(event.get("source_verdict") or "").upper()
            == str(_outcome_event_value(outcome_event) or "").upper()
        ):
            continue
        if action in structural or action in {
                "verdict", "blocked", "evidence", "review_candidate_evidence"}:
            return True
        if action == "link":
            return True
        if action == "move" and str(event.get("column") or "") in {
                "backlog", "open", "ready", "doing"}:
            return True
    return False


def _parent_review_generation(parent, outcome_ts, verdict):
    """Return the current immutable producer generation, or fail closed."""
    events = _matching_outcome_events(parent, outcome_ts, verdict)
    identities = {
        json.dumps(event, sort_keys=True, separators=(",", ":")) for event in events
    }
    if len(identities) != 1:
        return None
    outcome_event = events[0]
    if _generation_invalidated(parent, outcome_event):
        return None
    candidate = _provisional_candidate(parent, outcome_ts, str(verdict).upper())
    if not candidate:
        return None
    producer, path, digest, commit, tree, ref = candidate
    generation = hashlib.sha256(json.dumps({
        "candidate_sha256": digest,
        "commit": commit,
        "outcome_event": json.loads(next(iter(identities))),
        "parent": parent,
        "ref": ref,
        "tree": tree,
        "verdict": str(verdict).upper(),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return generation, producer, path, digest, commit, tree, ref


def _review_names_generation(review_id, generation, outcome_ts, verdict):
    """Require the review's current PASS to name the exact parent generation."""
    try:
        with open(os.path.join(CARDS, review_id, "core.json"), encoding="utf-8") as fh:
            description = str(json.load(fh).get("description") or "")
    except (OSError, ValueError, TypeError):
        return False
    if "Outcome generation: %s." % generation not in description:
        return False
    matches = _matching_outcome_events(review_id, outcome_ts, verdict)
    return bool(
        len({json.dumps(event, sort_keys=True, separators=(",", ":"))
             for event in matches}) == 1
        and not _generation_invalidated(review_id, matches[0])
    )


def _review_join_value(parent, outcome_event, generation, review_id, review_event):
    """Return deterministic evidence joining exact source and review generations."""
    return (
        "generation=%s source=%s source_event_sha256=%s review=%s "
        "review_event_sha256=%s"
        % (
            generation,
            parent,
            _event_identity(outcome_event),
            review_id,
            _event_identity(review_event),
        )
    )


def _has_review_join(parent, value):
    return any(
        str(event.get("action") or "") == "link"
        and str(event.get("link_key") or "") == "review_join"
        and str(event.get("link_value") or "") == value
        and str(event.get("writer") or "") == "fleet-review-closer"
        for event in event_rows(parent)
    )

def _record_review_refusal(review_id, parent, outcome_ts, verdict):
    """Persist one stable refusal key so later rotations do not retry it."""
    os.makedirs(_REVIEW_REFUSALS, exist_ok=True)
    path = os.path.join(_REVIEW_REFUSALS, review_id + ".json")
    payload = json.dumps({
        "review_id": review_id,
        "parent": parent,
        "outcome_ts": outcome_ts,
        "verdict": verdict,
    }, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    try:
        os.write(fd, payload.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return True

def _provisional_candidate(parent, outcome_ts, token):
    """Return exact producer and durable candidate evidence for one outcome.

    Outcome attribution comes from verdict-bearing evidence, never lifecycle or
    structural links.  Missing, conflicting, inaccessible, or hash-mismatched
    candidate evidence fails closed.
    """
    matching = []
    rows = list(event_rows(parent)) + list(_load_evidence_events().get(parent, ()))
    for event in rows:
        if str(event.get("ts") or "") != str(outcome_ts or ""):
            continue
        value = None
        if event.get("action") in ("verdict", "blocked"):
            value = _native_outcome_value(event)
        elif event.get("action") == "evidence":
            value = event.get("verdict")
        elif event.get("action") == "link" and any(
                key in _fold_key(event.get("link_key")) for key in _OUTCOME_KEYS):
            raw = str(event.get("link_value") or "")
            match = _OUTCOME_VALUE_RE.match(raw) or _PIPE_OUTCOME_RE.search(raw)
            value = match.group(1) if match else None
        match = _PROVISIONAL_PASS_RE.match(str(value or ""))
        if match and match.group(1).upper() == token:
            matching.append(event)
    writers = {str(event.get("writer") or "").strip() for event in matching}
    if "" in writers or len(writers) != 1:
        return None
    producer = next(iter(writers))
    supplemental = {json.dumps(event, sort_keys=True, separators=(",", ":")): event
                    for event in rows
                    if event.get("action") == "review_candidate_evidence" and
                    str(event.get("source_outcome_ts") or "") == str(outcome_ts or "") and
                    str(event.get("source_verdict") or "").upper() == token and
                    str(event.get("producer") or "").strip() == producer}
    if len(supplemental) > 1:
        return None
    supplemental = list(supplemental.values())
    candidate_bytes = set()
    artifact_evidence = set()
    typed = {"candidate_commit": set(), "candidate_tree": set(), "candidate_ref": set()}

    def candidate_links(event):
        """Yield hash-bound candidate artifacts embedded in a verdict event."""
        links = event.get("evidence_links")
        if not isinstance(links, list):
            return
        for link in links:
            if (not isinstance(link, dict) or
                    not str(link.get("type") or "").startswith("candidate_")):
                continue
            path = os.path.expanduser(str(link.get("path") or ""))
            digest = str(link.get("sha256") or "").lower()
            if path and re.fullmatch(r"[0-9a-f]{64}", digest):
                yield path, digest

    for event in rows:
        source_event = event in supplemental
        if not source_event and (
                str(event.get("ts") or "") != str(outcome_ts or "") or
                str(event.get("writer") or "").strip() != producer):
            continue
        for path_key, digest_key, target in (
                ("candidate_path", "candidate_sha256", candidate_bytes),
                ("artifact_path", "artifact_sha256", artifact_evidence)):
            path = str(event.get(path_key) or "")
            digest = str(event.get(digest_key) or "").lower()
            if path and re.fullmatch(r"[0-9a-f]{64}", digest):
                target.add((os.path.expanduser(path), digest))
        candidate_bytes.update(candidate_links(event))
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        for key in typed:
            value = event.get(key, payload.get(key))
            if isinstance(value, str) and value.strip():
                typed[key].add(value.strip())
        evidence_path = os.path.expanduser(str(
            event.get("evidence_path") or event.get("evidence_link") or ""))
        evidence_digest = str(event.get("artifact_sha256") or "").lower()
        if evidence_path and re.fullmatch(r"[0-9a-f]{64}", evidence_digest):
            try:
                with open(evidence_path, "rb") as fh:
                    evidence_bytes = fh.read()
                if hashlib.sha256(evidence_bytes).hexdigest() == evidence_digest:
                    embedded = json.loads(evidence_bytes)
                    candidate_bytes.update(candidate_links(embedded))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
    candidates = candidate_bytes or artifact_evidence
    verified = []
    for path, digest in sorted(candidates):
        try:
            with open(path, "rb") as fh:
                actual = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            continue
        if actual == digest:
            verified.append((path, digest))
    if len(verified) != 1:
        return None
    has_typed = any(typed.values())
    if has_typed:
        if any(len(values) != 1 for values in typed.values()):
            return None
        commit = next(iter(typed["candidate_commit"])).lower()
        tree = next(iter(typed["candidate_tree"])).lower()
        ref = next(iter(typed["candidate_ref"]))
        if not (re.fullmatch(r"[0-9a-f]{40}", commit) and
                re.fullmatch(r"[0-9a-f]{40}", tree) and
                re.fullmatch(r"(?:refs/heads/|https://)\S+", ref)):
            return None
    else:
        commit = tree = ref = ""
    return producer, verified[0][0], verified[0][1], commit, tree, ref


def _eligible_provisional_reviews(capacity):
    """Return a deterministic prefix bounded by initial free review slots."""
    try:
        budget = max(0, int(capacity))
    except (TypeError, ValueError):
        return []
    if budget == 0:
        return []
    reviews = _reviews_by_parent()
    selected = []
    for parent, (outcome_ts, raw_verdict) in sorted(_load_outcomes().items()):
        if len(selected) >= budget:
            break
        if lifecycle_state(parent) != "open":
            continue
        match = _PROVISIONAL_PASS_RE.match(str(raw_verdict or ""))
        if not match:
            continue
        if any(lifecycle_state(cid) in {"open", "claimed"}
               for cid in reviews.get(parent, ())):
            continue
        token = match.group(1).upper()
        review_id = _review_card_id(parent, str(outcome_ts or ""), token)
        if os.path.isdir(os.path.join(CARDS, review_id)):
            continue
        if os.path.exists(os.path.join(_REVIEW_REFUSALS, review_id + ".json")):
            continue
        generation = _parent_review_generation(parent, outcome_ts, token)
        if not generation:
            _log_once_per_hour(
                d,
                "OPEN_REVIEW_EVIDENCE_BLOCKED",
                parent,
                "OPEN_REVIEW_EVIDENCE_BLOCKED|%s|%s|outcome=%s|%s" %
                (HOST, parent, str(outcome_ts or ""), token),
            )
            continue
        selected.append((parent, str(outcome_ts or ""), token, review_id) + generation)
    return selected


def _authoritative_review_readback(
        review_id, parent, producer, path, digest, generation,
        commit="", tree="", ref=""):
    """Fail closed unless CardStore folds the exact newly created review."""
    try:
        core_path = os.path.join(CARDS, review_id, "core.json")
        with open(core_path, encoding="utf-8") as fh:
            core = json.load(fh)
        parent_labels = [label for label in folded_labels(review_id, core)
                         if str(label).startswith("parent-")]
        description = str(core.get("description") or "")
        typed = not commit or all(
            value in description for value in (
                "Candidate commit: %s." % commit,
                "Candidate tree: %s." % tree,
                "Candidate ref: %s." % ref,
            )
        )
        return bool(
            core.get("id") == review_id and
            parent_labels == ["parent-%s" % parent] and
            lifecycle_state(review_id) == "open" and
            "Producer identity: %s." % producer in description and
            "Candidate evidence: %s sha256=%s." % (path, digest) in description and
            "Outcome generation: %s." % generation in description and
            typed
        )
    except (OSError, ValueError, TypeError):
        return False


_REVIEW_READBACK_BLOCKED = set()


def open_provisional_reviews(capacity, dry_run=False):
    """Plan or create a bounded batch of governed provisional-pass reviews."""
    selected = _eligible_provisional_reviews(capacity)
    log(d, "REVIEW_BATCH_PLAN|%s|capacity=%d|eligible=%d|batch=%d|dry_run=%s" %
        (HOST, max(0, int(capacity)), len(selected), len(selected),
         str(bool(dry_run)).lower()))
    if dry_run:
        for row in selected:
            parent, review_id = row[0], row[3]
            log(d, "WOULD_OPEN_REVIEW|%s|%s|review=%s" % (HOST, parent, review_id))
        return len(selected)

    opened = 0
    for (parent, outcome_ts, token, review_id, generation, producer, path, digest,
         commit, tree, ref) in selected:
        # Each attempted create consumes one unit of the initial capacity budget,
        # whether it succeeds or fails.  A transient failure stops the batch.
        description = (
            "Independently review parent %s at outcome %s (%s). Producer identity: %s. "
            "Candidate evidence: %s sha256=%s. Outcome generation: %s. "
            "Reviewer identity must differ."
            % (parent, outcome_ts or "unknown", token, producer, path, digest, generation)
        )
        if commit:
            description += (
                " Candidate commit: %s. Candidate tree: %s. Candidate ref: %s."
                % (commit, tree, ref)
            )
        r = subprocess.run(
            [SKC, "coord", "create", "--id", review_id,
             "--title", "[REVIEW] Review provisional outcome for %s" % parent,
             "--desc", description,
             "--priority", "high", "--tag", "parent-%s" % parent,
             "--tag", "review", "--tag", "qwen-suitable",
             "--tag", "source-implementer-%s" % producer,
             "--by", "fleet-review-opener",
             "--criteria", "Verify exact candidate %s at sha256 %s." % (path, digest),
             "--criteria", "Verify exact parent outcome generation %s." % generation,
             "--criteria", "Reviewer identity must differ from source implementer %s." % producer,
             "--criteria", "Record a leading PASS or FAIL verdict with immutable evidence."],
            capture_output=True, text=True,
            env=dict(os.environ, SKCOORD_CARD_STORE="1"))
        if r.returncode == 0:
            _rows.pop(review_id, None)
            if not _authoritative_review_readback(
                    review_id, parent, producer, path, digest, generation,
                    commit, tree, ref):
                _REVIEW_READBACK_BLOCKED.add(review_id)
                log(d, "OPEN_REVIEW_STALE_READBACK|%s|%s|review=%s" %
                    (HOST, parent, review_id))
                break
            opened += 1
            reviews = _reviews_by_parent()
            if review_id not in reviews.get(parent, set()):
                _REVIEW_READBACK_BLOCKED.add(review_id)
                log(d, "OPEN_REVIEW_LINEAGE_READBACK_FAILED|%s|%s|review=%s" %
                    (HOST, parent, review_id))
                break
            log(d, "OPENED_REVIEW|%s|%s|review=%s|%s|producer=%s|sha256=%s" %
                (HOST, parent, review_id, token, producer, digest))
            continue
        current = _reviews_by_parent().get(parent, set())
        if any(lifecycle_state(cid) in {"open", "claimed"} for cid in current):
            log(d, "OPEN_REVIEW_RACED|%s|%s|review=%s" % (HOST, parent, review_id))
            continue
        error = ((r.stderr or "") + " " + (r.stdout or "")).strip()
        if _GOVERNOR_REFUSAL_RE.search(error):
            if _record_review_refusal(review_id, parent, outcome_ts, token):
                log(d, "OPEN_REVIEW_REFUSED|%s|%s|review=%s|%s" %
                    (HOST, parent, review_id, error[:110]))
            continue
        log(d, "OPEN_REVIEW_FAILED|%s|%s|review=%s|%s" %
            (HOST, parent, review_id, error[:110]))
        break
    return opened

def close_reviewed_parents():
    """Complete cards whose independent review is complete and PASSED."""
    if HOST != AUTHORITY_HOST:
        return 0
    outcomes = _load_outcomes()
    closed = 0
    for parent, reviews in _reviews_by_parent().items():
        if not os.path.isdir(os.path.join(CARDS, parent)): continue
        if lifecycle_state(parent) != "open": continue
        _pts, pval = outcomes.get(parent, (None, None))
        match = _PROVISIONAL_PASS_RE.match(str(pval or ""))
        if not match: continue
        generation = _parent_review_generation(parent, _pts, match.group(1).upper())
        if not generation: continue
        generation_id = generation[0]
        for rev in sorted(reviews):
            if lifecycle_state(rev) != "complete": continue
            _rts, rval = outcomes.get(rev, (None, None))
            rv = str(rval or "")
            if not rv.strip() or not _PASS_ONLY_RE.match(rv):
                continue          # BLOCKED, or said nothing: the parent stays open
            if not _review_names_generation(rev, generation_id, _rts, rv):
                continue
            parent_event = _matching_outcome_events(
                parent, _pts, match.group(1).upper()
            )[0]
            review_event = _matching_outcome_events(rev, _rts, rv)[0]
            join_value = _review_join_value(
                parent, parent_event, generation_id, rev, review_event
            )
            if not _has_review_join(parent, join_value):
                r = subprocess.run(
                    [SKC, "coord", "link", parent, "review_join", join_value,
                     "--agent", "fleet-review-closer"],
                    capture_output=True, text=True)
                if r.returncode != 0:
                    log(d, "CLOSE_JOIN_FAILED|%s|%s|%s" % (
                        HOST, parent, (r.stderr or "").strip()[:110]))
                    break
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


def _durable_review_outcome(cid):
    """Return a terminal review verdict backed by readable hashed evidence."""
    rows = list(event_rows(cid)) + list(_load_evidence_events().get(cid, ()))
    rows.sort(key=lambda event: (str(event.get("ts") or ""), str(event.get("event_id") or "")))
    for outcome_index in range(len(rows) - 1, -1, -1):
        outcome = rows[outcome_index]
        value = ""
        if outcome.get("action") in ("verdict", "blocked"):
            value = str(_native_outcome_value(outcome) or "")
        elif outcome.get("action") == "link" and any(
                key in _fold_key(outcome.get("link_key")) for key in _OUTCOME_KEYS):
            value = str(outcome.get("link_value") or "")
        if not re.match(r"^\s*(PASS|FAIL|BLOCKED)\s*(?::|$)", value, re.I):
            continue
        writer = str(outcome.get("writer") or "")
        evidence_path = str(outcome.get("evidence") or outcome.get("evidence_path") or "")
        evidence_sha = str(
            outcome.get("evidence_sha256") or outcome.get("artifact_sha256") or ""
        ).lower()
        for event in rows[outcome_index + 1:]:
            if str(event.get("writer") or "") != writer:
                continue
            key = _fold_key(event.get("link_key"))
            if any(outcome_key in key for outcome_key in _OUTCOME_KEYS):
                break
            if key in ("evidence", "review_evidence"):
                evidence_path = str(event.get("link_value") or "")
            elif key in ("evidence_sha256", "review_evidence_sha256"):
                evidence_sha = str(event.get("link_value") or "").lower()
        if not evidence_path or not re.fullmatch(r"[0-9a-f]{64}", evidence_sha):
            return None
        try:
            with open(os.path.expanduser(evidence_path), "rb") as handle:
                if hashlib.sha256(handle.read()).hexdigest() != evidence_sha:
                    return None
        except OSError:
            return None
        return value
    return None


def release_finished_review_claims():
    """Release exact local review generations after durable terminal outcomes."""
    if HOST != AUTHORITY_HOST or DRY:
        return 0
    released = 0
    owner_pattern = re.compile(
        r"^pi-(?:seraph|link|codex-review|glm-review)-%s-([0-9a-f]{8})$"
        % re.escape(HOST)
    )
    for card_dir in sorted(glob.glob(os.path.join(CARDS, "*"))):
        cid = os.path.basename(card_dir)
        owner, _claimed_at, revision = _current_claim_identity_fresh(cid)
        match = owner_pattern.fullmatch(str(owner or ""))
        if not match or match.group(1) != cid or not revision:
            continue
        verdict = _durable_review_outcome(cid)
        if verdict is None:
            continue
        process = _card_process_snapshot(cid)
        if process["sessions"] or process["units"]:
            continue
        # The process check and the mutation are joined to one exact CardStore
        # generation. A new owner or revision loses the race and is untouched.
        fresh_owner, _fresh_at, fresh_revision = _current_claim_identity_fresh(cid)
        if fresh_owner != owner or fresh_revision != revision:
            continue
        result = subprocess.run(
            [SKC, "coord", "release-claim", cid, "--owner", owner,
             "--expected-claim-revision", revision, "--agent", "fleet-review-closer"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log(d, "REVIEW_RELEASE_FAILED|%s|%s|%s" % (
                HOST, cid, (result.stderr or result.stdout).strip()[:110]))
            continue
        _rows.pop(cid, None)
        global _outcomes
        _outcomes = None
        released += 1
        log(d, "REVIEW_RELEASED|%s|%s|owner=%s|claim_revision=%s|verdict=%s" % (
            HOST, cid, owner, revision, verdict[:40]))
    return released


def _legacy_selector_decision(cid, core_p):
    """Run the legacy selector's authoritative exclusion path for one card."""
    if cid in excluded:
        return {"eligible": False, "reason": "lifecycle_excluded"}
    if cid in _REVIEW_READBACK_BLOCKED:
        return {"eligible": False, "reason": "selector_excluded"}
    if unclaimable(cid):
        return {"eligible": False, "reason": "attempt_limit"}
    if itil_terminal(cid):
        return {"eligible": False, "reason": "terminal_itil"}
    lifecycle = lifecycle_state(cid)
    outcome_bucket = outcome_lifecycle_bucket(lifecycle, awaiting_review(cid))
    if outcome_bucket == "ambiguous":
        decision=authoritative_claimability(cid)
        return {
            "eligible": False,
            "reason": "malformed",
            "detail": decision.get("reason", "malformed:ambiguous-lifecycle"),
        }
    if outcome_bucket != "open":
        return {"eligible": False, "reason": outcome_bucket}
    if blocked_backoff(cid):
        return {
            "eligible": False,
            "reason": "awaiting_review" if awaiting_review(cid) else "backoff",
        }
    try:
        with open(core_p, encoding="utf-8") as handle:
            core = json.load(handle)
    except Exception:
        return {"eligible": False, "reason": "malformed", "detail": "malformed-core"}
    if terminal_review_verdict(cid, core):
        return {"eligible": False, "reason": "terminal_review"}
    decision=authoritative_claimability(cid,core)
    if not decision["claimable"]:
        return {
            "eligible": False,
            "reason": str(decision["reason"]),
            "decision": decision,
        }
    if str(decision["title"]).startswith("CMDB drift"):
        return {"eligible": False, "reason": "selector_excluded"}
    return {"eligible": True, "reason": "ready", "decision": decision, "core": core}

review_capacity = min(MAX_LAUNCH, sum(
    lane["free"] for lane in LANES if lane["name"] != "escalate"))
open_provisional_reviews(review_capacity, dry_run=DRY)
if not DRY:
    close_reviewed_parents()
    release_finished_review_claims()

_PINNED_IDS=set()
pool=[]
blocked=0
foreign_skipped=0
skipped_unclaimable=0
skipped_terminal=0
skipped_blocked=0
skipped_review=0
not_claimable_skipped=0
pinned_elsewhere=0
skipped_claimed=0
owned_ready=0
claimability_errors=[]
sensitive_withheld=0
historical_review_terminal=0
historical_review_claimed=0
structural_leaf=leaf_eligibility_counts(Path(HOME) / ".skcapstone").leaves
human_gated=0
ENG=("SKGW","SKCP","SKCOORD","SKHARNESS","SKMEM","CAPAUTH","FLEET","INC",
     "SKW","QWEN38","CARD-CORE","CARD-EVENT","SKDASH","SKGATEWAY","SKSEC","SKL-")
for cd in sorted(glob.glob(CARDS+"/*")):
    cid=os.path.basename(cd)
    core_p=os.path.join(cd,"core.json")
    if not os.path.exists(core_p): continue
    try:
        _structural_core = json.load(open(core_p))
    except Exception:
        _structural_core = {}
    if lifecycle_state(cid) == "open":
        human_gated += int(_human_gate(cid))
    legacy = _legacy_selector_decision(cid, core_p)
    legacy_reason = legacy["reason"]
    if legacy_reason == "selector_excluded" and cid in _REVIEW_READBACK_BLOCKED:
        if DRY:
            log(d,"DRY_SELECTION|%s|%s|excluded=stale-review-readback"%(HOST,cid))
    if not legacy["eligible"]:
        if legacy_reason in {"lifecycle_excluded", "selector_excluded"}:
            pass
        elif legacy_reason == "attempt_limit":
            skipped_unclaimable += 1
        elif legacy_reason in {"terminal_itil", "terminal_review"}:
            skipped_terminal += 1
        elif legacy_reason == "malformed":
            claimability_errors.append("%s:%s" % (cid, legacy.get("detail", "malformed")))
        elif legacy_reason in {"claimed", "historical_review_claimed"}:
            skipped_claimed += 1
            historical_review_claimed += int(legacy_reason == "historical_review_claimed")
        elif legacy_reason in {
            "complete",
            "void",
            "terminal",
            "historical_review_terminal",
        }:
            skipped_terminal += 1
            historical_review_terminal += int(legacy_reason == "historical_review_terminal")
        elif legacy_reason == "awaiting_review":
            skipped_review += 1
        elif legacy_reason == "backoff":
            skipped_blocked += 1
            if DRY:
                log(d,"DRY_SELECTION|%s|%s|excluded=authoritative-blocked-unchanged"%
                    (HOST,cid))
        elif legacy_reason in ("done", "void", "archive"):
            skipped_terminal += 1
        elif legacy_reason.startswith("owned-"):
            skipped_claimed += 1
            owned_ready += 1
        elif legacy_reason == "dependency":
            blocked += 1
        elif legacy_reason.startswith("host-pin:"):
            pinned_elsewhere += 1
        elif legacy_reason == "foreign-project":
            foreign_skipped += 1
        elif legacy_reason == "not-claimable":
            not_claimable_skipped += 1
        elif legacy_reason == "sensitive-category":
            sensitive_withheld += 1
        continue
    core=legacy["core"]
    decision=legacy["decision"]
    core=decision["core"]
    title=decision["title"]
    labels=decision["labels"]
    blob=(title+" "+json.dumps(labels)).upper()
    _pin=decision["host_pin"]
    if _pin == HOST:
        _PINNED_IDS.add(cid)
    up=title.upper().lstrip("[")
    # lane 0 SKLEGAL (Chef priority, Casey funded and waiting), 1 other engineering,
    # 2 business cards that need founder decisions an agent cannot supply.
    if up.startswith("SKLEGAL") or "SKLEGAL" in blob: lane=0
    elif any(up.startswith(e) for e in ENG): lane=1
    else: lane=2
    pool.append([lane,PRI.get(str(core.get("initial_priority")),4),cid,core,labels])

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
pool_ids=",".join(sorted(row[2] for row in pool)) or "-"
log(d,"POOL_IDS|%s|ids=%s"%(HOST,pool_ids))
# lane, then most-unblocking first, then priority, then stable id
pool.sort(key=lambda x:(x[0],-x[5],x[1],x[2]))
lc={0:0,1:0,2:0}
for x in pool: lc[x[0]]+=1
top=pool[0][5] if pool else 0
if claimability_errors:
    log(d,"CLAIMABILITY_EXCLUDED|%s|%s"%(HOST,",".join(claimability_errors)))
log(d,"POOL|%s|ready=%d sklegal=%d eng=%d biz=%d dep_blocked=%d "
      "unclaimable=%d claimed=%d itil_closed=%d blocked_backoff=%d "
      "awaiting_review=%d pinned_elsewhere=%d foreign=%d not_claimable=%d "
      "historical_review_terminal=%d historical_review_claimed=%d "
      "category_withheld=%d owned_ready=%d "
      "structural_leaf=%d human_gated=%d "
      "safety_filtered=%d top_unblocks=%d"
      %(HOST,len(pool),lc[0],lc[1],lc[2],blocked,skipped_unclaimable,
        skipped_claimed,skipped_terminal,skipped_blocked,skipped_review,
        pinned_elsewhere,foreign_skipped,not_claimable_skipped,
        historical_review_terminal,historical_review_claimed,
        sensitive_withheld,owned_ready,
        structural_leaf,human_gated,
        skipped_unclaimable+sensitive_withheld+not_claimable_skipped+foreign_skipped,top))

# Scheduler truth. POOL_V2 is the bounded admission partition. The legacy pool
# remains diagnostic only. SKCoord contributes read-only lifecycle classes
# through this adapter; it does not own runtime backoff, worker health, ITIL
# state, or host routing policy.
_POOL_V2_ADMISSIONS = {}
_POOL_V2_DECISIONS = None
_POOL_V2_FAILED = False
_POOL_V2_CLASSES = {}
_POOL_V2_EXCLUDED = set()


def _pool_v2_fingerprint(admission):
    """Return the stable identity of every fact used for admission."""
    return hashlib.sha256(json.dumps(admission, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _pool_v2_dispatchable(admission):
    """Allow only explicitly claimable work from the bounded snapshot."""
    if not isinstance(admission, dict):
        return False
    overlay = admission.get("overlay")
    if not isinstance(overlay, dict):
        return False
    # Unresolved BLOCKED dependency holds must not reach Seraph/elastic review
    # even when the seat admission bit is set. Measured 2026-09-12: 4cd4dd62
    # claim_revision 47e420668e074c8098f1cc0688f489c3 relaunched after an
    # unchanged blocked_on dependency because dispatchable ignored backoff.
    if overlay.get("backoff") is True:
        return False
    ordinary = (
        admission.get("claimable") is True
        and admission.get("reason") == "claimable"
    )
    seraph_review = (
        admission.get("claimable") is False
        and admission.get("reason") == "review"
        and admission.get("seraph_review_admitted") is True
    )
    elastic_review = (
        admission.get("claimable") is False
        and admission.get("reason") == "review"
        and admission.get("elastic_review_admitted") is True
    )
    return bool(
        isinstance(admission.get("card_id"), str)
        and isinstance(admission.get("core"), dict)
        and admission["core"].get("id") == admission["card_id"]
        and isinstance(admission.get("title"), str)
        and isinstance(admission.get("labels"), list)
        and re.fullmatch(r"[0-9a-f]{64}", str(admission.get("source_revision") or ""))
        and (ordinary or seraph_review or elastic_review)
    )


def _pool_v2_ready_ids(decisions, admissions, failed=False):
    """Return explicitly eligible rows with a dispatchable snapshot."""
    if failed:
        return set()
    return {
        row.card_id for row in decisions
        if row.eligible is True
        and _pool_v2_dispatchable(admissions.get(row.card_id))
    }


def _pool_v2_preclaim_matches(selected, fresh):
    """Require dispatchable, byte-identical admission facts."""
    return bool(
        isinstance(selected, dict)
        and isinstance(fresh, dict)
        and _pool_v2_dispatchable(fresh)
        and _pool_v2_fingerprint(fresh) == _pool_v2_fingerprint(selected)
    )


def _pool_v2_overlay(cid, core, reason):
    """Capture every non-claimability exclusion used by POOL_V2."""
    return {
        "lifecycle": lifecycle_state(cid),
        "itil_terminal": itil_terminal(cid),
        "superseded": cid in _POOL_V2_CLASSES.get("superseded_cards", set()),
        "excluded": cid in _POOL_V2_EXCLUDED,
        "review_readback": cid in _REVIEW_READBACK_BLOCKED,
        "terminal_review": bool(terminal_review_verdict(cid, core)),
        "awaiting_review": awaiting_review(cid),
        "backoff": blocked_backoff(cid),
        "attempt_limit": unclaimable(cid),
        "class_facets": sorted(
            name for name, ids in _POOL_V2_CLASSES.items() if cid in ids
        ),
        "reason": reason,
    }


def _pool_v2_admission(cid, core, claimability, fresh=False):
    """Build the complete bounded admission record for one card."""
    reason = str(claimability.get("reason") or "")
    folded_core = claimability.get("core") or core
    labels = claimability.get("labels") or ()
    governed_review = _governed_review_metadata(folded_core, labels) is not None
    review_seat = governed_review_seat(labels,qualified_reviewer_seats(folded_core))
    # Reason: overlay.backoff is the single admission hold bit (from
    # blocked_backoff in production). Read it here so Seraph/elastic bits and
    # _pool_v2_dispatchable share one source of truth, and extracted test
    # namespaces that stub only _pool_v2_overlay do not NameError.
    overlay = _pool_v2_overlay(cid, core, reason)
    hold = overlay.get("backoff") is True
    seraph_review_admitted = bool(
        globals().get("_ONLY_SEAT", "") == review_seat
        and review_seat is not None
        and reason == "review"
        and claimability.get("claimable") is False
        and governed_review
        and not hold
    )
    elastic_review_admitted = bool(
        not globals().get("_ONLY_SEAT", "")
        and reason == "review"
        and claimability.get("claimable") is False
        and governed_review
        and review_seat is not None
        and not hold
    )
    return {
        "card_id": cid,
        "claimable": claimability.get("claimable"),
        "reason": reason,
        "host_pin": claimability.get("host_pin"),
        "title": claimability.get("title"),
        "labels": claimability.get("labels"),
        "core": claimability.get("core"),
        "governed_review": governed_review,
        "seraph_review_admitted": seraph_review_admitted,
        "elastic_review_admitted": elastic_review_admitted,
        "overlay": overlay,
        "source_revision": claimability.get("source_revision"),
    }


def _pool_v2_authority_rows(decisions, admissions, failed, unblocks, priorities,
                            engineering_prefixes, host):
    """Build every dispatch row from the same bounded POOL_V2 snapshot."""
    ready_ids = _pool_v2_ready_ids(decisions, admissions, failed)
    rows = []
    pinned = set()
    for cid in sorted(ready_ids):
        if not re.fullmatch(r"[0-9a-f]{8}", cid):
            continue
        admission = admissions[cid]
        core = admission["core"]
        only_seat = globals().get("_ONLY_SEAT", "")
        seat_labels = {
            str(label).strip().lower()
            for label in admission["labels"]
            if str(label).strip().lower().startswith("seat-")
        }
        if only_seat and seat_labels != {"seat-" + only_seat}:
            continue
        if not only_seat and seat_labels & {"seat-tank", "seat-atlas"}:
            continue
        if only_seat in {"tank", "atlas"} and _CATEGORY_OPT_IN not in {
            str(label).strip().lower() for label in admission["labels"]
        }:
            continue
        if only_seat in {"tank", "atlas"} and _role_seat_metadata(core, only_seat) is None:
            continue
        title = admission["title"]
        labels = admission["labels"]
        blob = (title + " " + json.dumps(labels)).upper()
        upper = title.upper().lstrip("[")
        if admission["host_pin"] == host:
            pinned.add(cid)
        if upper.startswith("SKLEGAL") or "SKLEGAL" in blob:
            lane = 0
        elif any(upper.startswith(prefix) for prefix in engineering_prefixes):
            lane = 1
        else:
            lane = 2
        rows.append([
            lane, priorities.get(str(core.get("initial_priority")), 4),
            cid, core, labels, unblocks.get(cid, 0),
        ])
    rows.sort(key=lambda row: (row[0], -row[5], row[1], row[2]))
    return rows, pinned


def _pool_v2_owner_map(rows, host, pinned_ids):
    """Return exact stable host ownership for the authoritative rows."""
    owners = {}
    blocked = {}
    for row in rows:
        cid, core = row[2], row[3]
        elastic = _POOL_V2_ADMISSIONS.get(cid, {}).get("elastic_review_admitted") is True
        owner, reason = _seat_owner(
            cid, None if elastic else seat_for(cid, core), host if cid in pinned_ids else None
        )
        owners[cid] = owner if owner is not None else "unassigned:%s" % reason
        if owner is None:
            blocked[cid] = reason
    return owners, blocked


def _pool_v2_preclaim_handoff(cid, selected, fresh, reviewer):
    """Authorize review assignment only after the final admission comparison."""
    if not _pool_v2_preclaim_matches(selected, fresh):
        raise BoundaryError("POOL_V2 admission changed before claim")
    return _review_assignment(cid, fresh["core"], fresh["labels"], reviewer)


def _seraph_unique_source_heads(candidates):
    """Exclude every ambiguous source/head pair before claim or launch."""
    if globals().get("_ONLY_SEAT", "") != "seraph":
        return list(candidates), set()
    grouped = collections.Counter(
        (
            str((row[3].get("meta") or {}).get("link_source_card") or ""),
            str((row[3].get("meta") or {}).get("link_head_revision") or ""),
        )
        for row in candidates
    )
    duplicates = {key for key, count in grouped.items() if not all(key) or count > 1}
    return [
        row
        for row in candidates
        if (
            str((row[3].get("meta") or {}).get("link_source_card") or ""),
            str((row[3].get("meta") or {}).get("link_head_revision") or ""),
        )
        not in duplicates
    ], duplicates


def _shadow_pool_v2():
    global _POOL_V2_ADMISSIONS, _POOL_V2_CLASSES, _POOL_V2_DECISIONS
    global _POOL_V2_EXCLUDED
    classes = assessment.get("classes", {}) if isinstance(assessment, dict) else {}
    class_ids = {
        name: {str(row.get("card_id")) for row in rows if row.get("card_id")}
        for name, rows in classes.items()
        if isinstance(rows, list)
    }
    all_excluded = set(excluded)
    _POOL_V2_ADMISSIONS = {}
    _POOL_V2_CLASSES = class_ids
    _POOL_V2_EXCLUDED = all_excluded
    population = []
    for card_dir in sorted(glob.glob(CARDS + "/*")):
        cid = os.path.basename(card_dir)
        core_path = os.path.join(card_dir, "core.json")
        if not os.path.exists(core_path):
            continue
        adapter_facets = tuple(
            sorted("skcoord:" + name for name, ids in class_ids.items() if cid in ids)
        )
        try:
            with open(core_path, encoding="utf-8") as handle:
                core = json.load(handle)
            lifecycle = lifecycle_state(cid)
            claimability = authoritative_claimability(cid, core)
            reason = str(claimability.get("reason") or "")
            _POOL_V2_ADMISSIONS[cid] = _pool_v2_admission(
                cid, core, claimability
            )
            seraph_review_admitted = _POOL_V2_ADMISSIONS[cid].get(
                "seraph_review_admitted"
            ) is True
            elastic_review_admitted = _POOL_V2_ADMISSIONS[cid].get(
                "elastic_review_admitted"
            ) is True
            owner_health = None
            if reason.startswith("owned-"):
                if cid in class_ids.get("dead_worker_claims", set()):
                    owner_health = "dead"
                elif cid in class_ids.get("stale_claims", set()):
                    owner_health = "stale"
                else:
                    owner_health = "live"
            superseded = cid in class_ids.get("superseded_cards", set())
            mapped_exclusion = (
                superseded
                or cid in class_ids.get("dead_worker_claims", set())
                or cid in class_ids.get("stale_claims", set())
                or cid in class_ids.get("void_dependency_edges", set())
                or cid in class_ids.get("unreadable_cards", set())
            )
            population.append(
                SchedulerFacts(
                    card_id=cid,
                    malformed=(
                        claimability.get("claimable") not in {True, False}
                        or lifecycle == "ambiguous"
                    )
                    or reason.startswith("malformed:"),
                    lifecycle_excluded=cid in all_excluded and not mapped_exclusion,
                    selector_excluded=(
                        cid in _REVIEW_READBACK_BLOCKED
                        or terminal_review_verdict(cid, core)
                        or str(core.get("title") or "").startswith("CMDB drift")
                    ),
                    terminal_cardstore=lifecycle in {"complete", "void"},
                    terminal_itil=itil_terminal(cid),
                    superseded=superseded,
                    owner_health=owner_health,
                    human_gate=reason == "human-gate",
                    foreign_project=reason == "foreign-project",
                    not_claimable=reason in {"not-claimable", "non-task"},
                    sensitive_category=reason == "sensitive-category",
                    dependency=(
                        reason == "dependency"
                        or cid in class_ids.get("void_dependency_edges", set())
                    ),
                    awaiting_review=(
                        awaiting_review(cid) or reason == "review"
                    ) and not (seraph_review_admitted or elastic_review_admitted),
                    backoff=blocked_backoff(cid),
                    attempt_limit=unclaimable(cid),
                    host_pin_elsewhere=reason.startswith("host-pin:"),
                    adapter_facets=adapter_facets,
                )
            )
        except Exception as exc:
            _POOL_V2_ADMISSIONS[cid] = {
                "card_id": cid,
                "claimable": False,
                "reason": "malformed:%s" % type(exc).__name__,
            }
            population.append(
                SchedulerFacts(
                    card_id=cid,
                    malformed=True,
                    adapter_facets=("adapter_error:" + type(exc).__name__,),
                )
            )
    decisions = classify_scheduler_population(population)
    _POOL_V2_DECISIONS = decisions
    report = pool_v2(HOST, decisions)
    log(d, report.render())
    ready_ids = {row.card_id for row in decisions if row.eligible}
    legacy_ids = {row[2] for row in pool}
    only_v2 = sorted(ready_ids - legacy_ids)
    only_legacy = sorted(legacy_ids - ready_ids)
    log(
        d,
        "POOL_V2_PARITY|%s|match=%s only_v2=%d only_legacy=%d "
        "only_v2_ids=%s only_legacy_ids=%s"
        % (
            HOST,
            str(ready_ids == legacy_ids).lower(),
            len(only_v2),
            len(only_legacy),
            ",".join(only_v2) or "-",
            ",".join(only_legacy) or "-",
        ),
    )


def _emit_shadow_pool_v2():
    global _POOL_V2_FAILED
    try:
        _shadow_pool_v2()
    except Exception as exc:
        _POOL_V2_FAILED = True
        log(d, "SHADOW_ERROR|%s|%s:%s" % (HOST, type(exc).__name__, str(exc)[:160]))


_emit_shadow_pool_v2()

# POOL_V2 alone supplies dispatch candidates. Reuse legacy rows where present,
# then build missing rows only from the same admission snapshot. Any unknown or
# malformed state produces zero candidates rather than a legacy fallback.
_legacy_ready = len(pool)
pool, _PINNED_IDS = _pool_v2_authority_rows(
    _POOL_V2_DECISIONS or (), _POOL_V2_ADMISSIONS, _POOL_V2_FAILED,
    unblocks, PRI, ENG, HOST
)
pool, _DUPLICATE_SERAPH_SOURCE_HEADS = _seraph_unique_source_heads(pool)
_elastic_rows = [
    row
    for row in pool
    if _POOL_V2_ADMISSIONS[row[2]].get("elastic_review_admitted")
]
_elastic_limit = review_fanout_limit(
    len(_elastic_rows),
    max(
        0,
        CODEX_PHYSICAL_LIMIT
        - sum(len(lane["busy"]) for lane in LANES if lane["name"] == "codex"),
    ),
    REVIEW_MAXIMUM,
)
_elastic_ids = {row[2] for row in _elastic_rows[:_elastic_limit]}
pool = [
    row
    for row in pool
    if not _POOL_V2_ADMISSIONS[row[2]].get("elastic_review_admitted")
    or row[2] in _elastic_ids
]
for _source_head in sorted(_DUPLICATE_SERAPH_SOURCE_HEADS):
    log(
        d,
        "REVIEW_SOURCE_HEAD_WITHHELD|%s|source=%s|head=%s|reason=duplicate"
        % (HOST, _source_head[0] or "missing", _source_head[1] or "missing"),
    )
log(d, "POOL_AUTHORITY|%s|source=POOL_V2|ready=%d|legacy_ready=%d" %
    (HOST, len(pool), _legacy_ready))

# Partition the CARD SPACE by hash, not by pool index. Index striding assumes all
# three hosts see an identical pool at the same instant; ~/.skcapstone is Syncthing
# shared and claims land continuously, so the pools drift and strides collide.
# A hash partition is stable no matter what the local pool looks like.
off = ROTATION_HOSTS.index(HOST) if HOST in ROTATION_HOSTS else 0
_NHOST = len(ROTATION_HOSTS)
_OWNER_BY_ID, _SEAT_BLOCKED = _pool_v2_owner_map(pool, HOST, _PINNED_IDS)
for _cid, _reason in sorted(_SEAT_BLOCKED.items()):
    log(d, "SEAT_PLACEMENT_BLOCKED|%s|%s|%s" % (HOST, _cid, _reason))

def owner_host(cid):
    """Return the one stable host authorized to select this card."""
    return _OWNER_BY_ID.get(cid, "unassigned:not-authoritative")

def owns(cid):
    # A host-pinned card is owned by its pinned host, full stop. Letting the hash
    # partition also apply would strand any card whose pin and hash slice disagree:
    # pinned to chiap08 but hashed into chiap02's slice means NO host takes it.
    return owner_host(cid) == HOST
owned=[x for x in pool if owns(x[2])]

# Source-only logical-route cards belong to the Niobe builder path. Remove them
# from every regular host before any lane can claim them, then let only the host
# carrying Niobe's public placement publish the remote request.
_builder_candidates = [
    candidate
    for candidate in pool
    if builder_dispatch.eligible(
        dict(candidate[3], id=candidate[2]), candidate[4]
    )
]
_builder_candidate_ids = {candidate[2] for candidate in _builder_candidates}
owned = [candidate for candidate in owned if candidate[2] not in _builder_candidate_ids]

# Niobe may place one generic medium source card on a Ready builder standby.
# The remote node claims the card itself, so the CardStore fence remains the
# authority and this scheduler never impersonates a remote worker.
if _is_niobe_builder_host(HOST):
    for _candidate in tuple(_builder_candidates):
        _remote_core = dict(_candidate[3], id=_candidate[2])
        try:
            _remote_request = builder_dispatch.offer(
                default_fleet_paths(),
                _remote_core,
                _candidate[4],
                writer=fleet_store.Writer(
                    role="scheduler", node="niobe", identity="niobe"
                ),
            )
        except (builder_dispatch.BuilderDispatchError, OSError) as _exc:
            log(d, "BUILDER_DISPATCH_BLOCKED|%s|%s|%s" % (HOST, _candidate[2], _exc))
            continue
        if _remote_request is not None:
            log(
                d,
                "BUILDER_DISPATCH_OFFERED|%s|%s|node=%s|request_id=%s"
                % (
                    HOST,
                    _candidate[2],
                    _remote_request["node"],
                    _remote_request["request_id"],
                ),
            )
            break

# Never steal another host's hash slice without an authoritative shared lock.
# Syncthing propagation is not a compare-and-swap primitive. Measured 2026-08-28:
# all three hosts stole review card 334b8d63 and claimed it within 350 ms under
# one identity before any claim reached the other hosts. Stable ownership may
# leave capacity idle when the pool is tiny, but it preserves one card, one claim,
# one identity, and one worker. Safe work stealing requires a separate centralized
# admission design.
_ESCALATE_LABEL="needs-stronger-model"
_LANE_ONLY_LABELS={
    "codex-only":"codex",
    "qwen-only":"qwen",
    "glm-only":"glm",
    "kimi-only":"kimi",
    "escalation-only":"escalate",
}

_CAPABILITY_VERDICT_RE = re.compile(
    r"blocked_on[=: |]+\s*capability\b|^\s*BLOCKED\s*\|\s*capability\b", re.I)

_SEMANTIC_COMPLETE_ACTION="semantic_stage_complete"


def semantic_stage_completed(cid):
    """Require a completed artifact or an explicit separate implementation card."""
    for event in event_rows(cid):
        if event.get("action")==_SEMANTIC_COMPLETE_ACTION:
            payload=event.get("payload") if isinstance(event.get("payload"),dict) else event
            artifact=str(payload.get("artifact") or "")
            digest=str(payload.get("artifact_sha256") or payload.get("sha256") or "")
            if artifact and re.fullmatch(r"[0-9a-fA-F]{64}",digest):
                return True
    for event in _load_evidence_events().get(cid,[]):
        if event.get("action")!="link":
            continue
        if _fold_key(event.get("link_key"))!="semantic_downstream_implementation":
            continue
        match=_CARD_REFERENT_RE.match(str(event.get("link_value") or ""))
        if match and match.group(1).lower()!=cid and os.path.exists(
                os.path.join(CARDS,match.group(1).lower(),"core.json")):
            return True
    return False


def qwen_first_exclusive(cid,labels):
    normalized={str(label).strip().lower() for label in (labels or [])}
    return "qwen-first" in normalized and not semantic_stage_completed(cid)


def lane_compatibility(labels, escalation_required=False, qwen_allowed=True,
                       qwen_exclusive=False, qwen_enabled=True, glm_enabled=True):
    """Return compatible lanes and a stable routing reason."""
    normalized={str(label).strip().lower() for label in (labels or [])}
    required={lane for label,lane in _LANE_ONLY_LABELS.items() if label in normalized}
    if normalized & {"kimi", "kimi-only", "kimi-suitable", "kimi-lane"}:
        required.add("kimi")
    if qwen_exclusive:
        required.add(
            "qwen" if qwen_enabled or "qwen-only" in normalized else "codex"
        )
    if "glm-first" in normalized:
        required.add(
            "glm" if glm_enabled or "glm-only" in normalized else "codex"
        )
    if escalation_required:
        required.add("escalate")
    if len(required)>1:
        return (),"conflicting-lane-only:%s"%",".join(sorted(required))
    if required:
        lane=next(iter(required))
        return (lane,),"required-lane:%s"%lane
    ordinary=("qwen","glm","codex") if qwen_allowed else ("glm","codex")
    return ordinary,"ordinary"


def select_compatible_lane(
        labels, escalation_required, lane_order, remaining, qwen_allowed=True,
        qwen_exclusive=False, lane_health_by_name=None, qwen_enabled=True,
        glm_enabled=True):
    """Choose the first free compatible lane without consuming another lane."""
    compatible,reason=lane_compatibility(
        labels,escalation_required,qwen_allowed,qwen_exclusive,
        qwen_enabled,glm_enabled)
    if not compatible:
        return None,reason
    health=lane_health_by_name or {}
    healthy=[name for name in compatible
             if health.get(name,(True,"healthy"))[0]]
    for lane in lane_order:
        name=lane["name"] if isinstance(lane,dict) else str(lane)
        admitted=health.get(name,(True,"healthy"))[0]
        if name in compatible and admitted and remaining.get(name,0)>0:
            return name,"compatible"
    if not healthy:
        return None,"no-compatible-healthy-lane:%s"%",".join(compatible)
    return None,"no-free-lane:%s"%",".join(compatible)


def needs_escalation(cid, core=None, labels=None):
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
    if labels is None:
        try: labels=folded_labels(cid, core or {})
        except Exception: labels=[]
    if _ESCALATE_LABEL in {str(x).strip().lower() for x in (labels or [])}:
        return True
    try:
        _ts, _val = _load_outcomes().get(cid, (None, None))
    except Exception:
        return False
    return bool(_ts and _CAPABILITY_VERDICT_RE.search(str(_val or "")))

_QWEN_UNSUITABLE = re.compile(
    r"(capauth|credential|custody|issuer|secret|\bkey\b|rollback|deploy|"
    r"production|release|migrat|schema|architecture|\[HUMAN\]|\[XL\])", re.I)

def qwen_suitable(core):
    """Return whether Qwen may receive this card before a paid lane."""
    return not _QWEN_UNSUITABLE.search(str((core or {}).get("title") or ""))


def _lane_model(lane, core):
    if lane["name"]=="glm":
        return _glm_model_for(core) or lane["model"]
    if lane["name"]=="codex":
        return _size_model_for(core) or lane["model"]
    if lane["name"]=="kimi":
        return _kimi_model_for(core)
    return lane["model"]


_LANE_HEALTH_PATH=os.environ.get(
    "SKFLEET_LANE_HEALTH_PATH",
    os.path.join(HOME,".skcapstone/evidence/fleet-lane-health.json"))
_CAPACITY_DOMAINS={
    "codex":tuple(os.environ.get("SKFLEET_CODEX_CAPACITY_DOMAINS","codex").split(",")),
    "glm":tuple(os.environ.get("SKFLEET_GLM_CAPACITY_DOMAINS","zai").split(",")),
    "qwen":tuple(os.environ.get(
        "SKFLEET_QWEN_CAPACITY_DOMAINS","chiap01-qwen38,chiap08-qwen38").split(",")),
    "kimi":tuple(os.environ.get(
        "SKFLEET_KIMI_CAPACITY_DOMAINS","kimi-for-coding,kimi-k3").split(",")),
    "escalate":tuple(os.environ.get("SKFLEET_ESC_CAPACITY_DOMAINS","codex").split(",")),
}
_health_lanes=list(LANES)
for _glm_model in sorted(set(_GLM_LEVELS.values())):
    if _glm_model!=next(lane for lane in LANES if lane["name"]=="glm")["model"]:
        _health_lanes.append({"name":"glm","model":_glm_model})
for _codex_model in sorted(set(_SIZE_MODELS.values())):
    if _codex_model!=next(lane for lane in LANES if lane["name"]=="codex")["model"]:
        _health_lanes.append({"name":"codex","model":_codex_model})
for _kimi_model in ("kimi-for-coding", "k3"):
    _health_lanes.append({"name":"kimi","model":_kimi_model})
_cycle_id=new_cycle_id(HOST,STAMP)
_lane_health_snapshot=acquire_lane_snapshot(
    _GATEWAY_ENDPOINT,_health_lanes,_CAPACITY_DOMAINS,
    Path(_LANE_HEALTH_PATH),_cycle_id)
_active_gateway_revision=str(_lane_health_snapshot.get("runtime_revision") or "")


def _health_for(lane,model):
    return lane_health(
        _lane_health_snapshot,lane,model,cycle_id=_cycle_id,
        endpoint=_GATEWAY_ENDPOINT,capacity_domains=_CAPACITY_DOMAINS[lane],
        active_revision=_active_gateway_revision)

def _bounded_candidate_sequence(candidates, limit):
    """Return a stable, duplicate-free candidate sequence for one rotation.

    Candidate identity is the card id (the third tuple field).  The rotation
    must not repeatedly select a rejected card in the same cycle, while still
    allowing later compatible cards to fill a slot after a recoverable failure.
    """
    if limit <= 0:
        return []
    seen = set()
    result = []
    for candidate in candidates:
        # Rotation candidates are tuples, but keeping scalar ids valid makes the
        # helper safe for callers that have already projected the CardStore row.
        card_id = candidate[2] if isinstance(candidate, (tuple, list)) and len(candidate) > 2 else candidate
        if card_id in seen:
            continue
        seen.add(card_id)
        result.append(candidate)
        if len(result) >= limit:
            break
    return result

picks=[]; _i=0
remaining={lane["name"]:lane["free"] for lane in LANES}
_LANE_RANK={"qwen":0,"glm":1,"codex":2,"kimi":3,"escalate":4}
lane_order=sorted(LANES,key=lambda lane:_LANE_RANK.get(lane["name"],9))
_esc_waiting=0
_lane_deferred=collections.Counter()
_lane_deferred_cards={}
# Lane affinity, in both directions. An escalation card may go ONLY to the escalate
# lane, because returning it to a lane that already refused it just re-derives the
# same verdict. The escalate lane takes ONLY escalation cards, because the strong
# model is a scarce budget and must not be spent on work the cheap lanes can do.
#
# A card with no compatible free lane is SKIPPED, not allowed to end the loop. The
# earlier version broke out entirely when the head card could not be placed, which
# with lane affinity would let one waiting escalation card starve every ordinary
# card queued behind it.
# Scan a bounded, deterministic sequence once per cycle.  Rejected candidates
# are consumed by the scan and cannot be selected again during this rotation.
_candidate_scan = _bounded_candidate_sequence(owned, MAX_CANDIDATE_SCAN)
while _i<len(owned) and _i<len(_candidate_scan):
    _card=_candidate_scan[_i]; _i+=1
    _labels=_card[4]
    _esc=needs_escalation(_card[2], _card[3], _labels)
    _qwen_exclusive=qwen_first_exclusive(_card[2],_labels)
    _card_lane_health={lane["name"]:_health_for(
        lane["name"],_lane_model(lane,_card[3]))
        for lane in LANES}
    if not _ONLY_SEAT:
        _producer_routes=_producer_routes_for(
            _card[3],_labels,"codex" if "codex-only" in {
                str(label).strip().lower() for label in _labels} else None)
        _card_lane_health["codex"]=(
            bool(_producer_routes),"gateway-route-capacity" if _producer_routes else "unknown")
    if _ONLY_SEAT in {"link","mero","seraph"}:
        _card_lane_health["codex"]=(
            remaining.get("codex",0)>0,"review-route-capacity")
    _lane_name,_defer=select_compatible_lane(
        _labels,_esc,lane_order,remaining,qwen_suitable(_card[3]),_qwen_exclusive,
        _card_lane_health,QWEN_TARGET>0,GLM_TARGET>0)
    if _lane_name is None:
        _lane_deferred[_defer]+=1
        _lane_deferred_cards[_card[2]]=_defer
        if _defer.startswith("no-compatible-healthy-lane:"):
            details=",".join("%s=%s"%(name,state[1])
                             for name,state in sorted(_card_lane_health.items()))
            _log_once_per_hour(
                d,"lane_admission",_card[2],
                "LANE_ADMISSION_BLOCKED|%s|%s|%s|snapshot=%s"%
                (HOST,_card[2],details,_LANE_HEALTH_PATH))
        if DRY:
            log(d,"DRY_SELECTION|%s|%s|excluded=%s"%(HOST,_card[2],_defer))
        if _defer=="no-free-lane:escalate": _esc_waiting+=1
        continue
    _lane=next(lane for lane in LANES if lane["name"]==_lane_name)
    if DRY:
        log(d,"DRY_SELECTION|%s|%s|selected=%s|reason=%s"%
            (HOST,_card[2],_lane_name,"qwen-first" if _qwen_exclusive else "compatible"))
    picks.append((_lane,_card))
if _lane_deferred:
    log(d,"LANE_DEFER|%s|%s"%(HOST,",".join(
        "%s=%d"%(reason,_lane_deferred[reason]) for reason in sorted(_lane_deferred))))
if _esc_waiting:
    log(d,"ESCALATE_QUEUED|%s|%d card(s) need the stronger model; escalate lane full"
        %(HOST,_esc_waiting))

_review_withheld=[]
for _cid,_admission in sorted(_POOL_V2_ADMISSIONS.items()):
    _labels=_admission.get("labels") or ()
    if "review" not in {str(_label).strip().lower() for _label in _labels}:
        continue
    _overlay=_admission.get("overlay") or {}
    _reasons=governed_review_gate_reasons(
        _admission.get("core") or {},_labels,
        dependency_blocked=_overlay.get("reason")=="dependency",
        owned=str(_overlay.get("reason") or "").startswith("owned-"),
        capacity_available=_cid not in _lane_deferred_cards,
        dependency_blocker_holds=_overlay.get("backoff") is True,
    )
    if _cid in _SEAT_BLOCKED:
        _reasons=tuple((*_reasons,"wrong-seat"))
    if not _pool_v2_dispatchable(_admission) or _reasons:
        _fallback=str(_admission.get("reason") or "withheld")
        _review_withheld.append((_cid,",".join(dict.fromkeys(_reasons or (_fallback,)))))
for _cid,_reasons in _review_withheld[:12]:
    log(d,"REVIEW_WITHHELD|%s|card=%s|reasons=%s"%(HOST,_cid,_reasons))
if len(_review_withheld)>12:
    log(d,"REVIEW_WITHHELD_OMITTED|%s|count=%d"%(HOST,len(_review_withheld)-12))


def _observe_assigned_reviews():
    """Have Mero record current state for reviews launched by this host."""
    live_sessions = set(sh("tmux", "ls", "-F", "#{session_name}").split())
    live_units = active_worker_units()
    outcomes = _load_outcomes()
    for card_dir in glob.glob(os.path.join(CARDS, "*")):
        cid = os.path.basename(card_dir)
        rows = event_rows(cid)
        try:
            reconciled = reconcile_fanout_receipt(
                Path(HOME) / ".skcapstone",
                cid,
                live_sessions=live_sessions,
                live_units=live_units,
            )
            if reconciled is not None:
                log(d, "FANOUT_RECONCILED|%s|%s|state=%s" %
                    (HOST, cid, reconciled["state"]))
        except (FanoutBoundaryError, OSError, ValueError) as exc:
            log(d, "FANOUT_RECONCILE_FAILED|%s|%s|%s" % (HOST, cid, exc))
        receipts = [
            event for event in rows
            if event.get("action") == "review_assignment_launch"
            and event.get("claim_revision")
        ]
        observations = [
            event for event in rows
            if event.get("action") == "mero_observation"
            and isinstance(event.get("process"), dict)
        ]
        if not receipts or not observations:
            continue
        receipt = receipts[-1]
        prior = observations[-1]
        process = dict(prior["process"])
        if process.get("host") != HOST:
            continue
        session = str(process.get("session") or "")
        lifecycle = lifecycle_state(cid)
        _outcome_ts, outcome = outcomes.get(cid, (None, None))
        if lifecycle == "complete":
            state = "complete"
        elif re.match(r"^\s*BLOCKED\b", str(outcome or ""), re.I):
            state = "blocked"
        elif session in live_sessions:
            state = "active"
        elif _current_claim_identity_fresh(cid)[0]:
            state = "stale"
        else:
            state = "waiting"
        process.update({"session": session, "alive": session in live_sessions})
        evidence = hashlib.sha256(
            json.dumps(
                {
                    "card_id": cid,
                    "claim_revision": receipt["claim_revision"],
                    "state": state,
                    "process": process,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        observation_id = "mero-monitor-" + evidence[:32]
        try:
            MeroObservation(
                card_id=cid,
                observation_id=observation_id,
                state=state,
                process=process,
                evidence_sha256=evidence,
            ).append(Path(HOME) / ".skcapstone")
        except (BoundaryError, OSError, ValueError) as exc:
            log(d, "MERO_OBSERVATION_FAILED|%s|%s|%s" % (HOST, cid, exc))


if not picks:
    _observe_assigned_reviews()
    detail = _selection_diagnostic(
        pool, owned, LANES, owner_host, reporting_capacity())
    log(d,"SELECTION_EMPTY|%s|%s"%(HOST,detail))
    log(d,"NOOP|%s|selection empty: %s"%(HOST,detail))
    log(d,"NOOP_RECEIPT|%s|reason=%s|seat=%s"%
        (HOST,_noop_reason(pool,owned,_lane_deferred),_ONLY_SEAT or "generic"))
    sys.exit(0)

raced=0; _raced_ids=[]; lane_drift=0; claim_refused=0; admission_refused=0
launched=0
launch_receipts=0
processed_picks=0
launch_remaining={lane["name"]:lane["free"] for lane in LANES}
_review_route_reservations={}
logdir=os.path.join(HOME,".skcapstone/fleet/logs"); os.makedirs(logdir,exist_ok=True)
for _LANE,(_,_,cid,core,_labels,_nb) in picks:
    if launched>=MAX_LAUNCH or not any(launch_remaining.values()):
        break
    processed_picks+=1
    _attempt_escalation=needs_escalation(cid,core,_labels)
    _attempt_health={lane["name"]:_health_for(
        lane["name"],_lane_model(lane,core)) for lane in LANES}
    if not _ONLY_SEAT:
        _producer_routes=_producer_routes_for(
            core,_labels,"codex" if "codex-only" in {
                str(label).strip().lower() for label in _labels} else None)
        _attempt_health["codex"]=(
            bool(_producer_routes),"gateway-route-capacity" if _producer_routes else "unknown")
    _review_seat=governed_review_seat(
        _labels,
        qualified_reviewer_seats(core),
    )
    if _review_seat is not None:
        _attempt_health["codex"]=(
            launch_remaining.get("codex",0)>0,"review-route-capacity")
    _elastic_review = _POOL_V2_ADMISSIONS.get(cid, {}).get("elastic_review_admitted") is True
    _attempt_remaining = (
        {name: slots if name == "codex" else 0 for name, slots in launch_remaining.items()}
        if _elastic_review else launch_remaining
    )
    _attempt_lane_name,_attempt_defer=select_compatible_lane(
        _labels,_attempt_escalation,lane_order,_attempt_remaining,
        qwen_suitable(core),qwen_first_exclusive(cid,_labels),_attempt_health,
        QWEN_TARGET>0,GLM_TARGET>0)
    if _attempt_lane_name is None:
        log(d,"SKIPPED_ATTEMPT_ADMISSION|%s|%s|reason=%s"%
            (HOST,cid,_attempt_defer))
        continue
    _LANE=next(lane for lane in LANES if lane["name"]==_attempt_lane_name)
    try:
        unit=_worker_unit_name(_LANE["name"],cid)
    except ValueError as exc:
        _log_once_per_hour(
            d,"worker_unit_identity",cid,
            "UNSUPPORTED_CARD_ID|%s|%s|lane=%s|reason=%s"%
            (HOST,cid,_LANE["name"],exc))
        continue
    ac="\n".join("  %d. %s"%(i+1,x) for i,x in enumerate(core.get("acceptance_criteria") or []))
    # PREFIX CACHE ORDERING. vLLM caches on a shared PROMPT PREFIX. This brief
    # used to open with the card id and the card body, so every request diverged
    # within a few tokens and the ~1,700 invariant tokens after it could never be
    # reused. Measured before this change on chiap08: 45,613,120 hits against
    # 264,609,995 queries, a 17.2% hit rate. Invariant rails now come FIRST and the
    # card-specific text LAST, so every worker shares one long cacheable prefix.
    _RAILS=("CONSTRAINTS (standing rails, non-negotiable):\n"
      "- CardStore is append-only. Build JSON with a serializer and parse every line before appending. Never concatenate strings into JSON.\n"
      "- Join structural CardStore events with separate evidence events. Never infer a verdict from lifecycle state or from links alone.\n"
      + _worker_mail_instructions(
          _worker_mail_routing(os.environ, core.get("originator"))) +
      "MAIL CHECK CADENCE. Check mail after startup, before each major phase, and\n"
      "at least every five minutes during a long-running task. Mail does not interrupt\n"
      "a tool call, so process new instructions at the next safe boundary. Never\n"
      "treat an unanswered message as approval.\n"
      "Measured 2026-08-31: 63 agent messages, PASS and BLOCKED verdicts among them,\n"
      "had been written into ~/.skcapstone/mail/ and to ad-hoc .json and .txt files.\n"
      "NOTHING reads that directory. Those verdicts were produced and silently lost for\n"
      "five days and had to be recovered by hand. Writing anywhere else is the same as\n"
      "not reporting at all.\n"
      "\n"
      "TALK TO EACH OTHER. Mail is not only for reporting upward. If another agent holds\n"
      "a card yours depends on, or you find something that changes their work, mail them\n"
      "directly. Read your inbox before you start and before you finish: someone may have\n"
      "answered the question you are about to spend an hour on, or told you the card is\n"
      "void. Coordination beats duplicated effort.\n"
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
      "BOUNDED EVIDENCE DISCOVERY, mandatory when verifying sha256 digests:\n"
      "Do NOT recursively hash ~/.skcapstone, worktrees, /tmp, or the host.\n"
      "Card 4cd4dd62 burned 9h51m doing exactly that after a dependency blocker.\n"
      "Use only explicit card-referenced paths and evidence/work/<card_id>/ roots\n"
      "through skcapstone.fleet.bounded_artifact_discovery (time and file budgets).\n"
      "If the digest is absent inside those bounds, record BLOCKED on the producer\n"
      "dependency and stop. Never widen the search.\n"
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
      "- Never use an em dash or en dash.\n")
    brief=_RAILS + ("Work only SKCapstone card %s. The fleet selector has already claimed it "
      "for your exact agent identity. Verify that ownership before working and never "
      "claim or substitute another card. If ownership is absent, or a dependency is "
      "incomplete, say so and stop rather than working it anyway.\n\n"
      "COORDINATION WRITE BOUNDARY: Use skcapstone coord for every verdict, "
      "evidence, status, claim, label, dependency, and lifecycle write. Never create, "
      "append, rewrite, rename, or delete CardStore JSONL. Use CLI reads for normal "
      "verification; raw file inspection is emergency operator diagnostics only.\n\n"
      "CARD %s (%s)\nTITLE: %s\nDESCRIPTION: %s\n\nACCEPTANCE CRITERIA:\n%s\n\n" % (cid,cid,core.get("kind"),core.get("title"),core.get("description"),ac))
    _seat = None if _elastic_review else seat_for(cid, core)
    # A seat-owned card runs under the seat's identity, not the lane's. The
    # Worker identity stays lane-based so slot accounting, liveness, and reaping
    # remain unchanged; only the agent identity moves.
    name = (elastic_reviewer_identity(HOST, cid) if _elastic_review
            else _worker_owner(_LANE["name"], cid, _seat))
    if _seat:
        log(d, "SEAT|%s|%s|running under seat %s as %s" % (HOST, cid, _seat, name))
    if _seat == "tank":
        _artifact_sha256, = _role_seat_metadata(core, "tank")
        brief += (
            "\nTANK ROLE FENCE:\n"
            "- Release or install only the approved artifact with sha256=%s.\n"
            "- This exact governed release surface is the only exception to the "
            "generic no-deploy worker rail.\n"
            "- Do not author source, merge, approve your own work, or review this release.\n"
        ) % _artifact_sha256
    elif _seat == "atlas":
        _verification_target, _verification_evidence_sha256 = _role_seat_metadata(
            core, "atlas"
        )
        brief += (
            "\nATLAS ROLE FENCE:\n"
            "- Verify only target %s against evidence sha256=%s.\n"
            "- Record observed postconditions and a PASS, FAIL, or BLOCKED result.\n"
            "- Do not deploy, dispatch, invoke an actuator, or change the target.\n"
        ) % (_verification_target, _verification_evidence_sha256)
    sess="%s%s"%(_LANE["prefix"],cid)
    model=_logical_route_for(core)
    if model is None:
        log(d,"SKIPPED_LOGICAL_ROUTE|%s|%s|reason=missing-or-ambiguous-size"%
            (HOST,cid))
        continue
    pi_tools=pi_tool_allowlist(_labels)
    if DRY:
        log(d,"WOULD_LAUNCH|%s|%s|%s|lane=%s|model=%s|%s"%(HOST,sess,cid,_LANE["name"],model,str(core.get("title"))[:40]))
        launched+=1
        launch_remaining[_LANE["name"]]-=1
        continue
    _review_recommendation = None
    _review_handoff = None
    bf=os.path.join(logdir,"brief-%s.txt"%cid); open(bf,"w").write(brief)
    lf=os.path.join(logdir,"%s-%s.log"%(cid,STAMP))
    # Last-moment re-check through the same fold that built the pool.
    with open(os.path.join(CARDS,cid,"core.json"),encoding="utf-8") as _handle:
        _fresh_core=json.load(_handle)
    fresh_claimability=authoritative_claimability(cid,core=_fresh_core,fresh=True)
    _fresh_admission=_pool_v2_admission(
        cid,_fresh_core,fresh_claimability,fresh=True
    )
    _selected_admission=_POOL_V2_ADMISSIONS.get(cid)
    fresh_escalation=needs_escalation(
        cid,fresh_claimability["core"],fresh_claimability["labels"])
    compatible,affinity_reason=lane_compatibility(
        fresh_claimability["labels"],fresh_escalation,
        qwen_suitable(fresh_claimability["core"]),
        qwen_first_exclusive(cid,fresh_claimability["labels"]),
        QWEN_TARGET>0,GLM_TARGET>0)
    if _LANE["name"] not in compatible:
        lane_drift += 1
        log(d,"SKIPPED_LANE_RACE|%s|%s|%s|selected=%s|reason=%s"%
            (HOST,sess,cid,_LANE["name"],affinity_reason))
        continue
    model=_logical_route_for(fresh_claimability["core"])
    if model is None:
        lane_drift += 1
        log(d,"SKIPPED_LOGICAL_ROUTE_RACE|%s|%s|reason=missing-or-ambiguous-size"%
            (HOST,cid))
        continue
    _route_identity={
        "logical_route":model,
        "provider":"skgateway",
        "capacity_domains":[],
        "model_or_bucket":model,
    }
    _review_seat=governed_review_seat(
        fresh_claimability["labels"],
        qualified_reviewer_seats(fresh_claimability["core"]),
    )
    if _review_seat is not None:
        _metadata=_governed_review_metadata(
            fresh_claimability["core"],fresh_claimability["labels"])
        _size_match=_GLM_SIZE_RE.search(
            str(fresh_claimability["core"].get("title") or ""))
        _routes=([] if _metadata is None or _size_match is None
                 else eligible_review_routes(
                     _review_route_snapshot or {},_size_match.group(1),
                     fresh_claimability["labels"],_metadata[0],name,
                     _review_route_occupancy,declared_seat=_review_seat,
                     qualified_seats=qualified_reviewer_seats(
                         fresh_claimability["core"])))
        _selected_route=choose_review_route(_routes,_review_route_reservations)
        if _selected_route is None:
            lane_drift += 1
            log(d,"SKIPPED_REVIEW_ROUTE|%s|%s|reason=no-eligible-route"%(HOST,cid))
            continue
        _route_identity={
            "logical_route":model,
            "provider":"skgateway",
            "capacity_domains":[str(_selected_route["capacity_domain"])],
            "model_or_bucket":model,
        }
        admitted,health_reason=True,"healthy"
    else:
        admitted,health_reason=_health_for(_LANE["name"],model)
        _producer_routes=_producer_routes_for(
            fresh_claimability["core"],fresh_claimability["labels"],
            _LANE["name"] if "%s-only"%_LANE["name"] in {
                str(label).strip().lower() for label in fresh_claimability["labels"]}
            else None)
        _selected_route=choose_review_route(_producer_routes,_review_route_reservations)
        if _selected_route is None:
            lane_drift += 1
            log(d,"SKIPPED_PRODUCER_ROUTE|%s|%s|reason=no-eligible-route"%(HOST,cid))
            continue
        _route_identity={
            "logical_route":model,
            "provider":"skgateway",
            "capacity_domains":[str(_selected_route["capacity_domain"])],
            "model_or_bucket":model,
        }
        admitted,health_reason=True,"healthy"
    if not admitted:
        lane_drift += 1
        _log_once_per_hour(
            d,"lane_admission",cid,
            "SKIPPED_LANE_HEALTH|%s|%s|%s|lane=%s|model=%s|reason=%s"%
            (HOST,sess,cid,_LANE["name"],model,health_reason))
        continue
    # Link's recommendation appends evidence. Compare the bounded admission
    # after lane health so no event mutates the card before the final preclaim.
    try:
        name, _review_recommendation, _review_handoff = _pool_v2_preclaim_handoff(
            cid, _selected_admission, _fresh_admission, name
        )
    except BoundaryError as exc:
        if str(exc) == "POOL_V2 admission changed before claim":
            raced += 1
            _raced_ids.append(cid)
            log(d,"SKIPPED_ADMISSION_DRIFT|%s|%s|%s|reason=%s"%
                (HOST,sess,cid,fresh_claimability.get("reason","unknown")))
            continue
        log(d, "REVIEW_ASSIGNMENT_BLOCKED|%s|%s|%s" % (HOST, cid, exc))
        continue
    try:
        _fanout_request=pending_fanout_request(Path(HOME) / ".skcapstone",cid)
    except FanoutBoundaryError as exc:
        log(d,"FANOUT_BLOCKED|%s|%s|%s"%(HOST,cid,exc))
        continue
    _fanout_env=("", "", "")
    if _fanout_request is not None:
        _fanout_env=(
            _fanout_request.request_id,
            _fanout_request.requester,
            _fanout_request.route,
        )
        with open(bf,"a",encoding="utf-8") as _brief_handle:
            _brief_handle.write(
                "\nNIOBE ROLE-BOUNDED FAN-OUT:\n"
                "- request_id=%s\n- requester=%s\n- allowed_route=%s\n"
                "- Your authority is limited to this route and the card criteria.\n"
                % _fanout_env)
    try:
        _route_preflight=resolve_and_preflight(_GATEWAY_ENDPOINT,model)
    except ValueError as exc:
        log(d,"ROUTE_PREFLIGHT_BLOCKED|%s|%s|requested=%s|reason=%s"%
            (HOST,cid,model,str(exc)[:140]))
        continue
    log(d,"ROUTE_PREFLIGHT_OK|%s|%s|requested=%s|served=%s|provider=%s"%
        (HOST,cid,_route_preflight.requested_identity,
         _route_preflight.served_identity,_route_preflight.provider or "unknown"))
    default_workspace=os.path.join(HOME,".skcapstone/fleet/workspaces",name)
    try:
        _source_spec = _source_workspace_spec(
            fresh_claimability["core"], fresh_claimability["labels"]
        )
        if _source_spec is not None:
            _preclaim_source_ref(*_source_spec)
        workspace=_materialize_worker_workspace(
            default_workspace,
            fresh_claimability["core"],
            fresh_claimability["labels"],
        )
    except ValueError as exc:
        log(d,"WORKSPACE_BLOCKED|%s|%s|%s"%(HOST,cid,exc))
        continue
    if _fanout_request is not None:
        try:
            append_fanout_receipt(
                Path(HOME) / ".skcapstone",_fanout_request,state="materialized",
                process={"host":HOST,"workspace":workspace})
        except (FanoutBoundaryError, OSError, ValueError) as exc:
            log(d,"FANOUT_MATERIALIZE_RECEIPT_FAILED|%s|%s|%s"%(HOST,cid,exc))
            continue
    claim=subprocess.run([SKC,"coord","claim",cid,"--agent",name],capture_output=True,text=True)
    claimed_owner,_claimed_at,claimed_revision=_current_claim_identity_fresh(cid)
    claim_outcome=_classify_claim_outcome(
        True,
        claim.returncode if claimed_revision else 1,
        claimed_owner,
        name)
    if claim_outcome == "claim_refused":
        claim_refused += 1
        detail=(claim.stderr or claim.stdout or
                "claim not visible with an explicit revision in CardStore fold").strip()[:140]
        log(d,"CLAIM_REFUSED|%s|%s|%s|owner=%s|%s"%(HOST,sess,cid,claimed_owner,detail))
        continue
    # Atomic exact-card admission: a live holder for this card (another
    # authoritative owner, whatever lane or launcher generation created it)
    # refuses this launch BEFORE any worker process is created. The receipt
    # names the conflict deterministically and carries no secrets.
    _admission=acquire_card_admission(
        HOME,cid,owner=name,claim_revision=claimed_revision,
        workspace=workspace,unit=unit,session=sess)
    if not _admission["admitted"]:
        admission_refused += 1
        _holder=_admission.get("receipt") or {}
        log(d,"ADMISSION_REFUSED|%s|%s|%s|reason=%s|holder_owner=%s|"
            "holder_claim_revision=%s|holder_workspace=%s|holder_pid=%s|"
            "holder_unit=%s"%(
                HOST,sess,cid,_admission["reason"],
                _holder.get("owner"),_holder.get("claim_revision"),
                _holder.get("workspace"),_holder.get("pid"),
                _holder.get("unit")))
        # Release only when our fresh generation is the duplicate; a live
        # holder of the SAME generation is our own worker and is untouched.
        _same_generation=(
            _holder.get("owner") == name and
            _holder.get("claim_revision") == claimed_revision)
        if not _same_generation:
            subprocess.run(
                [SKC,"coord","release-claim",cid,"--owner",name,
                 "--expected-claim-revision",claimed_revision,"--agent","niobe"],
                capture_output=True,text=True)
        continue
    # Recheck under the lock: the claim won before admission, but the
    # authoritative fold can change underneath us (a concurrent owner's
    # earlier claim syncs in and the CardStore fold resolves to them, the
    # 0d7331e7 shape). This is the last point before process creation where
    # the displacement can still be refused without killing a worker.
    _final_owner,_final_at,_final_revision=_current_claim_identity_fresh(cid)
    if _final_owner != name or _final_revision != claimed_revision:
        admission_refused += 1
        _release_card_admission(HOME,cid)
        log(d,"ADMISSION_REFUSED|%s|%s|%s|reason=claim-displaced|"
            "fold_owner=%s|fold_claim_revision=%s|expected_owner=%s|"
            "expected_claim_revision=%s"%(
                HOST,sess,cid,_final_owner,_final_revision,
                name,claimed_revision))
        subprocess.run(
            [SKC,"coord","release-claim",cid,"--owner",name,
             "--expected-claim-revision",claimed_revision,"--agent","niobe"],
            capture_output=True,text=True)
        continue
    if _fanout_request is not None:
        try:
            append_fanout_receipt(
                Path(HOME) / ".skcapstone",_fanout_request,state="claimed",
                claim_owner=name,claim_revision=claimed_revision,
                process={"host":HOST,"session":sess,"workspace":workspace})
        except (FanoutBoundaryError, OSError, ValueError) as exc:
            subprocess.run(
                [SKC,"coord","release-claim",cid,"--owner",name,
                 "--expected-claim-revision",claimed_revision,"--agent","niobe"],
                capture_output=True,text=True)
            log(d,"FANOUT_CLAIM_RECEIPT_FAILED|%s|%s|%s"%(HOST,cid,exc))
            continue
    # The wrapper owns exact-generation release and snapshot retirement under
    # one CardStore fence. The child must never release independently.
    _bi = _beat_interval()
    _bf_path = "~/.skcapstone/fleet/beats/" + name + ".json"
    child=(
        "beat() { while :; do "
        "trap 'trap - HUP INT TERM; "
        "for sleeper in $(jobs -pr); do kill \"$sleeper\" 2>/dev/null || true; done; "
        "wait; exit 0' HUP INT TERM; "
        "mkdir -p ~/.skcapstone/fleet/beats; "
        "echo '{\"owner\":\"%s\",\"card_id\":\"%s\",\"claim_revision\":\"%s\","
        "\"session_id\":\"%s\","
        "\"emitter\":\"wrapper\",\"disposition\":\"RUNNING\","
        "\"beat_at\":'$(date +%%s)',\"elapsed_s\":'$SECONDS'}' "
        "> %s.tmp 2>/dev/null && mv %s.tmp %s 2>/dev/null || true; "
        "sleep %s & wait $!; done; }; "
        "beat & BEAT=$!; "
        "stop_beat() { kill $BEAT 2>/dev/null || true; wait $BEAT 2>/dev/null || true; }; "
        'trap "stop_beat; exit 143" HUP INT TERM; '
        'trap "stop_beat" EXIT; '
        "env SKAGENT=%s SKCAPSTONE_AGENT=%s SKFLEET_WORKSPACE=%s "
        "SKFLEET_CARD_ID=%s SKFLEET_CLAIM_REVISION=%s SKFLEET_SESSION_ID=%s "
        "SKFLEET_FANOUT_REQUEST_ID=%s SKFLEET_FANOUT_REQUESTER=%s "
        "SKFLEET_FANOUT_ROUTE=%s "
        "%s --approve --extension %s --name %s "
        "--provider skgateway --model %s --thinking off --no-context-files --no-skills --tools %s "
        '-p "$(cat %s)"; '
        "rc=$?; trap - EXIT HUP INT TERM; stop_beat; exit $rc"
        % (name, cid, claimed_revision, sess,
           _bf_path, _bf_path, _bf_path,
           _bi,
           name, name, shlex.quote(workspace), cid, shlex.quote(claimed_revision),
           shlex.quote(sess), *(shlex.quote(value) for value in
                                globals().get("_fanout_env", ("", "", ""))),
           shlex.quote(PI),
           shlex.quote(globals().get("PI_CARDSTORE_GUARD", "pi-cardstore-guard.mjs")),
           name, model,
           pi_tools, bf))
    wrapper=os.path.join(os.path.dirname(__file__),"skfleet-worker-wrapper.py")
    inner=[
        sys.executable,wrapper,"--card",cid,"--owner",name,
        "--claim-revision",claimed_revision,"--host",HOST,"--lane",_LANE["name"],
        "--model",model,"--logical-route",_route_identity["logical_route"],
        "--provider",_route_identity["provider"],
        *[item for domain in _route_identity["capacity_domains"]
          for item in ("--capacity-domain",domain)],
        "--stdout",lf,"--evidence-dir",_WORKER_EXIT_DIR,
        "--mail-recipient",WORKER_MAIL_RECIPIENTS[0],
        "--live-snapshot",os.path.join(LIVE, HOST + ".json"),
        "--session",sess,"--worker-executable",PI,
        "--","bash","-lc",child,
    ]
    r=subprocess.run(_worker_launch_command(unit,workspace,inner),capture_output=True,text=True)
    ok = r.returncode==0
    launch_identity=(
        _launch_claim_fields(name,claimed_revision,ok)
        if ok else "|owner=%s|claim_revision=%s" % (name, claimed_revision)
    )
    launch_action="LAUNCHED" if ok else "LAUNCH_FAILED"
    log(d,"%s|%s|%s|%s|lane=%s|model=%s%s"%
        (launch_action,HOST,sess,cid,_LANE["name"],model,launch_identity))
    launch_receipts+=1
    if _fanout_request is not None:
        try:
            append_fanout_receipt(
                Path(HOME) / ".skcapstone",_fanout_request,
                state="launched" if ok else "launch_failed",
                claim_owner=name,claim_revision=claimed_revision,
                process={
                    "request_id":_fanout_request.request_id,
                    "card_id":cid,
                    "owner":name,
                    "claim_revision":claimed_revision,
                    "host":HOST,
                    "session_id":sess,
                    "unit":unit,
                    "alive":ok,
                })
        except (FanoutBoundaryError, OSError, ValueError) as exc:
            log(d,"FANOUT_LAUNCH_RECEIPT_FAILED|%s|%s|%s"%(HOST,cid,exc))
    if _review_recommendation is not None:
        _observation_evidence = hashlib.sha256(
            (launch_action + "\0" + cid + "\0" + name + "\0" + claimed_revision).encode()
        ).hexdigest()
        try:
            append_review_launch_receipt(
                Path(HOME) / ".skcapstone",
                _review_handoff,
                actor=name,
                claim_revision=claimed_revision,
                launched=ok,
                route_identity=_route_identity,
            )
            MeroObservation(
                card_id=cid,
                observation_id=(
                    "mero-" + _review_recommendation.recommendation_id + "-" + claimed_revision
                ),
                state="launched" if ok else "launch_failed",
                process={"host":HOST,"session":sess,"alive":ok,
                         "route_identity":_route_identity},
                evidence_sha256=_observation_evidence,
            ).append(Path(HOME) / ".skcapstone")
            log(
                d,
                "MERO_OBSERVED|%s|%s|state=%s"
                % (HOST, cid, "launched" if ok else "launch_failed"),
            )
        except (BoundaryError, OSError, ValueError) as exc:
            log(d, "MERO_OBSERVATION_FAILED|%s|%s|%s" % (HOST, cid, exc))
    if not ok:
        subprocess.run([SKC,"coord","release-claim",cid,"--owner",name,
                        "--expected-claim-revision",claimed_revision,"--agent",name],
                       capture_output=True,text=True)
    else:
        launched+=1
        launch_remaining[_LANE["name"]]-=1
        if _route_identity.get("capacity_domains"):
            _domain=_route_identity["capacity_domains"][0]
            _review_route_reservations[_domain]=(
                _review_route_reservations.get(_domain,0)+1)
    time.sleep(2)

# A selected Seraph candidate can be suppressed before claim, leaving no worker
# attempt to emit the normal launch receipt. Preserve the diagnostics above, but
# close that producer contract with one bounded terminal receipt for the cycle.
_terminal_noop = _seraph_terminal_noop(
    HOST, _ONLY_SEAT, DRY, len(picks), processed_picks, launch_receipts
)
if _terminal_noop:
    log(d, _terminal_noop)

# Republish after launching, because the first publish is a snapshot of the
# workers that existed when this tick STARTED. Publishing only there means a host
# never reports the workers it just launched until its next tick, five minutes
# later, so for those five minutes those cards are invisible to every other host.
#
# The reaper reaps on "no host reports this card running". A card nobody reports is
# a card that looks dead, and CLAIM_GRACE alone was carrying the whole burden of
# not acting on that. Observed 2026-08-28: every host published cards=0 while
# chiap01 ran 3 workers and chiap03 ran 2, purely because each had launched them
# after its own publish.
#
# Re-reading both migration-era tmux and managed units closes the window.
try:
    publish_live(
        sh("tmux","ls","-F","#{session_name}").split(), active_worker_units()
    )
except Exception as _exc:
    log(d,"WARN|%s|could not republish liveness after launching: %s"%(HOST,_exc))


_observe_assigned_reviews()

if raced:
    _raced_ids_value, _raced_omitted = _bounded_ids(_raced_ids)
    log(d,"RACED|%s|count=%d ids=%s omitted=%d selection race between pool build and launch"%
        (HOST,raced,_raced_ids_value,_raced_omitted))
if lane_drift:
    log(d,"LANE_RACED|%s|%d card(s) changed lane compatibility before claim"%
        (HOST,lane_drift))
if claim_refused:
    log(d,"CLAIM_REFUSED_TOTAL|%s|%d claim command(s) refused or not visible "
        "in the authoritative fold"%(HOST,claim_refused))
if admission_refused:
    log(d,"ADMISSION_REFUSED_TOTAL|%s|%d launch(es) refused before process "
        "creation: a live authoritative owner already holds the exact card"%(
        HOST,admission_refused))
log(d,"CYCLE_RECEIPT|%s|seat=%s|launched=%d|attempted=%d|receipts=%d"%
    (HOST,_ONLY_SEAT or "niobe",launched,processed_picks,launch_receipts))
