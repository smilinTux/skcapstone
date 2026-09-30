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
import urllib.parse

from fiber_admission import Refused, card_fingerprint, read_claim, prepare_worktree

ROUTES = {'deepseek': ('skgw-deepseek', 'deepseek-flash'),
          'codex': ('skgw-codex', 'gpt-5.6-sol'), 'zai': ('skgw-zai', 'sk-zai-m')}
STATE = Path.home() / '.local/state/skfleet-fiber'
DATA = Path.home() / '.local/share/skfleet-fiber'
GATEWAY = 'http://chiap01:18790/v1'
WORKER_PATH = ':'.join(str(Path.home() / path) for path in ('.skenv/bin', '.npm-global/bin', '.local/bin')) + ':/usr/local/bin:/usr/bin:/bin'


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
    return job, receipt


def source_binding(row):
    """Normalize the existing metadata/link contract across installed versions.

    Older node libraries read metadata only. Never discard conflicting links
    to make that older parser accept a different source.
    """
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
    return _source({'meta': binding})


def verify_source(row, job):
    if job.get('card_fingerprint') != card_fingerprint(row):
        raise Refused('task-contract-changed')
    labels = set(row.get('labels') or [])
    if 'source-only' not in labels or labels.intersection({'local-only', 'no-egress', 'sovereign-only', 'qwen-only'}):
        raise Refused('source-lane-policy')
    if (row.get('meta') or {}).get('matter_id'):
        raise Refused('matter-not-in-source-lane')
    for label, family in [('codex-only', 'codex'), ('glm-only', 'zai')]:
        if label in labels and job['provider'] != family:
            raise Refused('provider-affinity')
    if source_binding(row) != (job['repository'], job['base_ref'], job['base_revision']):
        raise Refused('source-binding-changed')


def check_catalog(job):
    """Require a preinstalled agent catalog; never invent qualification here."""
    catalog = json.loads((Path.home() / '.pi/agent/models.json').read_text())
    provider = catalog.get('providers', {}).get(job['pi_provider'], {})
    if provider.get('baseUrl', '').rstrip('/') != GATEWAY:
        raise Refused('gateway-catalog-mismatch')
    if job['model'] not in {model.get('id') for model in provider.get('models', [])}:
        raise Refused('model-not-in-agent-catalog')


def verify_claim(request):
    """Never claim on a replica. Require authority and local visibility to agree."""
    job = request['job']
    claim = request.get('claim', {})
    revision = claim.get('revision')
    if claim.get('authority') != 'chiap08' or claim.get('owner') != job['owner'] or not re.fullmatch('[0-9a-f]{32}', str(revision)):
        raise Refused('authority-claim-required')

    def authority_read(argv, **kwargs):
        command = ['env', 'SKAGENT=' + job['owner'], 'SKCAPSTONE_AGENT=' + job['owner'],
                   '/home/skuser01/.skenv/bin/skcapstone', *argv[1:]]
        return subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                               'chiap08', shlex.join(command)], **kwargs)

    authoritative = read_claim(job['card'], job['owner'], revision=revision, runner=authority_read)
    verify_source(authoritative, job)
    local = read_claim(job['card'], job['owner'], revision=revision)
    verify_source(local, job)
    return local


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


def read_unit(receipt):
    fields = ['Id', 'Description', 'LoadState', 'ActiveState', 'SubState', 'MainPID', 'InvocationID', 'ExecMainCode', 'ExecMainStatus']
    command = ['systemctl', '--user', 'show', receipt['unit']]
    for field in fields:
        command.extend(['-p', field])
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    expected_description = 'SKFleet fiber ' + receipt['card'] + ' ' + receipt['token']
    if result.returncode or values.get('LoadState') != 'loaded' or values.get('Id') != receipt['unit'] or values.get('Description') != expected_description:
        return dict(receipt, state='unknown')
    invocation = values.get('InvocationID', '')
    if not re.fullmatch('[0-9a-f]{32}', invocation):
        return dict(receipt, state='unknown')
    pid = int(values.get('MainPID', '0'))
    state = 'unknown'
    if values.get('ActiveState') in {'active', 'activating'} and pid > 0 and Path('/proc', str(pid)).exists():
        state = 'running'
    elif pid == 0 and values.get('ExecMainCode') not in {'', '0'} and (values.get('ActiveState') in {'failed', 'inactive'} or values.get('SubState') == 'exited'):
        state = 'terminal'
    return dict(receipt, state=state, invocation=invocation, main_pid=pid,
                exit_code=int(values.get('ExecMainStatus', '0')), active_state=values.get('ActiveState'), sub_state=values.get('SubState'))


def start(request):
    job, receipt = validate(request)
    if socket.gethostname().split('.')[0].lower() != job['host']:
        raise Refused('wrong-execution-host')
    check_catalog(job)
    verify_claim(request)
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
               '--property=CPUQuota=200%', '--property=MemoryMax=3G', '--property=MemorySwapMax=512M',
               '--property=TasksMax=256', '--property=RuntimeMaxSec=3600',
               str(Path.home() / '.skenv/bin/python'), str(Path(__file__).resolve()), 'run', str(path)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise Refused('remote-start-failed')
    return read_unit(receipt)


def execute(request):
    job, receipt = validate(request)
    if socket.gethostname().split('.')[0].lower() != job['host']:
        raise Refused('wrong-execution-host')
    check_catalog(job)
    row = verify_claim(request)
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
        if cache.is_symlink() or git('-C', str(cache), 'remote', 'get-url', 'origin') != job['repository']:
            raise Refused('source-cache-mismatch')
        ensure_commit(cache, job['base_revision'], git)
        prepare_worktree(cache, workspace, 'work/fiber-' + job['card'] + '-' + receipt['token'][:8], job['base_revision'])
    runtime = workspace / '.fiber-runtime'
    runtime.mkdir()
    (runtime / 'tmp').mkdir()
    (runtime / 'output').mkdir()
    env = {'HOME': str(Path.home()), 'PATH': WORKER_PATH,
           'LANG': 'C.UTF-8', 'SKAGENT': job['owner'], 'SKCAPSTONE_AGENT': job['owner'],
           'SKFLEET_CARD_ID': job['card'], 'SKFLEET_CLAIM_REVISION': row['meta']['_claim_revision'],
           'SKFLEET_PROVIDER': job['provider'], 'SKFLEET_MODEL': job['model'],
           'TMPDIR': str(runtime / 'tmp'), 'SKLEGAL_TEST_OUTPUT': str(runtime / 'output'),
           'SKLEGAL_TEST_DB': str(runtime / 'test.sqlite3'), 'SKLEGAL_TEST_PORT': '0', 'SKLEGAL_SIMULATION': '1'}
    prompt = (
        f"Work only card {job['card']} as {job['owner']}. The dispatcher already claimed it. "
        f"Verify owner and exact revision {row['meta']['_claim_revision']} through mediated coord reads; do not re-claim or release another owner's claim. "
        "Read AGENTS.md, the assigned TDD, current criteria and dependencies before task actions. "
        "If any binding or claim differs, STOP and report BLOCKED. "
        "After every file write run ls. After any authorized commit run git rev-parse HEAD and echo its hash. "
        "Stop immediately if tool output looks wrong. After the second compaction write .handoff.md, finish the current step and stop. "
        "No push, application deployment, external legal actions, or HammerTime Inbox access. Commit only if the card explicitly requests it. "
        "Use only this isolated worktree. Tests must use task-owned databases and outputs, port 0 or an isolated dynamic port, and simulation mode. "
        "Never touch another task's processes, claims, workspaces, databases, or ports. "
        f"Write exact completion evidence to docs/evidence/agents/{job['card']}/COMPLETION-EVIDENCE.md. "
        "Report files, exact tests/results, limitations, and verified artifacts, then stop."
    )
    command = [str(pi), '--no-approve', '--extension', str(guard), '--name', job['owner'],
               '--provider', job['pi_provider'], '--model', job['model'], '--thinking', 'off',
               '--no-context-files', '--no-skills', '--tools', 'read,bash,edit,write,grep,find,ls', '-p', prompt]
    return subprocess.run(command, cwd=workspace, env=env).returncode


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
            if not (Path.home() / '.skenv/bin/pi-cardstore-guard.mjs').is_file() or not os.access(Path.home() / '.npm-global/bin/pi', os.X_OK):
                raise Refused('worker-runtime-unqualified')
            runtime = subprocess.run([str(Path.home() / '.npm-global/bin/pi'), '--version'],
                                     env={'HOME': str(Path.home()), 'PATH': WORKER_PATH, 'LANG': 'C.UTF-8'},
                                     capture_output=True, text=True, timeout=15)
            if runtime.returncode:
                raise Refused('worker-runtime-startup-failed')
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
