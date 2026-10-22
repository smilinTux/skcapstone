"""Validate task-qualified test-plane evidence, never a model's test claims.

The operator-owned plan identifies the original card's required tests. The
trusted test plane produces receipts and retained logs outside all model
sandboxes. Hashes bind those bytes, not the truth of arbitrary self-authored
reports. This reader neither runs model-provided commands nor issues receipts.
"""
import math
from pathlib import Path
import re

from fiber_admission import Refused
from fiber_bundle import read_blob
from fiber_refill import MAX_TTL, _directory, _read, digest


def _hex(value, size):
    """Reject malformed pins, including non-string lookalikes."""
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(size) + '}', value)


def verified_tests(directory, producer, *, now):
    """Return a current exact-head receipt, or None while test evidence is absent.

    These fixed paths are in controller-private lifecycle storage. Their
    content must come from the qualified test plane, never from candidate
    worktrees or the model's completion report. The caller retains admission
    authority and rechecks the original source/review generations separately.
    """
    directory = Path(directory)
    _directory(directory)
    plan_path, receipt_path = directory / 'test-plan.json', directory / 'test-receipt.json'
    if any(not path.exists() and not path.is_symlink() for path in (plan_path, receipt_path)):
        return None
    plan, receipt = _read(plan_path), _read(receipt_path)
    binding = {'source_card': producer['job']['card'],
               'source_head': producer['candidate']['commit'],
               'source_fingerprint': producer['job']['card_fingerprint'],
               'repository': producer['job']['repository']}
    plan_fields = {'schema', 'checks', 'qualified_at', 'expires_at', 'evidence_sha256', *binding}
    receipt_fields = {'schema', 'checks', 'plan_sha256', 'started_at', 'finished_at', *binding}
    if (set(plan) != plan_fields or plan['schema'] != 'skfleet.test-plan/v1'
            or set(receipt) != receipt_fields or receipt['schema'] != 'skfleet.test-receipt/v1'
            or any(plan.get(key) != value or receipt.get(key) != value for key, value in binding.items())
            or not _hex(plan['evidence_sha256'], 64) or receipt['plan_sha256'] != digest(plan)):
        raise Refused('test-acceptance-binding')
    clocks = [plan['qualified_at'], receipt['started_at'], receipt['finished_at'], now, plan['expires_at']]
    if (any(type(value) not in (int, float) or not math.isfinite(value) for value in clocks)
            or not clocks[0] <= clocks[1] <= clocks[2] <= clocks[3] < clocks[4]
            or not 0 < clocks[4] - clocks[0] <= MAX_TTL):
        raise Refused('test-acceptance-stale')
    expected, actual = plan['checks'], receipt['checks']
    if (not isinstance(expected, list) or not 1 <= len(expected) <= 16
            or not isinstance(actual, list) or len(actual) != len(expected)):
        raise Refused('test-acceptance-coverage')
    names = set()
    for requirement, result in zip(expected, actual):
        if (not isinstance(requirement, dict)
                or set(requirement) != {'id', 'argv', 'minimum_passed', 'allowed_skips'}
                or not isinstance(result, dict)
                or set(result) != {'id', 'argv', 'passed', 'failed', 'errors', 'skipped',
                                   'exit_code', 'host', 'unit', 'invocation', 'log_file', 'log_sha256'}):
            raise Refused('test-acceptance-check-shape')
        name, argv = requirement['id'], requirement['argv']
        allowed, skipped = requirement['allowed_skips'], result['skipped']
        if (not isinstance(name, str) or not re.fullmatch('[a-z][a-z0-9-]{0,63}', name)
                or name in names or result['id'] != name or result['argv'] != argv
                or not isinstance(argv, list) or not 1 <= len(argv) <= 32
                or any(not isinstance(arg, str) or not arg or len(arg) > 1024
                       or any(ord(char) < 32 for char in arg) for arg in argv)
                or type(requirement['minimum_passed']) is not int or requirement['minimum_passed'] < 1
                or type(result['passed']) is not int or result['passed'] < requirement['minimum_passed']
                or any(type(result[key]) is not int or result[key] != 0
                       for key in ('failed', 'errors', 'exit_code'))):
            raise Refused('test-acceptance-check-failed')
        if (any(not isinstance(value, list) or len(value) > 256 for value in (allowed, skipped))
                or any(not isinstance(value, str) or not value or len(value) > 512
                       for value in [*allowed, *skipped])
                or len(set(allowed)) != len(allowed) or len(set(skipped)) != len(skipped)
                or not set(skipped) <= set(allowed)):
            raise Refused('test-acceptance-unqualified-skip')
        if (result['host'] not in {'chiap02', 'chiap04', 'chiwk13', 'ziowk01'}
                or not isinstance(result['unit'], str)
                or not re.fullmatch('skfleet-test-' + binding['source_card'] + '-[0-9a-f]{32}\\.service', result['unit'])
                or not _hex(result['invocation'], 32) or not _hex(result['log_sha256'], 64)
                or not isinstance(result['log_file'], str)
                or not re.fullmatch('[a-z0-9][a-z0-9-]{0,95}\\.log', result['log_file'])):
            raise Refused('test-acceptance-execution-binding')
        if not read_blob(directory / result['log_file'], result['log_sha256']):
            raise Refused('test-acceptance-empty-log')
        names.add(name)
    return {'plan_sha256': digest(plan), 'receipt_sha256': digest(receipt),
            'source_head': binding['source_head'], 'checks': sorted(names),
            'receipt_path': str(receipt_path), 'expires_at': plan['expires_at']}
