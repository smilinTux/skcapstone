"""Refill explicit requests, never discover work or grant execution authority.

Caller holds fiber_dispatch.authority across this entire operation. Receipts
are operator-qualified inputs, protected by filesystem ownership and the worker
sandbox, not self-certifying approvals. All launch gates remain downstream.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

from fiber_admission import Refused, card_fingerprint

MAX_INPUTS = 32
MAX_PENDING = 2
MAX_BYTES = 65536
MAX_TTL = 86400
SCHEMA = 'skfleet.qualified-request/v1'
CURSOR_SCHEMA = 'skfleet.refill-cursor/v1'
CURSOR_NAME = 'refill-cursor.json'


def digest(value):
    """Hash the canonical JSON value used in a qualification receipt."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _directory(path):
    """Reject redirected or writable-by-others qualification directories."""
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise Refused('refill-unsafe-directory')


def _read(path):
    """Read bounded regular operator-owned JSON without following symlinks."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise Refused('refill-unsafe-file')
        data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise Refused('refill-input-too-large')
    value = json.loads(data)
    if not isinstance(value, dict):
        raise Refused('refill-invalid-object')
    return value


def _names(directory):
    """Enumerate flat ``*.json`` names without decoding or stat-ing contents.

    Directory-entry count is metadata, never a worker-capacity decision. A
    large retained inbox must not refuse useful refill; only the number of
    decoded files and native reads per pass is bounded, by ``_page``. Every
    ``*.json`` dirent is listed, including symlinks and non-regular entries;
    the bounded no-follow reader refuses those when their page is examined.
    """
    return sorted(entry.name for entry in os.scandir(directory)
                  if entry.name.endswith('.json'))


def _cursor_path(state):
    return Path(state) / CURSOR_NAME


def _read_cursor(state):
    """Read a bounded, operator-owned cursor map; a missing cursor starts scanning.

    Unsafe cursor inputs (symlink, non-regular, foreign-owned, writable,
    oversized or malformed) are refused rather than silently ignored, because
    a cursor is a scheduling hint that must never be attacker-controlled.
    """
    path = _cursor_path(state)
    if not path.exists() and not path.is_symlink():
        return {}
    try:
        value = _read(path)
    except (OSError, ValueError):
        raise Refused('refill-invalid-cursor') from None
    if set(value) - {'schema', 'scan'} or value.get('schema') not in (None, CURSOR_SCHEMA):
        raise Refused('refill-invalid-cursor')
    scan = value.get('scan') or {}
    if not isinstance(scan, dict) or set(scan) - {'queue', 'qualified'}:
        raise Refused('refill-invalid-cursor')
    for key, name in scan.items():
        if not isinstance(name, str) or not re.fullmatch(r'[0-9a-f]{8}\.json', name):
            raise Refused('refill-invalid-cursor')
    return {key: name for key, name in scan.items() if name}


def _write_cursor(state, scan):
    """Atomically publish advanced cursor hints in the private state directory.

    The cursor is a scheduling hint, never authorization, so an atomic
    same-directory replace is correct and cannot replay work on restart.
    """
    path = _cursor_path(state)
    current = {}
    if path.exists() and not path.is_symlink():
        current = _read_cursor(state)
    merged = dict(current, **scan)
    fd, temporary = tempfile.mkstemp(prefix='.refill-cursor-', dir=path.parent)
    scratch = Path(temporary)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'schema': CURSOR_SCHEMA, 'scan': merged}, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(scratch, 0o600)
        os.replace(scratch, path)  # Same-directory atomic rename; cursor is a hint.
    finally:
        scratch.unlink(missing_ok=True)
    _sync_directory(path.parent)


def _page(names, cursor, limit):
    """Return at most ``limit`` names after ``cursor``, wrapping once.

    Lexical ordering is the scheduling order. The returned ``next`` cursor is
    the last examined name, so a stable finite inbox eventually reaches later
    eligible work even when early names are expired, malformed or ineligible.
    """
    ordered = sorted(names)
    if not ordered:
        return [], None
    if cursor:
        ordered = [name for name in ordered if name > cursor] + [name for name in ordered if name <= cursor]
    page = ordered[:limit]
    return page, (page[-1] if page else cursor)


def _runs(state):
    """Read launch identities without changing admission state or reservations."""
    database = state / 'admission.sqlite3'
    if not database.exists():
        return {}
    db = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
            return {}
        return dict(db.execute('SELECT card, state FROM runs'))
    finally:
        db.close()


def _publish(path, job):
    """Publish durable complete JSON atomically, refusing an occupied name."""
    fd, name = tempfile.mkstemp(prefix='.refill-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(job, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Atomic no-clobber, including after a crash.
    finally:
        temporary.unlink(missing_ok=True)
    _sync_directory(path.parent)


def _sync_directory(path):
    """Persist directory entries before dropping their preceding durable name."""
    directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _qualified(path, now):
    """Validate immutable job bytes, bounded lifetime and evidence identity."""
    receipt = _read(path)
    fields = {'schema', 'job', 'job_sha256', 'card_links_sha256',
              'qualified_at', 'expires_at', 'evidence_sha256'}
    if set(receipt) != fields or receipt['schema'] != SCHEMA:
        raise Refused('refill-invalid-receipt')
    job = receipt['job']
    if (not isinstance(job, dict) or '_qualification' in job or 'claimable' in job
            or job.get('card') != path.stem or not re.fullmatch('[0-9a-f]{8}', path.stem)):
        raise Refused('refill-card-mismatch')
    for field in ('job_sha256', 'card_links_sha256', 'evidence_sha256'):
        if not isinstance(receipt[field], str) or not re.fullmatch('[0-9a-f]{64}', receipt[field]):
            raise Refused('refill-invalid-digest')
    start, end = receipt['qualified_at'], receipt['expires_at']
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (now, start, end)):
        raise Refused('refill-invalid-clock')
    if not start <= now < end or not 0 < end - start <= MAX_TTL:
        raise Refused('refill-stale-qualification')
    if digest(job) != receipt['job_sha256']:
        raise Refused('refill-job-changed')
    return receipt


def check_qualification(job, qualified, *, read_cards, eligible, verify, clock):
    """Recheck a pinned refill receipt inside admission, including after probes.

    Existing manually queued jobs have no refill marker and retain their old
    contract. Refilled jobs carry the entire receipt in their launch binding;
    deleting or replacing its input cannot silently downgrade the job.
    """
    if '_qualification' not in job:
        return
    qualified = Path(qualified)
    _directory(qualified)
    receipt = _qualified(qualified / (job['card'] + '.json'), clock())
    if receipt != job['_qualification']:
        raise Refused('refill-qualification-changed')
    original = receipt['job']
    actual = {key: value for key, value in job.items() if key not in {'_qualification', 'claimable'}}
    if original.get('host') == 'auto':
        actual['host'] = 'auto'  # The existing selector alone resolves placement.
    if actual != original:
        raise Refused('refill-job-changed')
    board = read_cards([job['card']])
    row = board[job['card']]
    if (card_fingerprint(row) != original.get('card_fingerprint')
            or digest(row.get('links') or {}) != receipt['card_links_sha256']):
        raise Refused('refill-card-changed')
    verify(row, job)
    if not eligible(row, board):
        raise Refused('refill-card-ineligible')
    if clock() >= receipt['expires_at']:
        raise Refused('refill-stale-qualification')


def refill_queue(state, queue, qualified, hold, *, read_cards, eligible, verify, now, dry_run=False):
    """Top up at most two unlaunched queued requests under the authority lock.

    Only queued cards WITHOUT an existing ledger run consume unlaunched
    capacity. RUNNING, LAUNCHING, TERMINAL and other retained run states are
    preserved exactly, never replayed and never counted against the buffer.

    Directory size is metadata, not capacity: a large retained inbox never
    refuses refill. Each pass decodes at most ``MAX_INPUTS`` names from a
    bounded lexical cursor page, skipping names that already have a ledger run
    before reading them. A missing cursor starts scanning; interruption or
    restart cannot authorize duplicate work. Native reads cover only the
    at-most-32 page entries. HOLD precedes all reads and writes and a dry run
    writes neither cursors nor jobs.
    """
    state, queue, qualified, hold = map(Path, (state, queue, qualified, hold))
    result = {'state': 'refill-checked', 'added': [], 'archived': [], 'skipped': []}
    if hold.exists() or hold.is_symlink():
        return dict(result, state='admission-held')
    if not qualified.exists() and not qualified.is_symlink():
        return dict(result, state='disabled')
    _directory(qualified)
    if not dry_run:
        queue.mkdir(parents=True, exist_ok=True, mode=0o700)
    if queue.exists():
        _directory(queue)
    runs = _runs(state)
    cursor = _read_cursor(state)
    queued_names = _names(queue)
    # Unlaunched entries are those without a retained ledger run. Every other
    # retained state (launching/running/terminal/unknown) is preserved as-is
    # and does not consume the unlaunched buffer.
    unlaunched = [name for name in queued_names if name[:-5] not in runs]
    # Bounded queue page: decode at most MAX_INPUTS queue names per pass, and
    # only those that already have a terminal or other retained run. Terminal
    # archival stays recoverable and byte-preserving; other retained entries
    # are simply skipped without being read again.
    queue_page, queue_next = _page(queued_names, cursor.get('queue'), MAX_INPUTS)
    for name in queue_page:
        if runs.get(name[:-5]) != 'terminal':
            continue  # Running/launching/unknown/unlaunched entries stay untouched.
        path = queue / name
        job = _read(path)
        if job.get('card') != name[:-5]:
            raise Refused('refill-queued-identity-mismatch')
        archive = queue / 'terminal'
        if not dry_run:
            archive.mkdir(mode=0o700, exist_ok=True)
            _directory(archive)
            destination = archive / (name[:-5] + '-' + digest(job) + '.json')
            if destination.exists() or destination.is_symlink():
                if _read(destination) != job:
                    raise Refused('refill-archive-conflict')
            else:
                os.link(path, destination, follow_symlinks=False)
            _sync_directory(archive)
            path.unlink()  # Exact queue entry has an identical preserved copy.
            _sync_directory(queue)
        result['archived'].append(name[:-5])
    if queue_next and not dry_run:
        _write_cursor(state, {'queue': queue_next})
    if len(unlaunched) >= MAX_PENDING:
        return result
    qualified_page, qualified_next = _page(
        [name for name in _names(qualified) if name[:-5] not in runs],
        cursor.get('qualified'), MAX_INPUTS)
    if qualified_next and not dry_run:
        _write_cursor(state, {'qualified': qualified_next})
    for name in qualified_page:
        if len(unlaunched) + len(result['added']) >= MAX_PENDING:
            break
        path = qualified / name
        if (queue / name).exists() or (queue / name).is_symlink():
            continue
        try:
            receipt = _qualified(path, now)
            job = receipt['job']
            board = read_cards([job['card']])
            row = board[job['card']]
            if card_fingerprint(row) != job.get('card_fingerprint') or digest(row.get('links') or {}) != receipt['card_links_sha256']:
                raise Refused('refill-card-changed')
            verify(row, job)
            if not eligible(row, board):
                raise Refused('refill-card-ineligible')
            if not dry_run:
                queued = dict(job, _qualification=receipt)
                if len(json.dumps(queued, sort_keys=True).encode()) > MAX_BYTES:
                    raise Refused('refill-queued-request-too-large')
                _publish(queue / name, queued)
            result['added'].append(job['card'])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, Refused) else type(exc).__name__
            result['skipped'].append({'file': name, 'reason': reason})
    return result
