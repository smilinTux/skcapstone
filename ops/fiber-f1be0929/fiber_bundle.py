"""Bounded source-bundle transport for the existing worker Git cache.

Receive only stores opaque hash-addressed bytes. Import happens inside an
already claimed, resource-limited worker service, before model execution.
No model obtains SSH, filesystem privileges or a provider credential.
"""
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from fiber_admission import Refused

MAX_BYTES = 8 * 1024 * 1024


def select(job):
    """Validate the optional selector without allowing caller-supplied paths."""
    if 'source_bundle' not in job:
        return None
    value = job['source_bundle']
    fields = {'sha256', 'base_revision', 'tree', 'ref'}
    if job.get('stage') != 'review' or not isinstance(value, dict) or set(value) != fields:
        raise Refused('bundle-selector-invalid')
    for key, width in [('sha256', 64), ('base_revision', 40), ('tree', 40)]:
        if not isinstance(value[key], str) or not re.fullmatch('[0-9a-f]{' + str(width) + '}', value[key]):
            raise Refused('bundle-selector-invalid')
    if (not isinstance(value['ref'], str)
            or not re.fullmatch(r'refs/heads/work/fiber-[0-9a-f]{8}-[0-9a-f]{8}', value['ref'])
            or not re.fullmatch('[0-9a-f]{40}', str(job.get('base_revision') or ''))
            or value['base_revision'] == job['base_revision']):
        raise Refused('bundle-selector-invalid')
    return value


def verify_binding(row, job):
    """Bind transfer to the original typed review, including historical base."""
    value = select(job)
    if value is None:
        return
    for key, expected in [('base_revision', value['base_revision']),
                          ('candidate_tree', value['tree']), ('candidate_ref', value['ref']),
                          ('link_head_revision', job['base_revision'])]:
        values = [(row.get(container) or {})[key] for container in ('meta', 'links')
                  if key in (row.get(container) or {})]
        if not values or any(item != expected for item in values):
            raise Refused('bundle-review-binding-changed')


def _directory(path):
    """Keep artifact namespaces private and refuse redirected directories."""
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or path.resolve() != path
            or info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise Refused('bundle-unsafe-directory')


def read_blob(path, expected):
    """Read one bounded, owner-controlled blob without following a symlink."""
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise Refused('bundle-unsafe-file')
        data = stream.read(MAX_BYTES + 1)
    if not 0 < len(data) <= MAX_BYTES or hashlib.sha256(data).hexdigest() != expected:
        raise Refused('bundle-digest-mismatch')
    return data


def receive(packet, data_root):
    """Store exactly one blob atomically; retries never overwrite existing data."""
    if (not isinstance(packet, dict) or set(packet) != {'sha256', 'bundle_b64'}
            or not isinstance(packet['sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', packet['sha256'])):
        raise Refused('bundle-packet-invalid')
    try:
        encoded = packet['bundle_b64']
        if not isinstance(encoded, str) or len(encoded) > (MAX_BYTES + 2) // 3 * 4:
            raise ValueError('size')
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise Refused('bundle-packet-invalid') from None
    if not 0 < len(raw) <= MAX_BYTES or hashlib.sha256(raw).hexdigest() != packet['sha256']:
        raise Refused('bundle-digest-mismatch')
    data_root = Path(data_root)
    _directory(data_root)
    directory = data_root / 'bundles'
    directory.mkdir(mode=0o700, exist_ok=True)
    _directory(directory)
    destination = directory / (packet['sha256'] + '.bundle')
    fd, name = tempfile.mkstemp(prefix='.bundle-', dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            read_blob(destination, packet['sha256'])
        dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        temporary.unlink(missing_ok=True)
    read_blob(destination, packet['sha256'])
    return {'state': 'stored', 'sha256': packet['sha256'], 'bytes': len(raw)}


def import_source(cache, job, data_root, git, ensure_commit):
    """Import exact objects under the worker's existing per-repository lock.

    The caller has verified the current claim and typed review binding. Only
    the pinned historical base may be fetched from the existing origin. The
    unpublished head must come from the exact protected bundle, never a push.
    """
    value = select(job)
    if value is None:
        return
    directory = Path(data_root) / 'bundles'
    _directory(Path(data_root))
    _directory(directory)
    path = directory / (value['sha256'] + '.bundle')
    read_blob(path, value['sha256'])
    ensure_commit(cache, value['base_revision'], git)
    args = ('-C', str(cache), '-c', 'core.hooksPath=/dev/null',
            '-c', 'fetch.fsckObjects=true', '-c', 'transfer.fsckObjects=true')
    git(*args, 'bundle', 'verify', str(path))
    heads = git(*args, 'bundle', 'list-heads', str(path))
    if heads.split() != [job['base_revision'], value['ref']]:
        raise Refused('bundle-head-mismatch')
    git(*args, '-c', 'protocol.file.allow=always', 'fetch', '--quiet', '--no-tags',
        str(path), value['ref'])
    if git(*args, 'rev-parse', job['base_revision'] + '^{tree}') != value['tree']:
        raise Refused('bundle-tree-mismatch')
    git(*args, 'merge-base', '--is-ancestor', value['base_revision'], job['base_revision'])
    read_blob(path, value['sha256'])


if __name__ == '__main__':
    try:
        raw = sys.stdin.read(2 * MAX_BYTES + 1)
        if sys.argv[1:] or len(raw) > 2 * MAX_BYTES:
            raise Refused('bundle-request-invalid')
        print(json.dumps(receive(json.loads(raw), Path.home() / '.local/share/skfleet-fiber'),
                         sort_keys=True))
    except (Refused, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'state': 'refused', 'reason': str(exc) if isinstance(exc, Refused)
                          else type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1)
