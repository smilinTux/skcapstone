"""Central capacity reservations for the fiber dispatcher, not task authority.

Only chiap08 may own the live database. Do not put it in Syncthing or call this
independently on worker hosts. Observations must come from live collectors.
Claim, policy, exact source, and review gates still apply after reservation.
No reservation expires merely because time elapsed or a probe timed out.
"""
from contextlib import closing
import math
import json
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import uuid

ESTATE_CAP = 12
SPACING = 75
PLACEMENT = {'implementation': ('chiap02', 2), 'review': ('chiap03', 1),
             'tests': ('chiap04', 1)}
HOST_SLOTS = {'chiap02': 3, 'chiap03': 2, 'chiap04': 2,
              'chiwk12': 2, 'chiwk13': 1, 'ziowk01': 2}
STAGE_HOSTS = {'implementation': ('chiap02', 'chiwk12'),
               'review': ('chiap03', 'chiwk12'),
               'tests': ('chiap04', 'chiap02', 'chiwk13')}
PROVIDERS = {'deepseek', 'codex', 'zai'}


class Refused(ValueError):
    pass


def card_fingerprint(row):
    """Bind task semantics, excluding claim state and appended evidence links."""
    material = {key: row.get(key) for key in ['id', 'title', 'description', 'acceptance_criteria']}
    material['labels'] = sorted(row.get('labels') or [])
    material['dependencies'] = sorted(row.get('dependencies') or [])
    material['meta'] = {key: value for key, value in (row.get('meta') or {}).items() if not key.startswith('_claim')}
    material['links'] = {key: (row.get('links') or {}).get(key) for key in ['repository', 'base_ref', 'base_revision', 'assigned_tdd']}
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _positive_int(value):
    return type(value) is int and value > 0


def observed_workers(snapshot, now):
    if not isinstance(now, (int, float)) or not math.isfinite(now):
        raise Refused('invalid-clock')
    observed = snapshot.get('observed_at')
    if not isinstance(observed, (int, float)) or not math.isfinite(observed) or not 0 <= now - observed <= 30:
        raise Refused('stale-observation')
    if snapshot.get('complete') is not True:
        raise Refused('incomplete-observation')
    workers = snapshot.get('workers')
    if not isinstance(workers, list):
        raise Refused('incomplete-observation')
    current = {}
    for worker in workers:
        if not isinstance(worker, dict) or not isinstance(worker.get('key'), str) or not worker['key'] or not worker.get('host'):
            raise Refused('ambiguous-worker')
        key = worker['key']
        identity = (worker['host'], worker.get('provider'))
        if key in current and current[key] != identity:
            raise Refused('ambiguous-worker')
        current[key] = identity
    return current


def placement_allowed(job, host):
    if job.get('stage') not in STAGE_HOSTS:
        return False
    if host == 'ziowk01':
        return job.get('host') == 'ziowk01'  # Explicit WAN pin only.
    return host in STAGE_HOSTS[job['stage']] and (host != 'chiwk13' or job.get('work_class') == 'S')


def select_host(database, job, snapshot, *, now):
    """Choose a ready host under the authority lock, counting pending launches.

    No provider qualification is inferred here. Normal reserve() still checks
    the exact qualified route and a fresh observation before any launch.
    """
    workers = observed_workers(snapshot, now)
    if Path(database).exists():
        with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reservations'").fetchone():
                for card, host, provider in db.execute('SELECT card, host, provider FROM reservations'):
                    key = 'card:' + card
                    if key in workers and workers[key] != (host, provider):
                        raise Refused('ambiguous-worker')
                    workers[key] = (host, provider)
    if len(workers) >= ESTATE_CAP:
        raise Refused('estate-capacity')
    candidates = STAGE_HOSTS.get(job.get('stage'), ()) if job.get('host') == 'auto' else (job.get('host'),)
    ranked = []
    for order, host in enumerate(candidates):
        node = snapshot.get('hosts', {}).get(host, {})
        if not placement_allowed(job, host) or node.get('ready') is not True or not _positive_int(node.get('slots')):
            continue
        slots = min(HOST_SLOTS[host], node['slots'])
        occupied = sum(place == host for place, _ in workers.values())
        if occupied < slots:
            ranked.append((occupied / slots, order, host))
    if not ranked:
        raise Refused('no-eligible-host')
    return min(ranked)[2]


def _validate(job, snapshot, now):
    current = observed_workers(snapshot, now)
    if not re.fullmatch(r'[0-9a-f]{8}', str(job.get('card', ''))):
        raise Refused('invalid-card')
    if job.get('claimable') is not True:
        raise Refused('card-not-claimable')
    if not placement_allowed(job, job.get('host')):
        raise Refused('stage-placement')
    provider = job.get('provider')
    if provider not in PROVIDERS:
        raise Refused('provider-not-qualified')
    if job['stage'] == 'review' and (not job.get('producer_provider') or job['producer_provider'] == provider):
        raise Refused('review-independence')
    host = snapshot.get('hosts', {}).get(job['host'], {})
    if host.get('ready') is not True or not _positive_int(host.get('slots')):
        raise Refused('host-not-ready')
    route = snapshot.get('providers', {}).get(provider, {})
    if route.get('ready') is not True or not _positive_int(route.get('cap')):
        raise Refused('provider-not-qualified')
    return current, min(host['slots'], HOST_SLOTS[job['host']]), route['cap']


def reserve(database, job, snapshot, *, now):
    """Atomically reserve one card; refuse ambiguity and count legacy workers.

    This enforces admission spacing. The launcher must also enforce SPACING
    from its last acknowledged remote start while holding its authority lock.
    An unacknowledged launch must remain reserved, never be retried blindly.
    """
    workers, host_cap, provider_cap = _validate(job, snapshot, now)
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    # ponytail: one SQLite writer on chiap08; not a distributed lock service.
    with closing(sqlite3.connect(database, timeout=10, isolation_level=None)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute('CREATE TABLE IF NOT EXISTS reservations (card TEXT PRIMARY KEY, host TEXT NOT NULL, provider TEXT NOT NULL, token TEXT UNIQUE NOT NULL, reserved_at REAL NOT NULL)')
            reservations = db.execute('SELECT card, host, provider, reserved_at FROM reservations').fetchall()
            if any(row[0] == job['card'] for row in reservations) or 'card:' + job['card'] in workers:
                raise Refused('duplicate-card')
            for card, host, provider, stamp in reservations:
                key = 'card:' + card
                if key in workers and workers[key] != (host, provider):
                    raise Refused('ambiguous-worker')
                workers[key] = (host, provider)
            if len(workers) >= ESTATE_CAP:
                raise Refused('estate-capacity')
            if sum(host == job['host'] for host, _ in workers.values()) >= host_cap:
                raise Refused('host-capacity')
            # Unattributed providers consume every provider budget conservatively.
            if sum(provider == job['provider'] or provider not in PROVIDERS for _, provider in workers.values()) >= provider_cap:
                raise Refused('provider-capacity')
            if reservations and now - max(row[3] for row in reservations) < SPACING:
                raise Refused('launch-spacing')
            token = uuid.uuid4().hex
            db.execute('INSERT INTO reservations VALUES (?, ?, ?, ?, ?)',
                       (job['card'], job['host'], job['provider'], token, now))
            db.execute('COMMIT')
            return {'card': job['card'], 'host': job['host'], 'provider': job['provider'], 'token': token, 'reserved_at': now}
        except Exception:
            db.execute('ROLLBACK')
            raise


def prepare_worktree(repository, workspace, branch, revision):
    """Create only a new exact-revision worktree and a new branch.

    Call only after the card claim and source binding have been verified. No
    cleanup on failure: preserve any partial artifact for operator inspection.
    Git serializes branch creation; the dispatcher serializes workspace paths.
    """
    repository, workspace = Path(repository), Path(workspace)
    if os.path.lexists(workspace):
        raise Refused('occupied-workspace')
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise Refused('source-revision-not-exact')
    valid = subprocess.run(['git', 'check-ref-format', '--branch', branch], capture_output=True)
    if valid.returncode:
        raise Refused('invalid-branch')
    exists = subprocess.run(['git', '-C', str(repository), 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch])
    if exists.returncode == 0:
        raise Refused('existing-branch')
    if exists.returncode != 1:
        raise Refused('branch-check-failed')
    result = subprocess.run(['git', '-C', str(repository), 'worktree', 'add', '--quiet', '-b', branch, '--', str(workspace), revision], capture_output=True, text=True)
    if result.returncode:
        raise Refused('worktree-creation-failed')


def claim_for_worker(card, owner, *, runner=subprocess.run):
    """Use the existing gated CLI, then verify exact ownership via its read API.

    Failure never releases another claim, retries with force, or authorizes
    workspace creation. The caller must preserve its reservation on ambiguity.
    """
    if not re.fullmatch(r'[0-9a-f]{8}', card) or not re.fullmatch(r'[a-z][a-z0-9-]{0,95}', owner):
        raise Refused('invalid-claim-identity')
    claimed = runner(['skcapstone', 'coord', 'claim', card, '--agent', owner],
                     capture_output=True, text=True, timeout=60)
    if claimed.returncode != 0:
        raise Refused('claim-refused')
    return read_claim(card, owner, runner=runner)


def read_claim(card, owner, *, revision=None, runner=subprocess.run):
    """Read-only exact-generation check, shared by authority and remote workers."""
    if not re.fullmatch(r'[0-9a-f]{8}', card) or not re.fullmatch(r'[a-z][a-z0-9-]{0,95}', owner):
        raise Refused('invalid-claim-identity')
    if revision is not None and not re.fullmatch(r'[0-9a-f]{32}', str(revision)):
        raise Refused('invalid-claim-revision')
    result = runner(['skcapstone', 'coord', 'show', card, '--json'],
                    capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise Refused('claim-readback-failed')
    try:
        document = json.loads(result.stdout)
    except ValueError as exc:
        raise Refused('claim-readback-invalid') from exc
    matches = []
    def visit(value):
        if isinstance(value, dict):
            if value.get('id') == card and 'owner' in value:
                matches.append(value)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)
    visit(document)
    if len(matches) != 1:
        raise Refused('claim-readback-ambiguous')
    row = matches[0]
    actual_revision = (row.get('meta') or {}).get('_claim_revision', '')
    if row.get('owner') != owner or not re.fullmatch(r'[0-9a-f]{32}', str(actual_revision)) or (revision is not None and actual_revision != revision):
        raise Refused('claim-owner-or-revision-mismatch')
    if row.get('status') not in {'ready', 'doing'} or row.get('archived') is True:
        raise Refused('claim-not-active')
    return row
