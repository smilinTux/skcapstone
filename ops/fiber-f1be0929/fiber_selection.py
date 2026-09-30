"""Bridge an operator-pinned card scope through Chef's existing promoter.

No discovery, claim transfer, launch, or outcome authority lives here. The
existing dispatcher holds its authority lock and rechecks every launch gate.
"""
import importlib.util
import math
from pathlib import Path
import re
import time
from types import SimpleNamespace

from fiber_admission import Refused, card_fingerprint
from fiber_refill import (MAX_INPUTS, MAX_PENDING, MAX_TTL, SCHEMA, _directory,
                          _paths, _publish, _qualified, _read, _runs, digest)


def load_promoter():
    """Load the unchanged, reviewed helper beside the installed controller."""
    path = Path(__file__).with_name('skfleet-backlog-promoter.py')
    spec = importlib.util.spec_from_file_location('fiber_existing_promoter', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _policy(path, now):
    """Require a finite, short-lived, operator-owned scope of exact contracts."""
    _directory(path.parent)
    value = _read(path)
    fields = {'schema', 'cards', 'repository', 'qualified_at', 'expires_at', 'evidence_sha256'}
    if set(value) not in (fields, fields | {'test_environment'}):
        raise Refused('selection-invalid-policy')
    if value['schema'] != 'skfleet.selection/v1':
        raise Refused('selection-invalid-schema')
    start, end = value['qualified_at'], value['expires_at']
    if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (now, start, end))
            or not start <= now < end or not 0 < end - start <= MAX_TTL):
        raise Refused('selection-stale-policy')
    entries = value['cards']
    if not isinstance(entries, dict) or not 0 < len(entries) <= MAX_INPUTS:
        raise Refused('selection-invalid-scope')
    if any(not re.fullmatch('[0-9a-f]{8}', cid) or not isinstance(pin, str)
           or not re.fullmatch('[0-9a-f]{64}', pin) for cid, pin in entries.items()):
        raise Refused('selection-invalid-pin')
    if (not isinstance(value['evidence_sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', value['evidence_sha256'])
            or not isinstance(value['repository'], str)):
        raise Refused('selection-invalid-evidence')
    return value


def select_requests(state, qualified, policy_path, hold, *, read_cards, describe,
                    eligible, verify, now, promoter=None, dry_run=False, clock=time.time):
    """Qualify at most two pinned, untouched breadth cards using existing gates.

    Scope is explicit rather than an unbounded board scan. A card with prior
    output needs the existing audited handoff/review path, never a new worker.
    Receipts are no-clobber and never renewed automatically after expiration.
    """
    state, qualified, policy_path, hold = map(Path, (state, qualified, policy_path, hold))
    result = {'state': 'selection-checked', 'added': [], 'skipped': []}
    if hold.exists() or hold.is_symlink():
        return dict(result, state='admission-held')
    if not policy_path.exists() and not policy_path.is_symlink():
        return dict(result, state='disabled')
    policy = _policy(policy_path, now)
    _directory(qualified)
    existing = _paths(qualified)
    runs = _runs(state)
    pending = 0
    for path in existing:
        if path.stem in runs:
            continue
        try:
            _qualified(path, now)
        except Refused as exc:
            if str(exc) != 'refill-stale-qualification':
                raise
            result['skipped'].append({'card': path.stem, 'reason': str(exc)})
            continue  # Preserve expired evidence without consuming live depth.
        pending += 1
    if pending >= MAX_PENDING:
        return result
    promoter = promoter or load_promoter()
    for cid, pin in policy['cards'].items():
        if pending + len(result['added']) >= MAX_PENDING:
            break
        destination = qualified / (cid + '.json')
        if cid in runs or destination.exists() or destination.is_symlink():
            continue
        try:
            board = read_cards([cid])
            row = board[cid]
            if row.get('id') != cid or card_fingerprint(row) != pin:
                raise Refused('selection-contract-changed')
            if row.get('status') not in {'backlog', 'ready'}:
                raise Refused('selection-prior-work')
            raw_labels = row.get('labels') or []
            if not isinstance(raw_labels, list) or any(not isinstance(label, str) for label in raw_labels):
                raise Refused('selection-invalid-labels')
            labels = set(raw_labels)
            if not {'source-only', 'sklegal'} <= labels or any(label.startswith('seat-') for label in labels):
                raise Refused('selection-outside-scope')
            if 'codex-only' in labels or re.findall(r'\[(S|M|L|XL)\]', row.get('title', '')) not in (['S'], ['M']):
                raise Refused('selection-not-breadth')
            if set(row.get('links') or {}) - {'repository', 'base_ref', 'base_revision', 'assigned_tdd', 'tdd_custody'}:
                raise Refused('selection-preserved-output')
            for dependency in row.get('dependencies') or []:
                target = board.get(dependency, {})
                if target.get('status') != 'done' or (target.get('links') or {}).get('verdict') != 'PASS':
                    raise Refused('selection-dependency-unverified')
            folded = {key: SimpleNamespace(**value) for key, value in board.items()}
            reason = promoter.refusal(Path.home() / '.skcapstone', folded[cid], folded, set())
            if reason:
                raise Refused('selection-promoter:' + reason)
            if not eligible(row, board):
                raise Refused('selection-native-gate')
            family = 'zai' if 'glm-only' in labels else 'deepseek'
            job = describe(cid, 'implementation', family)
            if (job.get('card') != cid or job.get('stage') != 'implementation'
                    or job.get('provider') != family or job.get('host') != 'auto'
                    or job['repository'] != policy['repository'] or job['card_fingerprint'] != pin):
                raise Refused('selection-source-changed')
            if 'test_environment' in policy:
                job['test_environment'] = policy['test_environment']
            verify(row, job)
            stamp = clock()
            if type(stamp) not in (int, float) or not math.isfinite(stamp) or not now <= stamp < policy['expires_at']:
                raise Refused('selection-expired-during-check')
            proof = {'schema': SCHEMA, 'job': job, 'job_sha256': digest(job),
                     'card_links_sha256': digest(row.get('links') or {}),
                     'qualified_at': stamp, 'expires_at': policy['expires_at'],
                     'evidence_sha256': policy['evidence_sha256']}
            if not dry_run:
                _publish(destination, proof)
            result['added'].append(cid)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result['skipped'].append({'card': cid, 'reason': str(exc) if isinstance(exc, Refused) else type(exc).__name__})
    return result
