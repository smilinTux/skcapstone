"""Compile private operator requirements for actual candidate test execution."""
import copy
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

from fiber_admission import Refused
from fiber_refill import MAX_TTL, _directory, _read
from fiber_test_environment import select as select_environment

TEST_HOSTS = {'chiap02', 'chiap04', 'chiwk13', 'ziowk01'}


def _hex(value, size):
    """Require an exact lowercase digest or identity pin."""
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(size) + '}', value)


def _checks(checks, selector):
    """Qualify bounded framework commands and explicit minimum coverage."""
    if not isinstance(checks, list) or not 1 <= len(checks) <= 16:
        raise Refused('test-plan-coverage')
    names = set()
    for check in checks:
        if (not isinstance(check, dict)
                or set(check) != {'id', 'argv', 'minimum_passed', 'allowed_skips'}):
            raise Refused('test-plan-check-shape')
        name, argv, skips = check['id'], check['argv'], check['allowed_skips']
        if (not isinstance(name, str) or not re.fullmatch('[a-z][a-z0-9-]{0,63}', name)
                or name in names or type(check['minimum_passed']) is not int
                or not 1 <= check['minimum_passed'] <= 1000000
                or not isinstance(argv, list) or not 3 <= len(argv) <= 32
                or any(not isinstance(arg, str) or not arg or len(arg) > 1024
                       or any(ord(char) < 32 for char in arg) for arg in argv)):
            raise Refused('test-plan-check-invalid')
        interpreters = {'python3', '/usr/bin/python3'} if selector is None else {'/opt/test-env/bin/python3.12'}
        if (argv[0] not in interpreters or argv[1] != '-m'
                or argv[2] not in {'unittest', 'pytest'}
                or (argv[2] == 'pytest' and selector is None)):
            raise Refused('test-plan-command-unqualified')
        if (not isinstance(skips, list) or len(skips) > 256
                or any(not isinstance(item, str) or not item or len(item) > 512
                       or any(ord(char) < 32 for char in item) for item in skips)
                or len(set(skips)) != len(skips)):
            raise Refused('test-plan-skips-invalid')
        names.add(name)


def test_plan(requirements, producer, now):
    """Bind existing qualified requirements to the exact candidate head.

    This pure compiler neither executes tests nor certifies a test result.
    Runtime callers must use qualified_plan to establish private-file custody.
    The remote executor separately qualifies the pinned environment artifact.
    """
    fields = {'schema', 'source_card', 'source_fingerprint', 'repository', 'checks',
              'qualified_at', 'expires_at', 'evidence_sha256', 'host', 'test_environment'}
    if (not isinstance(requirements, dict) or set(requirements) != fields
            or requirements['schema'] != 'skfleet.test-requirements/v1'):
        raise Refused('test-plan-requirements-shape')
    try:
        binding = {'source_card': producer['job']['card'],
                   'source_head': producer['candidate']['commit'],
                   'source_fingerprint': producer['job']['card_fingerprint'],
                   'repository': producer['job']['repository']}
    except (KeyError, TypeError):
        raise Refused('test-plan-producer-invalid') from None
    if (not _hex(binding['source_card'], 8) or not _hex(binding['source_head'], 40)
            or not _hex(binding['source_fingerprint'], 64)
            or any(requirements[key] != binding[key] for key in
                   ('source_card', 'source_fingerprint', 'repository'))
            or not _hex(requirements['evidence_sha256'], 64)):
        raise Refused('test-plan-source-binding')
    repository = binding['repository']
    try:
        url = urlsplit(repository) if isinstance(repository, str) else None
        valid_url = url and url.scheme == 'https' and url.hostname and not (
            url.username or url.password or url.query or url.fragment)
    except ValueError:
        valid_url = False
    if (not valid_url or not isinstance(requirements['host'], str)
            or requirements['host'] not in TEST_HOSTS):
        raise Refused('test-plan-placement-or-repository')
    start, end = requirements['qualified_at'], requirements['expires_at']
    if (any(type(value) not in (int, float) or not math.isfinite(value)
            for value in (start, now, end))
            or not start <= now < end or not 0 < end - start <= MAX_TTL):
        raise Refused('test-plan-stale-requirements')
    selected = requirements['test_environment']
    selector = select_environment({} if selected is None else {'test_environment': selected})
    _checks(requirements['checks'], selector)
    return {'schema': 'skfleet.test-plan/v1', **binding,
            'checks': copy.deepcopy(requirements['checks']),
            'qualified_at': start, 'expires_at': end,
            'evidence_sha256': requirements['evidence_sha256']}


def qualified_plan(path, producer, now):
    """Read only a private operator record, then compile its exact plan."""
    path = Path(path)
    _directory(path.parent)
    requirements = _read(path)
    return requirements, test_plan(requirements, producer, now)
