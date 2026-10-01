"""Reconcile actual tests inside the existing locked lifecycle dispatcher."""
import base64
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import time
import uuid

from fiber_admission import Refused
from fiber_bundle import _directory
from fiber_refill import _read, _sync_directory, digest
from fiber_test_acceptance import verified_tests
from fiber_test_plane import qualified_plan
from fiber_test_worker import LIMIT, identity, validate


def remote(mode, request):
    """Only the fixed installed test adapter is exposed over existing SSH."""
    validate(request)
    if mode not in {'check', 'start', 'observe', 'collect'}:
        raise Refused('test-transport-mode')
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
        'skuser01@' + request['requirements']['host'],
        shlex.join(['/home/skuser01/.skenv/bin/python',
                    '/home/skuser01/.local/lib/skfleet-fiber/fiber_test_worker.py', mode])]
    run = subprocess.run(command, input=json.dumps(request), capture_output=True,
                         text=True, timeout=40)
    if run.returncode or len(run.stdout) > 24 * 1024 * 1024:
        raise Refused('test-transport-' + mode + '-unacknowledged')
    return json.loads(run.stdout)


def observation(request, value, previous=None):
    """Bind live service-manager observations to one immutable request."""
    from fiber_test_plane import _hex
    if (not isinstance(value, dict) or any(value.get(k) != v for k, v in identity(request).items())
            or value.get('state') not in {'running', 'terminal'}
            or not _hex(value.get('invocation'), 32)
            or (previous is not None and value['invocation'] != previous['invocation'])):
        raise Refused('test-execution-ambiguous')
    return value


def publish_result(directory, request, packet, observed, *, now, once, evidence):
    """Publish retained bytes first, then a receipt validated by the existing gate."""
    if not isinstance(packet, dict) or set(packet) != {'observation', 'result', 'logs'}:
        raise Refused('test-collection-shape')
    current = observation(request, packet['observation'], observed)
    if current != observed or current['state'] != 'terminal':
        raise Refused('test-collection-unit-changed')
    result, logs = packet['result'], packet['logs']
    fields = {*identity(request), 'invocation', 'started_at', 'finished_at', 'checks'}
    if (not isinstance(result, dict) or set(result) != fields
            or any(result.get(k) != v for k, v in identity(request).items())
            or result['invocation'] != current['invocation']
            or not isinstance(result['checks'], list)
            or len(result['checks']) != len(request['plan']['checks'])
            or not isinstance(logs, dict)):
        raise Refused('test-collection-binding')
    receipt_checks = []
    names = set()
    for expected, check in zip(request['plan']['checks'], result['checks']):
        fields = {'id', 'argv', 'passed', 'failed', 'errors', 'skipped', 'exit_code', 'log_file', 'log_sha256'}
        if (not isinstance(check, dict) or set(check) != fields or check['id'] != expected['id']
                or check['argv'] != expected['argv'] or check['log_file'] != check['id'] + '.log'):
            raise Refused('test-collection-check-binding')
        name = check['log_file']
        encoded = logs.get(name)
        if not isinstance(encoded, str) or len(encoded) > (LIMIT + 2) // 3 * 4:
            raise Refused('test-collection-log-size')
        raw = base64.b64decode(encoded, validate=True)
        if not 0 < len(raw) <= LIMIT or hashlib.sha256(raw).hexdigest() != check['log_sha256']:
            raise Refused('test-collection-log-digest')
        # Prefix keeps candidate/review artifacts and earlier test logs distinct.
        local_name = 'test-' + name
        evidence(directory / local_name, raw)
        names.add(name)
        receipt_checks.append({**check, 'log_file': local_name,
            'host': request['requirements']['host'], 'unit': request['unit'], 'invocation': current['invocation']})
    if set(logs) != names:
        raise Refused('test-collection-extra-logs')
    once(directory / 'test-execution-result.json', result)
    binding = {k: request['plan'][k] for k in ('source_card', 'source_head', 'source_fingerprint', 'repository')}
    receipt = {'schema': 'skfleet.test-receipt/v1', **binding,
               'plan_sha256': digest(request['plan']), 'started_at': result['started_at'],
               'finished_at': result['finished_at'], 'checks': receipt_checks}
    # Retain failed execution evidence, but never publish it as successful acceptance.
    if type(current.get('exit_code')) is not int or current['exit_code'] != 0:
        raise Refused('test-required-check-failed')
    once(directory / 'test-receipt.json', receipt)
    return verified_tests(directory, request['producer'], now=now)


def ensure_tests(directory, producer, review, *, state, requirements_path, stage_bundle,
                 guard, transport=remote, clock=time.time):
    """Start once, observe across restarts, and collect one exact test service.

    Caller holds the existing authority lock. Missing qualification is disabled;
    it never creates a plan from model claims. No call waits for test execution.
    """
    from fiber_lifecycle import _once, _evidence
    directory, state, requirements_path = map(Path, (directory, state, requirements_path))
    _directory(directory)
    _directory(state)
    intent_path = directory / 'test-execution-intent.json'
    existing = intent_path.exists() or intent_path.is_symlink()
    if not requirements_path.exists() and not requirements_path.is_symlink():
        if existing:
            raise Refused('test-requirements-lost')
        return None
    requirements, plan = qualified_plan(requirements_path, producer, clock())
    guard()
    if review.get('proposal', {}).get('verdict') != 'PASS':
        raise Refused('test-independent-review-required')
    if existing:
        request = _read(intent_path)
        if (request.get('producer') != producer or request.get('requirements') != requirements
                or request.get('plan') != plan or request.get('review_sha256') != digest(review)
                or request.get('source_revision') != review['source_revision']):
            raise Refused('test-intent-changed')
    else:
        token = uuid.uuid4().hex
        request = {'schema': 'skfleet.test-execution/v1', 'producer': producer,
            'source_revision': review['source_revision'], 'review_sha256': digest(review),
            'requirements': requirements, 'plan': plan, 'token': token,
            'unit': 'skfleet-test-' + producer['job']['card'] + '-' + token + '.service'}
    bundle_job = validate(request, clock())
    reservations = state / 'test-reservations'
    reservations.mkdir(mode=0o700, exist_ok=True)
    _directory(reservations)
    # Reserve one card/request, not the machine. Legacy host pins remain audit
    # evidence; they do not serialize unrelated test runs on that host.
    reservation = reservations / (request['plan']['source_card'] + '.json')
    pin = identity(request)
    occupied = reservation.exists() or reservation.is_symlink()
    ack_path = directory / 'test-accepted.json'
    if ack_path.exists() or ack_path.is_symlink():
        accepted = verified_tests(directory, producer, now=clock())
        ack = _read(ack_path)
        invocation = _read(directory / 'test-invocation.json')
        result = _read(directory / 'test-execution-result.json')
        if (not existing or not accepted or ack != {'request_sha256': digest(request),
                'acceptance': accepted, 'result_sha256': digest(result), 'invocation': invocation}
                or any(invocation.get(k) != v for k, v in pin.items())):
            raise Refused('test-accepted-binding-lost')
        if occupied and _read(reservation) == pin:
            reservation.unlink()
            _sync_directory(reservations)
        return accepted
    if occupied and _read(reservation) != pin:
        raise Refused('test-card-reservation-conflict')
    launched = directory / 'test-launch-intent.json'
    if not (launched.exists() or launched.is_symlink()):
        readiness = transport('check', request)
        if readiness != {'request_sha256': digest(request), 'state': 'ready'}:
            raise Refused('test-preflight-unverified')
    _once(intent_path, request)
    _once(directory / 'test-plan.json', plan)
    _once(reservation, pin)
    if not (launched.exists() or launched.is_symlink()):
        stage_bundle({**bundle_job, 'host': requirements['host']})
        guard()
        _once(launched, pin)
        # Persist before the remote call. Lost acknowledgement cannot replay it.
        observed = observation(request, transport('start', request))
    else:
        if _read(launched) != pin:
            raise Refused('test-launch-intent-changed')
        observed = observation(request, transport('observe', request))
    invocation_path = directory / 'test-invocation.json'
    _once(invocation_path, {**pin, 'invocation': observed['invocation']})
    if observed['state'] != 'terminal':
        return None
    packet = transport('collect', request)
    guard()
    # The requirement bytes and service identity must still be current after SSH.
    if qualified_plan(requirements_path, producer, clock()) != (requirements, plan):
        raise Refused('test-requirements-raced')
    if observation(request, transport('observe', request), observed) != observed:
        raise Refused('test-unit-raced')
    accepted = publish_result(directory, request, packet, observed,
                             now=clock(), once=_once, evidence=_evidence)
    if not accepted or _read(reservation) != pin:
        raise Refused('test-acceptance-or-reservation-lost')
    _once(ack_path, {'request_sha256': digest(request), 'acceptance': accepted,
        'result_sha256': digest(packet['result']), 'invocation': _read(invocation_path)})
    reservation.unlink()
    _sync_directory(reservations)
    return accepted
