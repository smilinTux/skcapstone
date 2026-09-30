"""Read exact terminal source artifacts without executing repository code on host.

This is evidence collection, not a verdict, test runner, claim handoff or
acceptance gate. The caller must independently verify the returned candidate.
All Git access after model execution stays inside the existing private root.
"""
import base64
import binascii
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys

from fiber_admission import Refused
import fiber_worker as worker

MAX_EVIDENCE = 256 * 1024
MAX_BUNDLE = 8 * 1024 * 1024

# This program is controller-owned, not a command supplied by a card or model.
_INSPECT = r'''
import base64, hashlib, json, os, re, resource, stat, subprocess, sys
from pathlib import Path
resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
base, card, branch, include_bundle, stage = sys.argv[1:]
root = Path('/work')
assert (root / '.git').is_dir() and not (root / '.git').is_symlink()
assert not os.path.lexists(root / '.git/objects/info/alternates')
assert not os.path.lexists(root / '.git/objects/info/http-alternates')
env = dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_NO_REPLACE_OBJECTS='1')
def git(*args):
    return subprocess.run(['/usr/bin/git', '--no-replace-objects',
        '--git-dir=/work/.git', '--work-tree=/work',
        '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
        *args], env=env, check=True, capture_output=True, timeout=15).stdout
commit = git('rev-parse', '--verify', 'HEAD^{commit}').decode().strip()
tree = git('rev-parse', '--verify', 'HEAD^{tree}').decode().strip()
ref = git('symbolic-ref', 'HEAD').decode().strip()
assert ref == branch
git('merge-base', '--is-ancestor', base, commit)
assert commit != base, 'no candidate commit'
assert not git('status', '--porcelain', '--untracked-files=all'), 'dirty candidate'
relative = 'docs/evidence/agents/' + card + '/COMPLETION-EVIDENCE.md'
path = root
for part in relative.split('/'):
    path = path / part
    assert not path.is_symlink()
fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
with os.fdopen(fd, 'rb') as stream:
    assert stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
    data = stream.read(262145)
assert 0 < len(data) <= 262144
assert git('cat-file', 'blob', commit + ':' + relative) == data, 'uncommitted evidence'
assert git('rev-parse', '--verify', 'HEAD^{commit}').decode().strip() == commit
packet = {'commit': commit, 'tree': tree, 'ref': ref,
    'evidence_sha256': hashlib.sha256(data).hexdigest(),
    'evidence_b64': base64.b64encode(data).decode()}
if stage == 'review':
    # A review may add its own evidence, never amend the reviewed source.
    prefix = 'docs/evidence/agents/' + card + '/'
    changed = git('diff', '--no-ext-diff', '--no-textconv', '--name-only',
                  '--no-renames', '-z', base, commit).decode().split('\0')
    assert all(name.startswith(prefix) for name in changed if name), 'review changed source'
    decision_path = root / (prefix + 'REVIEW-DECISION.json')
    assert not decision_path.is_symlink()
    with os.fdopen(os.open(decision_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        assert stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
        decision = stream.read(16385)
    assert 0 < len(decision) <= 16384
    assert git('cat-file', 'blob', commit + ':' + prefix + 'REVIEW-DECISION.json') == decision
    packet.update(review_decision_b64=base64.b64encode(decision).decode(),
                  review_decision_sha256=hashlib.sha256(decision).hexdigest())
if include_bundle == 'yes':
    bundle = git('bundle', 'create', '-', branch, '^' + base)
    assert 0 < len(bundle) <= 8 * 1024 * 1024, 'bundle exceeds source-only bound'
    packet.update(bundle_sha256=hashlib.sha256(bundle).hexdigest(),
                  bundle_b64=base64.b64encode(bundle).decode())
    assert git('rev-parse', '--verify', 'HEAD^{commit}').decode().strip() == commit
print(json.dumps(packet))
'''


def validate_packet(packet, request, *, include_bundle=False):
    """Validate returned bytes, never infer PASS from evidence prose."""
    fields = {'commit', 'tree', 'ref', 'evidence_sha256', 'evidence_b64'}
    if request['job']['stage'] == 'review':
        fields |= {'review_decision_b64', 'review_decision_sha256'}
    if include_bundle:
        fields |= {'bundle_sha256', 'bundle_b64'}
    if not isinstance(packet, dict) or set(packet) != fields:
        raise Refused('candidate-packet-invalid')
    for name, size in (('commit', 40), ('tree', 40), ('evidence_sha256', 64)):
        if not isinstance(packet[name], str) or not re.fullmatch('[0-9a-f]{' + str(size) + '}', packet[name]):
            raise Refused('candidate-packet-invalid')
    branch = 'refs/heads/work/fiber-' + request['job']['card'] + '-' + request['receipt']['token'][:8]
    if packet['ref'] != branch or packet['commit'] == request['job']['base_revision']:
        raise Refused('candidate-source-invalid')
    try:
        encoded = packet['evidence_b64']
        if not isinstance(encoded, str) or len(encoded) > (MAX_EVIDENCE + 2) // 3 * 4:
            raise ValueError('size')
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise Refused('candidate-evidence-invalid') from None
    if not 0 < len(data) <= MAX_EVIDENCE or hashlib.sha256(data).hexdigest() != packet['evidence_sha256']:
        raise Refused('candidate-evidence-invalid')
    if request['job']['stage'] == 'review':
        review_decision(packet, request['job'])
    if include_bundle:
        try:
            encoded = packet['bundle_b64']
            if not isinstance(encoded, str) or len(encoded) > (MAX_BUNDLE + 2) // 3 * 4:
                raise ValueError('size')
            bundle = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise Refused('candidate-bundle-invalid') from None
        if not 0 < len(bundle) <= MAX_BUNDLE or hashlib.sha256(bundle).hexdigest() != packet['bundle_sha256']:
            raise Refused('candidate-bundle-invalid')
    return packet


def review_decision(packet, job):
    """Decode a reviewer's typed proposal, not authorization or test evidence."""
    def unique(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError('duplicate key')
        return value
    try:
        encoded = packet['review_decision_b64']
        if not isinstance(encoded, str) or len(encoded) > 21848:
            raise ValueError('size')
        raw = base64.b64decode(encoded, validate=True)
        if not 0 < len(raw) <= 16384 or hashlib.sha256(raw).hexdigest() != packet['review_decision_sha256']:
            raise ValueError('digest')
        value = json.loads(raw, object_pairs_hook=unique)
        if (not isinstance(value, dict)
                or set(value) != {'schema', 'card', 'source_head', 'verdict', 'evidence_sha256'}
                or value['schema'] != 'skfleet.review-proposal/v1'
                or value['card'] != job['card'] or value['source_head'] != job['base_revision']
                or value['verdict'] not in {'PASS', 'FAIL', 'BLOCKED'}
                or value['evidence_sha256'] != packet['evidence_sha256']):
            raise ValueError('binding')
    except (KeyError, TypeError, ValueError, binascii.Error):
        raise Refused('candidate-review-proposal-invalid') from None
    return value


def inspect_workspace(request, workspace, control, *, include_bundle=False):
    """Read Git and evidence inside the existing sandbox, with /work read-only."""
    job, receipt = request['job'], request['receipt']
    branch = 'refs/heads/work/fiber-' + job['card'] + '-' + receipt['token'][:8]
    program = ['/usr/bin/python3', '-I', '-c', _INSPECT, job['base_revision'], job['card'], branch,
               'yes' if include_bundle else 'no', job['stage']]
    argv = worker.fiber_sandbox.command(workspace, control,
        Path.home() / '.npm-global/lib/node_modules', program)
    separator = argv.index('--')
    argv[separator:separator] = ['--remount-ro', '/work']
    result = subprocess.run(argv, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
                            capture_output=True, text=True, timeout=30)
    if result.returncode or len(result.stdout) > 2 * (MAX_BUNDLE if include_bundle else MAX_EVIDENCE):
        raise Refused('candidate-inspection-failed')
    try:
        packet = json.loads(result.stdout)
    except ValueError:
        raise Refused('candidate-packet-invalid') from None
    return validate_packet(packet, request, include_bundle=include_bundle)


def _terminal(receipt):
    """Require a currently observed successful exit of the recorded invocation."""
    current = worker.read_unit(receipt)
    if (any(current.get(key) != receipt[key] for key in
            ('card', 'host', 'token', 'unit', 'invocation'))
            or current.get('state') != 'terminal'
            or type(current.get('main_pid')) is not int or current['main_pid'] != 0
            or type(current.get('exit_code')) is not int or current['exit_code'] != 0):
        raise Refused('candidate-not-exact-terminal')


def _directory(path):
    """Use the same private-path rule without importing authority-only refill."""
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise Refused('candidate-unsafe-directory')


def _record(path):
    """Read immutable controller evidence and its exact byte hash on one fd."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise Refused('candidate-unsafe-execution')
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise Refused('candidate-execution-too-large')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise Refused('candidate-execution-invalid')
    return value, hashlib.sha256(raw).hexdigest()


def collect_candidate(request, *, include_bundle=False):
    """Bind a read-only candidate snapshot to its original claim and unit.

    No board or ledger mutation occurs. Rechecking claim and service identity
    after collection refuses evidence that raced a revocation or new run.
    """
    job, receipt = worker.validate(request)
    if socket.gethostname().split('.')[0].lower() != job['host']:
        raise Refused('wrong-execution-host')
    if not re.fullmatch('[0-9a-f]{32}', str(receipt.get('invocation'))):
        raise Refused('candidate-invocation-required')
    state, data = worker.STATE, worker.DATA
    for directory in (state, state / 'requests', state / 'controls', data, data / 'workspaces'):
        _directory(directory)
    saved, _ = _record(state / 'requests' / (receipt['token'] + '.json'))
    original_job = dict(saved.get('job') or {})
    original_receipt = dict(saved.get('receipt') or {})
    # Dispatch stores claimable only in the launch request, not the ledger.
    # Its reservation timestamp is metadata, not part of observe() identity.
    claimable = original_job.pop('claimable', None)
    reserved = original_receipt.pop('reserved_at', None)
    if (claimable is not True or type(reserved) not in (int, float)
            or not math.isfinite(reserved) or reserved <= 0
            or original_job != job or original_receipt !=
            {key: value for key, value in receipt.items() if key != 'invocation'}):
        raise Refused('candidate-request-changed')
    worker.verify_claim(saved, authority_only=True)
    _terminal(receipt)
    control = state / 'controls' / receipt['token']
    workspace = data / 'workspaces' / (job['card'] + '-' + receipt['token'])
    _directory(control)
    _directory(workspace)
    record_path = control / 'execution.json'
    record, record_hash = _record(record_path)
    expected = {'card': job['card'], 'claim_revision': saved['claim']['revision'],
        'token': receipt['token'], 'host': job['host'], 'workspace': str(workspace),
        'base_revision': job['base_revision'], 'state': 'awaiting-external-verification'}
    if (any(record.get(key) != value for key, value in expected.items())
            or record.get('accepted') is not False or record.get('gateway_failures') != []
            or type(record.get('exit_code')) is not int or record['exit_code'] != 0
            or type(record.get('gateway_requests')) is not int or record['gateway_requests'] < 1):
        raise Refused('candidate-execution-mismatch')
    packet = validate_packet(inspect_workspace(saved, workspace, control, include_bundle=include_bundle),
                             saved, include_bundle=include_bundle)
    worker.verify_claim(saved, authority_only=True)
    _terminal(receipt)
    if _record(record_path)[1] != record_hash:
        raise Refused('candidate-execution-changed')
    return {'schema': 'skfleet.candidate/v1', 'card': job['card'], 'token': receipt['token'],
        'host': job['host'], 'unit': receipt['unit'], 'invocation': receipt['invocation'],
        'owner': job['owner'], 'claim_revision': saved['claim']['revision'],
        'base_revision': job['base_revision'], 'execution_sha256': record_hash,
        'candidate': packet, 'accepted': False}


if __name__ == '__main__':
    # Read-only transport entrypoint. It accepts no model-supplied shell command.
    try:
        if sys.argv[1:] not in ([], ['--bundle']):
            raise Refused('candidate-invalid-mode')
        raw = sys.stdin.read(65537)
        if len(raw) > 65536:
            raise Refused('candidate-request-too-large')
        print(json.dumps(collect_candidate(json.loads(raw), include_bundle=bool(sys.argv[1:])),
                         sort_keys=True))
    except (Refused, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'state': 'refused', 'reason': str(exc) if isinstance(exc, Refused)
                          else type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1)
