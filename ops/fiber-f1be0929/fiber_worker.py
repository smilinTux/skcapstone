"""Bounded remote execution adapter for reviewed source-only fleet jobs."""
import argparse
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
import tempfile
import time
import urllib.parse

from fiber_admission import Refused, card_fingerprint, read_claim, prepare_worktree, placement_allowed
import fiber_sandbox
import fiber_test_environment
import fiber_bundle
from fiber_gateway_bridge import gateway_socket

ROUTES = {'deepseek': ('skgw-deepseek', 'deepseek-flash'),
          'codex': ('skgw-codex', 'gpt-5.6-sol'), 'zai': ('skgw-zai', 'sk-zai-m')}
STATE = Path.home() / '.local/state/skfleet-fiber'
DATA = Path.home() / '.local/share/skfleet-fiber'
GATEWAY = 'http://chiap01:18790/v1'
WORKER_PATH = fiber_sandbox.WORKER_PATH


def validate(request):
    job, receipt = request['job'], request['receipt']
    card, token = job['card'], receipt['token']
    if not re.fullmatch('[0-9a-f]{8}', card) or not re.fullmatch('[0-9a-f]{32}', token):
        raise Refused('invalid-job-identity')
    if not re.fullmatch('[a-z][a-z0-9-]{0,95}', job['owner']):
        raise Refused('invalid-worker-identity')
    if receipt['card'] != card or receipt['host'] != job['host'] or receipt['provider'] != job['provider']:
        raise Refused('receipt-binding-mismatch')
    expected = f'skfleet-fiber-{card}-{token}.service'
    if receipt['unit'] != expected:
        raise Refused('invalid-unit')
    if ROUTES.get(job['provider']) != (job['pi_provider'], job['model']):
        raise Refused('route-not-approved')
    parsed = urllib.parse.urlsplit(job['repository'])
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise Refused('unsafe-repository')
    if not re.fullmatch('[0-9a-f]{40}', job['base_revision']):
        raise Refused('source-revision-not-exact')
    if job['stage'] not in {'implementation', 'review', 'tests'}:
        raise Refused('invalid-stage')
    if job.get('host') != 'auto' and not placement_allowed(job, job.get('host')):
        raise Refused('stage-placement')
    fiber_test_environment.select(job)
    fiber_bundle.select(job)
    return job, receipt


def source_binding(row, *, review=False):
    """Resolve exact source without discarding conflicting metadata or links."""
    from skcapstone.fleet.builder_dispatch import _source
    binding = {}
    for key in ('repository', 'base_ref', 'base_revision'):
        values = []
        for container in ('meta', 'links'):
            value = (row.get(container) or {}).get(key)
            if value is not None and not isinstance(value, str):
                raise Refused('invalid-source-binding')
            value = (value or '').strip()
            if key == 'base_revision':
                value = value.lower()
            if value:
                values.append(value)
        if len(set(values)) > 1:
            raise Refused('conflicting-source-binding')
        binding[key] = values[0] if values else ''
    original = _source({'meta': binding})
    if not review:
        return original
    # Preserve the historical base in CardStore; review its typed candidate.
    patterns = {'link_head_revision': '[0-9a-f]{40}', 'link_source_card': '[0-9a-f]{8}',
                'producer_identity': '[a-z][a-z0-9-]{0,95}',
                'candidate_evidence_sha256': '[0-9a-f]{64}'}
    pins = {}
    for key, pattern in patterns.items():
        values = [(row.get(container) or {})[key] for container in ('meta', 'links')
                  if key in (row.get(container) or {})]
        if not values or any(not isinstance(value, str) or not re.fullmatch(pattern, value)
                             for value in values):
            raise Refused('invalid-review-binding:' + key)
        if len(set(values)) != 1:
            raise Refused('conflicting-review-binding:' + key)
        pins[key] = values[0]
    return original[0], original[1], pins['link_head_revision']


def verify_source(row, job):
    if job.get('card_fingerprint') != card_fingerprint(row):
        raise Refused('task-contract-changed')
    if 'work_class' in job and re.findall(r'\[(S|M|L|XL)\]', row.get('title', '')) != [job['work_class']]:
        raise Refused('work-class-mismatch')
    labels = set(row.get('labels') or [])
    if job.get('stage') not in {'implementation', 'review', 'tests'}:
        raise Refused('invalid-stage')
    review_marked = ('review' in labels or re.search(r'\[(?:RE-?REVIEW|REVIEW)\]', row.get('title', ''), re.I)
                     or any('link_head_revision' in (row.get(key) or {}) for key in ('meta', 'links')))
    if review_marked and job['stage'] != 'review':
        raise Refused('review-stage-required')
    if 'source-only' not in labels or labels.intersection({'local-only', 'no-egress', 'sovereign-only', 'qwen-only'}):
        raise Refused('source-lane-policy')
    if (row.get('meta') or {}).get('matter_id'):
        raise Refused('matter-not-in-source-lane')
    for label, family in [('codex-only', 'codex'), ('glm-only', 'zai')]:
        if label in labels and job['provider'] != family:
            raise Refused('provider-affinity')
    if source_binding(row, review=job.get('stage') == 'review') != (job['repository'], job['base_ref'], job['base_revision']):
        raise Refused('source-binding-changed')
    fiber_bundle.verify_binding(row, job)


def check_catalog(job):
    """Require a preinstalled agent catalog; never invent qualification here."""
    catalog = json.loads((Path.home() / '.pi/agent/models.json').read_text())
    provider = catalog.get('providers', {}).get(job['pi_provider'], {})
    if provider.get('baseUrl', '').rstrip('/') != GATEWAY:
        raise Refused('gateway-catalog-mismatch')
    if job['model'] not in {model.get('id') for model in provider.get('models', [])}:
        raise Refused('model-not-in-agent-catalog')


def verify_claim(request, *, authority_only=False, wait_seconds=0):
    """Start checks authority; execution checks both. Only replica reads wait."""
    job = request['job']
    claim = request.get('claim', {})
    revision = claim.get('revision')
    if claim.get('authority') != 'chiap08' or claim.get('owner') != job['owner'] or not re.fullmatch('[0-9a-f]{32}', str(revision)):
        raise Refused('authority-claim-required')
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 300:
        raise Refused('invalid-claim-wait')
    deadline = time.monotonic() + wait_seconds
    def authority_read(argv, **kwargs):
        kwargs['timeout'] = min(kwargs.get('timeout', 10), 10)
        command = ['env', 'SKAGENT=' + job['owner'], 'SKCAPSTONE_AGENT=' + job['owner'],
                   '/home/skuser01/.skenv/bin/skcapstone', *argv[1:]]
        return subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                               'chiap08', shlex.join(command)], **kwargs)

    def local_read(argv, **kwargs):
        kwargs['timeout'] = min(kwargs.get('timeout', 10), 10)
        return subprocess.run(argv, **kwargs)

    while True:
        authoritative = read_claim(job['card'], job['owner'], revision=revision, runner=authority_read)
        verify_source(authoritative, job)
        if authority_only:
            return authoritative
        try:
            options = {'runner': local_read} if wait_seconds else {}
            local = read_claim(job['card'], job['owner'], revision=revision, **options)
            verify_source(local, job)
            return local
        except Refused as exc:
            remaining = deadline - time.monotonic()
            if not wait_seconds or remaining <= 0 or str(exc) not in {'claim-owner-or-revision-mismatch', 'task-contract-changed', 'claim-readback-failed'}:
                raise
            time.sleep(min(2, remaining))


def verify_cache_origin(cache, repository, git):
    """Bind the stored source and permit only the estate's exact SSH transport."""
    if cache.is_symlink() or git('-C', str(cache), 'config', '--get-all', 'remote.origin.url') != repository:
        raise Refused('source-cache-mismatch')
    allowed = {repository}
    prefix = 'https://skgit.skstack01.douno.it/'
    if repository.startswith(prefix):
        allowed.add('ssh://git@skgit.skstack01.douno.it:222/' + repository[len(prefix):])
    if git('-C', str(cache), 'remote', 'get-url', 'origin') not in allowed:
        raise Refused('source-cache-mismatch')


def ensure_commit(cache, revision, git):
    """Reuse an exact imported bundle commit; otherwise fetch the pinned origin SHA."""
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise Refused('source-revision-not-exact')
    try:
        kind = git('-C', str(cache), 'cat-file', '-t', revision)
    except Refused:
        git('-C', str(cache), 'fetch', '--quiet', 'origin', revision)
        kind = git('-C', str(cache), 'cat-file', '-t', revision)
    if kind != 'commit':
        raise Refused('source-revision-not-commit')


def unit_values(receipt):
    """Read current service-manager state without inferring a generation."""
    fields = ['Id', 'Description', 'LoadState', 'ActiveState', 'SubState', 'MainPID', 'InvocationID', 'ExecMainCode', 'ExecMainStatus', 'ControlGroup']
    command = ['systemctl', '--user', 'show', receipt['unit']]
    for field in fields:
        command.extend(['-p', field])
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    return result.returncode, values


def stopped_unit(receipt, code, values):
    """Recover only an exact current-boot manager stop with no live cgroup."""
    unknown = dict(receipt, state='unknown')
    invocation = receipt.get('invocation')
    if not re.fullmatch('[0-9a-f]{32}', str(invocation)):
        return unknown
    if values.get('MainPID') != '0' or values.get('ActiveState') != 'inactive':
        return unknown
    uid = str(os.getuid())
    prefix = f'/user.slice/user-{uid}.slice/user@{uid}.service'
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip().replace('-', '')
    def entries(required, extra):
        fields = ','.join([*required, *extra])
        journal = subprocess.run(['journalctl', '--user', '--no-pager', '-o', 'json', '-n', '2',
            '--output-fields=' + fields, *[key + '=' + value for key, value in required.items()]],
            capture_output=True, text=True, timeout=10)
        if journal.returncode or len(journal.stdout) > 32768:
            return []
        try:
            rows = [json.loads(line) for line in journal.stdout.splitlines() if line.strip()]
        except ValueError:
            return []
        if any(not isinstance(row, dict) or any(row.get(k) != v for k, v in required.items()) for row in rows):
            return []
        return rows
    # Journal-supplied underscore fields bind the real cgroup, including
    # nested slices. Do not guess where a systemd version placed the unit.
    history = entries({'_SYSTEMD_USER_UNIT': receipt['unit'], '_SYSTEMD_INVOCATION_ID': invocation,
                       '_BOOT_ID': boot, '_UID': uid}, ['_SYSTEMD_CGROUP'])
    groups = {row.get('_SYSTEMD_CGROUP') for row in history}
    if len(groups) != 1:
        return unknown
    group = groups.pop()
    if (not isinstance(group, str) or not group.startswith(prefix + '/')
            or not group.endswith('/' + receipt['unit']) or '..' in group.split('/')):
        return unknown
    def inactive(returncode, current):
        description = 'SKFleet fiber ' + receipt['card'] + ' ' + receipt['token']
        return (current.get('Id') == receipt['unit'] and current.get('MainPID') == '0'
                and current.get('ActiveState') == 'inactive' and current.get('SubState') == 'dead'
                and current.get('InvocationID') in {'', invocation}
                and current.get('ControlGroup') in {'', group}
                and ((current.get('LoadState') == 'not-found' and returncode in {0, 1})
                     or (returncode == 0 and current.get('LoadState') == 'loaded'
                         and current.get('Description') == description)))
    def group_empty():
        directory = Path('/sys/fs/cgroup' + group)
        if not directory.exists():
            return True
        events = dict(line.split() for line in (directory / 'cgroup.events').read_text().splitlines())
        return events.get('populated') == '0'
    if not inactive(code, values) or not group_empty():
        return unknown
    required = {'USER_UNIT': receipt['unit'], 'USER_INVOCATION_ID': invocation,
                'MESSAGE_ID': '9d1aaa27d60140bd96365438aad20286', 'JOB_RESULT': 'done',
                '_BOOT_ID': boot, '_UID': uid, '_EXE': '/usr/lib/systemd/systemd',
                '_SYSTEMD_CGROUP': prefix + '/init.scope'}
    rows = entries(required, ['__CURSOR', '__REALTIME_TIMESTAMP'])
    if len(rows) != 1:
        return unknown
    event = rows[0]
    if any(event.get(key) != value for key, value in required.items()) or not event.get('__CURSOR'):
        return unknown
    if not inactive(*unit_values(receipt)) or not group_empty():
        return unknown
    return dict(receipt, state='stopped', main_pid=0, exit_code=None,
                stop_receipt={'cursor': event['__CURSOR'], 'unit': receipt['unit'],
                              'invocation': invocation, 'boot_id': boot,
                              'control_group': group,
                              'timestamp_us': event.get('__REALTIME_TIMESTAMP'),
                              'proof': 'systemd-user-manager-stop-completed'})


def read_unit(receipt):
    """Prefer live invocation data; absence alone is never terminal evidence."""
    code, values = unit_values(receipt)
    expected_description = 'SKFleet fiber ' + receipt['card'] + ' ' + receipt['token']
    if code or values.get('LoadState') != 'loaded' or values.get('Id') != receipt['unit'] or values.get('Description') != expected_description:
        return stopped_unit(receipt, code, values)
    invocation = values.get('InvocationID', '')
    if not re.fullmatch('[0-9a-f]{32}', invocation):
        return stopped_unit(receipt, code, values)
    pid = int(values.get('MainPID', '0'))
    state = 'unknown'
    if values.get('ActiveState') in {'active', 'activating'} and pid > 0 and Path('/proc', str(pid)).exists():
        state = 'running'
    elif pid == 0 and values.get('ExecMainCode') not in {'', '0'} and (values.get('ActiveState') in {'failed', 'inactive'} or values.get('SubState') == 'exited'):
        state = 'terminal'
    return dict(receipt, state=state, invocation=invocation, main_pid=pid,
                exit_code=int(values.get('ExecMainStatus', '0')), active_state=values.get('ActiveState'), sub_state=values.get('SubState'))


def environment_plan(job):
    """Require a ready repository-bound artifact, or stdlib when unselected."""
    selector = fiber_test_environment.select(job)
    if selector is None:
        return None
    return fiber_test_environment.qualify(selector, job['repository'])


def verify_environment_lock(workspace, selector):
    """Bind the materialized workspace uv.lock to the selected digest."""
    if selector is None:
        return None
    return fiber_test_environment.verify_lock(Path(workspace) / fiber_test_environment.LOCK_NAME,
                                              selector['lock_sha256'])


def start(request):
    job, receipt = validate(request)
    if socket.gethostname().split('.')[0].lower() != job['host']:
        raise Refused('wrong-execution-host')
    check_catalog(job)
    verify_claim(request, authority_only=True)
    directory = STATE / 'requests'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (receipt['token'] + '.json')
    with path.open('x') as stream:
        json.dump(request, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    command = ['systemd-run', '--user', '--quiet', '--service-type=exec', '--unit', receipt['unit'],
               '--setenv=PATH=' + WORKER_PATH,
               '--description', 'SKFleet fiber ' + job['card'] + ' ' + receipt['token'],
               '--property=RemainAfterExit=yes', '--property=KillMode=control-group',
               '--property=NoNewPrivileges=yes', '--property=UMask=0077',
               '--property=CPUQuota=200%', '--property=MemoryMax=3G', '--property=MemorySwapMax=512M',
               '--property=TasksMax=256', '--property=RuntimeMaxSec=3600',
               str(Path.home() / '.skenv/bin/python'), str(Path(__file__).resolve()), 'run', str(path)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise Refused('remote-start-failed')
    return read_unit(receipt)


def verify_resource_limits(cgroup=None):
    """Refuse task execution unless the kernel enforces the worker budget."""
    if cgroup is None:
        relative = next(line.split(':', 2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
        cgroup = Path('/sys/fs/cgroup') / relative.lstrip('/')
    expected = {'memory.max': '3221225472', 'memory.swap.max': '536870912', 'pids.max': '256'}
    if any((Path(cgroup) / name).read_text().strip() != value for name, value in expected.items()):
        raise Refused('worker-resource-limits')
    quota, period = (Path(cgroup) / 'cpu.max').read_text().split()
    if not quota.isdecimal() or not period.isdecimal() or int(period) <= 0 or int(quota) != 2 * int(period):
        raise Refused('worker-resource-limits')


def execute(request):
    job, receipt = validate(request)
    if socket.gethostname().split('.')[0].lower() != job['host']:
        raise Refused('wrong-execution-host')
    check_catalog(job)
    # Qualify the optional environment before any claim read or workspace action.
    environment = environment_plan(job)
    row = verify_claim(request, wait_seconds=300)
    verify_resource_limits()
    fiber_sandbox.probe()
    guard = Path.home() / '.skenv/bin/pi-cardstore-guard.mjs'
    pi = Path.home() / '.npm-global/bin/pi'
    if not guard.is_file() or not os.access(pi, os.X_OK):
        raise Refused('worker-runtime-unqualified')
    key = hashlib.sha256(job['repository'].encode()).hexdigest()
    cache_root = DATA / 'repositories'
    cache_root.mkdir(parents=True, exist_ok=True)
    cache = cache_root / (key + '.git')
    workspace = DATA / 'workspaces' / (job['card'] + '-' + receipt['token'])
    workspace.parent.mkdir(parents=True, exist_ok=True)
    git_env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    def git(*args):
        result = subprocess.run(['git', *args], env=git_env, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise Refused('source-materialization-failed')
        return result.stdout.strip()
    with (cache_root / (key + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if cache.is_symlink():
            raise Refused('source-cache-mismatch')
        if not cache.exists():
            git('init', '--bare', '--quiet', str(cache))
            git('-C', str(cache), 'remote', 'add', 'origin', job['repository'])
        verify_cache_origin(cache, job['repository'], git)
        fiber_bundle.import_source(cache, job, DATA, git, ensure_commit)
        ensure_commit(cache, job['base_revision'], git)
        fiber_sandbox.private_clone(cache, workspace, 'work/fiber-' + job['card'] + '-' + receipt['token'][:8], job['base_revision'], git)
    runtime = workspace / '.fiber-runtime'
    runtime.mkdir()
    # The workspace lock must match the selector exactly before model work.
    verify_environment_lock(workspace, job.get('test_environment'))
    git('-C', str(workspace), 'config', 'user.name', job['owner'])
    git('-C', str(workspace), 'config', 'user.email', job['owner'] + '@source.invalid')
    (workspace / '.git/info/exclude').write_text('.fiber-runtime/\n')
    prompt = fiber_sandbox.task_prompt(job, row)
    command = [fiber_sandbox.NODE, fiber_sandbox.PI_CLI,
               '--no-approve', '--no-extensions', '--name', job['owner'],
               '--provider', job['pi_provider'], '--model', job['model'], '--thinking', 'off',
               '--no-context-files', '--no-skills', '--tools', 'read,bash,edit,write,grep,find,ls', '-p', prompt]
    catalog = json.loads((Path.home() / '.pi/agent/models.json').read_text())
    credential = catalog['providers'][job['pi_provider']].get('apiKey')
    if isinstance(credential, str) and re.fullmatch('[A-Z_][A-Z0-9_]*', credential):
        credential = os.environ.get(credential)
    if not isinstance(credential, str) or not credential or credential.startswith('!'):
        raise Refused('credential-reference-unavailable')
    public = fiber_sandbox.public_catalog(catalog, job['pi_provider'], job['model'])
    controls = STATE / 'controls'
    controls.mkdir(parents=True, exist_ok=True, mode=0o700)
    control = controls / receipt['token']
    fiber_sandbox.prepare_control(control, workspace, row, public)
    pi_root = Path.home() / '.npm-global/lib/node_modules'
    # Qualify the mounted private prefix inside the sandbox before model work.
    fiber_sandbox.environment_probe(workspace, control, pi_root, environment)
    wrapped = fiber_sandbox.command(workspace, control, pi_root,
        ['/usr/bin/python3', '/opt/fiber_gateway_bridge.py', *command],
        identity={'SKFLEET_CARD_ID': job['card'], 'SKFLEET_PROVIDER': job['provider'],
                  'SKFLEET_MODEL': job['model'], 'SKAGENT': job['owner'], 'SKCAPSTONE_AGENT': job['owner']},
        test_environment=environment)
    with gateway_socket(control / 'gateway.sock', endpoint=GATEWAY, credential=credential,
                        family=job['provider'], model=job['model'],
                        check_claim=lambda: verify_claim(request)) as bridge:
        exit_code = fiber_sandbox.run_logged(wrapped, control / 'worker.log')
        calls = bridge.requests
        failures = list(bridge.failures)
    # No shell or repository-controlled Git command is executed by the host
    # after model work. Even the Git config inside the task is now untrusted.
    verify_claim(request)
    proof = {'card': job['card'], 'claim_revision': row['meta']['_claim_revision'],
             'token': receipt['token'], 'host': job['host'], 'workspace': str(workspace),
             'base_revision': job['base_revision'], 'exit_code': exit_code,
             'gateway_requests': calls, 'gateway_failures': failures,
             'accepted': False, 'state': 'awaiting-external-verification'}
    evidence = control / 'execution.json'
    with evidence.open('x') as stream:
        json.dump(proof, stream, sort_keys=True)
    linked = subprocess.run(['skcapstone', 'coord', 'link', job['card'], 'execution_record',
                             str(evidence), '--agent', job['owner']],
                            capture_output=True, text=True, timeout=30)
    if linked.returncode or (verify_claim(request).get('links') or {}).get('execution_record') != str(evidence):
        raise Refused('execution-evidence-readback-failed')
    return exit_code


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['check', 'start', 'observe', 'run'])
    parser.add_argument('request_path', nargs='?')
    args = parser.parse_args()
    try:
        if args.mode == 'run':
            path = Path(args.request_path).resolve()
            if path.parent != (STATE / 'requests').resolve():
                raise Refused('request-outside-state-directory')
            sys.exit(execute(json.loads(path.read_text())))
        request = json.load(sys.stdin)
        if args.mode == 'check':
            job, receipt = validate(request)
            if socket.gethostname().split('.')[0].lower() != job['host']:
                raise Refused('wrong-execution-host')
            check_catalog(job)
            # Fail before claim readback if the requested environment is absent
            # or its private Python/pytest/pydantic imports fail in-sandbox.
            environment = environment_plan(job)
            fiber_sandbox.probe()
            if environment is not None:
                with tempfile.TemporaryDirectory(prefix='fiber-env-preflight-') as td:
                    root = Path(td)
                    (root / 'work/.fiber-runtime/home').mkdir(parents=True)
                    (root / 'control').mkdir()
                    fiber_sandbox.environment_probe(root / 'work', root / 'control',
                        Path.home() / '.npm-global/lib/node_modules', environment)
            if not (Path.home() / '.skenv/bin/pi-cardstore-guard.mjs').is_file() or not os.access(Path.home() / '.npm-global/bin/pi', os.X_OK):
                raise Refused('worker-runtime-unqualified')
            print(json.dumps({'state': 'ready', 'card': job['card'], 'host': job['host'], 'model': job['model']}))
        elif args.mode == 'start':
            print(json.dumps(start(request), sort_keys=True))
        else:
            validate(request)
            print(json.dumps(read_unit(request['receipt']), sort_keys=True))
    except (Refused, KeyError, ValueError, OSError, subprocess.SubprocessError) as exc:
        # Do not leak command output, credentials, or remote exception payloads.
        print(json.dumps({'state': 'blocked', 'reason': str(exc) if isinstance(exc, Refused) else type(exc).__name__}), file=sys.stderr)
        sys.exit(70)
