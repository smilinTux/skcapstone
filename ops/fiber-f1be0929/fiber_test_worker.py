"""Bounded deterministic test services, without model credentials or authority."""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import time

from fiber_admission import Refused
import fiber_bundle
from fiber_refill import _read, _publish, digest
from fiber_test_plane import test_plan, _hex
import fiber_sandbox
from fiber_worker import (environment_plan, ensure_commit, verify_cache_origin,
                          verify_environment_lock, verify_resource_limits, unit_values)

STATE = Path.home() / '.local/state/skfleet-fiber'
DATA = Path.home() / '.local/share/skfleet-fiber'
LIMIT = 1024 * 1024
DEADLINE = 1200


def validate(request, now=None):
    """Every operation revalidates immutable requirements and candidate pins."""
    fields = {'schema', 'producer', 'source_revision', 'review_sha256',
              'requirements', 'plan', 'token', 'unit'}
    if not isinstance(request, dict) or set(request) != fields or request['schema'] != 'skfleet.test-execution/v1':
        raise Refused('test-request-shape')
    producer, requirements = request['producer'], request['requirements']
    plan = test_plan(requirements, producer, time.time() if now is None else now)
    card = plan['source_card']
    if (plan != request['plan'] or not _hex(request['token'], 32)
            or not _hex(request['review_sha256'], 64) or not _hex(request['source_revision'], 64)
            or not _hex(producer.get('claim_revision'), 32)
            or request['unit'] != 'skfleet-test-' + card + '-' + request['token'] + '.service'):
        raise Refused('test-request-binding')
    candidate = producer['candidate']
    transport_job = {'stage': 'review', 'repository': plan['repository'],
                     'base_revision': plan['source_head'],
                     'source_bundle': {'sha256': candidate.get('bundle_sha256'),
                         'tree': candidate.get('tree'), 'ref': candidate.get('ref'),
                         'base_revision': producer['job'].get('base_revision')}}
    fiber_bundle.select(transport_job)
    if requirements['test_environment'] is not None:
        transport_job['test_environment'] = requirements['test_environment']
    return transport_job


def authorize(request):
    """Read current native source identity on the existing authority host."""
    validate(request)
    if socket.gethostname().split('.')[0].lower() != 'chiap08':
        raise Refused('test-not-authority')
    from fiber_control import cards
    from fiber_finish import native_state
    from fiber_worker import verify_source
    producer = request['producer']
    job = producer['job']
    row = cards([job['card']])[job['card']]
    verify_source(row, job)
    current = native_state(Path.home() / '.skcapstone', job['card'])
    if (current['revision'] != request['source_revision']
            or current['owner'] != job['owner']
            or current['claim_revision'] != producer['claim_revision']
            or current['status'] not in {'ready', 'doing', 'review'}
            or row.get('archived') or (row.get('meta') or {}).get('claim_conflicts')):
        raise Refused('test-source-superseded')
    return {'authorized_sha256': digest(request)}


def authority_check(request):
    """Do not rely on a replicated card or a model-provided claim."""
    script = '/home/skuser01/.local/lib/skfleet-fiber/fiber_test_worker.py'
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', 'skuser01@chiap08',
               shlex.join(['/home/skuser01/.skenv/bin/python', script, 'authorize'])]
    run = subprocess.run(command, input=json.dumps(request), text=True,
                         capture_output=True, timeout=30)
    if run.returncode or json.loads(run.stdout) != {'authorized_sha256': digest(request)}:
        raise Refused('test-authority-refused')


def private(path):
    path.mkdir(exist_ok=True, mode=0o700)
    fiber_bundle._directory(path)
    return path


def paths(request):
    fiber_bundle._directory(STATE)
    root = private(STATE / 'tests')
    return private(root / request['token'])


def identity(request):
    return {'card': request['plan']['source_card'], 'host': request['requirements']['host'],
            'token': request['token'], 'unit': request['unit'], 'request_sha256': digest(request)}


def observe(request):
    """Missing units and changed descriptions remain unknown, never successful."""
    validate(request)
    binding = identity(request)
    if socket.gethostname().split('.')[0].lower() != binding['host']:
        raise Refused('test-wrong-host')
    code, values = unit_values(binding)
    description = 'SKFleet test ' + digest(request)
    if (code or values.get('LoadState') != 'loaded' or values.get('Id') != request['unit']
            or values.get('Description') != description or not _hex(values.get('InvocationID'), 32)):
        return dict(binding, state='unknown')
    pid = values.get('MainPID')
    state = 'unknown'
    if pid and pid.isdecimal() and int(pid) > 0 and values.get('ActiveState') in {'active', 'activating'}:
        state = 'running'
    elif (pid == '0' and values.get('ExecMainCode') == '1'
          and (values.get('SubState') == 'exited' or values.get('ActiveState') == 'failed')):
        state = 'terminal'
    status = values.get('ExecMainStatus', '')
    return dict(binding, state=state, invocation=values['InvocationID'],
                exit_code=int(status) if status.isdecimal() else None)


def headroom():
    from skcapstone.fleet.capacity import admit_headroom
    admitted, _, memory = admit_headroom(Path('/proc/meminfo').read_text())
    cpus = os.cpu_count() or 1
    if (not admitted or memory['MemAvailable'] < 4 * 1024 * 1024 or cpus < 2
            or os.getloadavg()[0] > max(1, cpus - 2)):
        raise Refused('test-host-capacity')


def start(request):
    validate(request)
    if socket.gethostname().split('.')[0].lower() != request['requirements']['host']:
        raise Refused('test-wrong-host')
    directory = paths(request)
    path = directory / 'request.json'
    if path.exists() or path.is_symlink():
        if _read(path) != request:
            raise Refused('test-request-conflict')
        # An ambiguous earlier start is never repeated, even if no unit exists.
        return observe(request)
    authority_check(request)
    headroom()
    _publish(path, request)
    command = ['systemd-run', '--user', '--quiet', '--service-type=exec', '--unit', request['unit'],
        '--description', 'SKFleet test ' + digest(request),
        '--property=RemainAfterExit=yes', '--property=KillMode=control-group',
        '--property=NoNewPrivileges=yes', '--property=UMask=0077',
        '--property=CPUQuota=200%', '--property=MemoryMax=3G', '--property=MemorySwapMax=512M',
        '--property=TasksMax=256', '--property=RuntimeMaxSec=' + str(DEADLINE),
        str(Path.home() / '.skenv/bin/python'), str(Path(__file__).resolve()), 'run', str(path)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise Refused('test-launch-unacknowledged')
    return observe(request)


def check(request):
    """A normal capacity wait does not create a launch intent or a failed service."""
    validate(request)
    if socket.gethostname().split('.')[0].lower() != request['requirements']['host']:
        raise Refused('test-wrong-host')
    try:
        headroom()
    except Refused as exc:
        if str(exc) != 'test-host-capacity':
            raise
        return {'request_sha256': digest(request), 'state': 'capacity'}
    return {'request_sha256': digest(request), 'state': 'ready'}


def materialize(request):
    job = validate(request)
    fiber_bundle._directory(DATA)
    cache_root = private(DATA / 'repositories')
    key = hashlib.sha256(job['repository'].encode()).hexdigest()
    cache = cache_root / (key + '.git')
    workspace = private(DATA / 'test-workspaces') / request['token']
    def git(*args):
        run = subprocess.run(['git', *args], env=dict(os.environ, GIT_TERMINAL_PROMPT='0'),
                             capture_output=True, text=True, timeout=120)
        if run.returncode:
            raise Refused('test-source-materialization')
        return run.stdout.strip()
    lockfd = os.open(cache_root / (key + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lockfd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if cache.is_symlink():
            raise Refused('test-cache-unsafe')
        if not cache.exists():
            git('init', '--bare', '--quiet', str(cache))
            git('-C', str(cache), 'remote', 'add', 'origin', job['repository'])
        verify_cache_origin(cache, job['repository'], git)
        fiber_bundle.import_source(cache, job, DATA, git, ensure_commit)
        fiber_sandbox.private_clone(cache, workspace, 'work/test-' + request['token'], job['base_revision'], git)
    verify_environment_lock(workspace, job.get('test_environment'))
    return workspace, environment_plan(job)


def sandbox_command(workspace, directory, output, argv, environment):
    """Reuse curated namespaces, with source read-only and one fresh output mount."""
    command = fiber_sandbox.command(workspace, directory,
        Path.home() / '.npm-global/lib/node_modules', [], test_environment=environment)
    mounts = ['--remount-ro', '/work', '--bind', str(output), '/test-output',
        '--ro-bind', str(Path(__file__).with_name('fiber_test_harness.py')), '/opt/test-harness.py']
    additions = ['--setenv', 'HOME', '/tmp', '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
        '--setenv', 'SKLEGAL_TEST_DB', '/tmp/test.sqlite3', '--setenv', 'SKLEGAL_TEST_OUTPUT', '/test-output']
    # The request is read-only and stdin is opened by the sandboxed adapter.
    launcher = ('import os,sys; f=os.open("/run/fleet/argv.json",os.O_RDONLY); '
                'os.dup2(f,0); os.close(f); os.execv(sys.argv[1],sys.argv[1:])')
    interpreter = '/usr/bin/python3' if argv[0] == 'python3' else argv[0]
    root_lock = next(i for i in range(len(command) - 1) if command[i:i+2] == ['--remount-ro', '/'])
    return command[:root_lock] + mounts + command[root_lock:-1] + additions + ['--', '/usr/bin/python3', '-c', launcher,
        interpreter, '/opt/test-harness.py', '/test-output/report.json']


def framework_report(path, argv, exit_code):
    """An actual process exit and a complete structured report must agree."""
    report = _read(path)
    if (set(report) != {'schema', 'argv', 'exit_code', 'passed', 'failed', 'errors', 'skipped'}
            or report['schema'] != 'skfleet.framework-report/v1' or report['argv'] != argv
            or type(report['exit_code']) is not int or report['exit_code'] != exit_code
            or any(type(report[k]) is not int or not 0 <= report[k] <= 1000000
                   for k in ('passed', 'failed', 'errors'))
            or not isinstance(report['skipped'], list) or len(report['skipped']) > 256
            or any(not isinstance(s, str) or not s or len(s) > 512 for s in report['skipped'])
            or len(set(report['skipped'])) != len(report['skipped'])):
        raise Refused('test-framework-report-invalid')
    return report


def execute(request):
    validate(request)
    if socket.gethostname().split('.')[0].lower() != request['requirements']['host']:
        raise Refused('test-wrong-host')
    verify_resource_limits()
    authority_check(request)
    invocation = os.environ.get('INVOCATION_ID')
    if not _hex(invocation, 32):
        raise Refused('test-service-invocation-required')
    directory = paths(request)
    # A host-wide lock independently bounds concurrent test services.
    fd = os.open(directory.parent / 'execution.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('test-host-reserved') from None
        workspace, environment = materialize(request)
        started = time.time()
        checks = []
        for check in request['plan']['checks']:
            authority_check(request)
            control = private(directory / check['id'])
            output = private(control / 'output')
            # The argv file is a list; _publish has no object-shape restriction.
            _publish(control / 'argv.json', check['argv'])
            command = sandbox_command(workspace, control, output, check['argv'], environment)
            log = directory / (check['id'] + '.log')
            code = fiber_sandbox.run_logged(command, log, limit=LIMIT)
            report = framework_report(output / 'report.json', check['argv'], code)
            raw = log.read_bytes()
            if not raw:
                raise Refused('test-empty-log')
            checks.append({'id': check['id'], 'argv': check['argv'],
                **{k: report[k] for k in ('passed', 'failed', 'errors', 'skipped', 'exit_code')},
                'log_file': log.name, 'log_sha256': hashlib.sha256(raw).hexdigest()})
        authority_check(request)
        result = {**identity(request), 'invocation': invocation, 'started_at': started,
                  'finished_at': time.time(), 'checks': checks}
        _publish(directory / 'result.json', result)
        return 0 if all(c['exit_code'] == 0 and c['failed'] == c['errors'] == 0 for c in checks) else 1


def collect(request):
    observed = observe(request)
    if observed['state'] != 'terminal':
        return observed
    directory = paths(request)
    result = _read(directory / 'result.json')
    if any(result.get(k) != v for k, v in {**identity(request), 'invocation': observed['invocation']}.items()):
        raise Refused('test-result-binding')
    logs = {}
    for check in result['checks']:
        if not re.fullmatch('[a-z][a-z0-9-]{0,63}\\.log', str(check.get('log_file'))):
            raise Refused('test-result-log-name')
        raw = fiber_bundle.read_blob(directory / check['log_file'], check['log_sha256'])
        if len(raw) > LIMIT:
            raise Refused('test-result-log-limit')
        logs[check['log_file']] = base64.b64encode(raw).decode()
    if observe(request) != observed:
        raise Refused('test-unit-changed-during-collection')
    return {'observation': observed, 'result': result, 'logs': logs}


if __name__ == '__main__':
    try:
        mode = sys.argv[1]
        if mode == 'run':
            raise SystemExit(execute(_read(Path(sys.argv[2]))))
        raw = sys.stdin.read(65537)
        if len(raw) > 65536:
            raise Refused('test-request-too-large')
        request = json.loads(raw)
        function = {'check': check, 'start': start, 'observe': observe, 'collect': collect, 'authorize': authorize}[mode]
        print(json.dumps(function(request), sort_keys=True))
    except (Refused, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(json.dumps({'state': 'refused', 'reason': str(exc) if isinstance(exc, Refused)
                          else type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1)
