"""Curated private root for source-only Pi workers. No host network or keys."""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from fiber_admission import Refused

WORKER_PATH = ':'.join(str(Path.home() / path) for path in ('.skenv/bin', '.npm-global/bin', '.local/bin')) + ':/usr/local/bin:/usr/bin:/bin'
NODE = '/opt/node/bin/node'
PI_CLI = '/opt/pi/@earendil-works/pi-coding-agent/dist/bundle/cli.js'


def node_executable():
    """Resolve only the trusted controller path, never job or shell input."""
    node = shutil.which('node', path=WORKER_PATH)
    if not node or not Path(node).is_file() or not os.access(node, os.X_OK):
        raise Refused('worker-node-unavailable')
    return Path(node).resolve(strict=True)


def private_clone(cache, workspace, branch, revision, git):
    """Create independent Git metadata and objects; never erase an old path."""
    if os.path.lexists(workspace):
        raise Refused('occupied-workspace')
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise Refused('source-revision-not-exact')
    git('check-ref-format', '--branch', branch)
    git('init', '--quiet', str(workspace))
    git('-C', str(workspace), '-c', 'protocol.file.allow=always', 'fetch', '--quiet', '--no-tags', str(cache), revision)
    git('-C', str(workspace), 'checkout', '--quiet', '-b', branch, revision)
    if not (Path(workspace) / '.git').is_dir() or (Path(workspace) / '.git/objects/info/alternates').exists():
        raise Refused('shared-git-metadata')


def public_catalog(catalog, provider, model):
    """Retain qualified SDK compatibility settings, but no host auth or URLs."""
    entry = catalog.get('providers', {}).get(provider, {})
    matches = [row for row in entry.get('models', []) if row.get('id') == model]
    if entry.get('baseUrl', '').rstrip('/') != 'http://chiap01:18790/v1' or len(matches) != 1:
        raise Refused('sandbox-catalog-mismatch')
    fields = ('id', 'name', 'api', 'reasoning', 'input', 'cost', 'contextWindow', 'maxTokens', 'compat', 'thinkingLevelMap')
    selected = {k: copy.deepcopy(v) for k, v in matches[0].items() if k in fields}
    api = selected.get('api', entry.get('api'))
    if api != 'openai-completions':
        raise Refused('sandbox-api-unqualified')
    selected['api'] = api
    selected['compat'] = {**copy.deepcopy(entry.get('compat', {})), **selected.get('compat', {})}
    selected['maxTokens'] = min(selected.get('maxTokens', 8192), 8192)
    return {'providers': {provider: {'baseUrl': 'http://127.0.0.1:18790/v1',
                                     'api': api, 'apiKey': 'isolated-controller', 'models': [selected]}}}


def environment_mount(test_environment):
    """Validate one qualified environment into read-only bind/env additions.

    Only the operator venv is exposed, read-only, at the fixed private prefix
    /opt/test-env. Its bin is prepended to PATH and validated repository-
    relative source directories are added to PYTHONPATH under /work. No host
    path is writable and no host credential or network is introduced.
    """
    if test_environment is None:
        return [], {}
    venv = Path(test_environment['venv'])
    if venv.is_symlink() or not (venv / 'bin').is_dir():
        raise Refused('test-environment-unavailable')
    source_dirs = test_environment.get('source_dirs')
    if not isinstance(source_dirs, list):
        raise Refused('test-environment-source-dir')
    pythonpath = []
    for directory in source_dirs:
        if (not isinstance(directory, str) or not directory or directory.startswith('/')
                or directory.startswith('~') or '\\' in directory or '\x00' in directory
                or any(part in {'', '.', '..'} for part in directory.split('/'))
                or not re.fullmatch('[A-Za-z0-9_][A-Za-z0-9_./-]*', directory)):
            raise Refused('test-environment-source-dir')
        pythonpath.append('/work/' + directory)
    additions = [['--ro-bind', str(venv), '/opt/test-env']]
    environment = {'PATH': '/opt/test-env/bin:/opt/node/bin:/usr/bin:/bin'}
    if pythonpath:
        environment['PYTHONPATH'] = ':'.join(pythonpath)
    return additions, environment


def command(workspace, control, pi_root, argv, *, identity=None, test_environment=None):
    """Build a root containing system binaries and exactly one writable repo."""
    mounts, environment = environment_mount(test_environment)
    result = ['/usr/bin/bwrap', '--unshare-all', '--unshare-user', '--disable-userns', '--new-session',
              '--die-with-parent', '--cap-drop', 'ALL', '--clearenv',
              '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
              '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
              '--symlink', 'usr/bin', '/sbin', '--proc', '/proc', '--dev', '/dev',
              '--tmpfs', '/tmp', '--tmpfs', '/run', '--dir', '/etc', '--dir', '/home',
              '--bind', str(workspace), '/work', '--ro-bind', str(control), '/run/fleet',
              '--ro-bind', str(pi_root), '/opt/pi',
              '--ro-bind', str(node_executable()), NODE,
              '--ro-bind', str(Path(__file__).with_name('fiber_gateway_bridge.py')), '/opt/fiber_gateway_bridge.py']
    for mount in mounts:
        result += mount
    result += ['--remount-ro', '/', '--remount-ro', '/run', '--chdir', '/work']
    # Host sudo wrappers, shell initialization, keys, DBus and Docker sockets
    # are deliberately not present. /usr contains binaries, not operator state.
    base_environment = {'HOME': '/work/.fiber-runtime/home', 'PATH': '/opt/node/bin:/usr/bin:/bin',
                        'LANG': 'C.UTF-8', 'TMPDIR': '/tmp', 'GIT_CONFIG_NOSYSTEM': '1',
                        'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
                        'SKLEGAL_SIMULATION': '1', 'SKLEGAL_TEST_PORT': '0',
                        'SKLEGAL_TEST_DB': '/work/.fiber-runtime/test.sqlite3',
                        'SKLEGAL_TEST_OUTPUT': '/work/.fiber-runtime/output'}
    base_environment.update(environment)
    for name, value in base_environment.items():
        result += ['--setenv', name, value]
    for name, value in (identity or {}).items():
        if name not in {'SKFLEET_CARD_ID', 'SKFLEET_PROVIDER', 'SKFLEET_MODEL', 'SKAGENT', 'SKCAPSTONE_AGENT'}:
            raise Refused('sandbox-environment-refused')
        result += ['--setenv', name, value]
    return result + ['--', *argv]


def environment_probe(workspace, control, pi_root, test_environment, *, timeout=60):
    """Qualify the private Python prefix inside the real sandbox boundary.

    Requires the mounted /opt/test-env prefix to report that exact prefix and
    to import pytest and pydantic under Python 3.12. The host interpreter is
    never consulted, so host availability cannot qualify a task. Any failure
    refuses before model execution or workspace model work.
    """
    if test_environment is None:
        return True
    probe_code = ('import sys; assert sys.prefix == "/opt/test-env", sys.prefix; '
                  'assert sys.version_info[:2] == (3, 12), sys.version_info; '
                  'import pytest, pydantic')
    result = subprocess.run(command(workspace, control, pi_root,
        ['/opt/test-env/bin/python3.12', '-c', probe_code], test_environment=test_environment),
        env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise Refused('test-environment-imports-failed')
    return True


def review_prompt(job):
    """Describe the evidence-only review proposal contract inside the sandbox."""
    if job['stage'] != 'review':
        return ''
    return (
        f" Also write docs/evidence/agents/{job['card']}/REVIEW-DECISION.json with exactly these fields: "
        f'{{"schema":"skfleet.review-proposal/v1","card":"{job["card"]}",'
        f'"source_head":"{job["base_revision"]}","verdict":"PASS|FAIL|BLOCKED",'
        '"evidence_sha256":"SHA256 of your COMPLETION-EVIDENCE.md bytes"}. '
        "Choose exactly one verdict. The report must explain findings and exact tests, including any omissions. "
        f"Do not modify source or tests: your only committed changes may be under docs/evidence/agents/{job['card']}/. "
        "The JSON is a proposal only; controller verification and native completion gates still apply."
    )


def task_prompt(job, row):
    """Keep the existing worker boundary plus explicit review-only artifacts."""
    return (
        f"Work only card {job['card']} as {job['owner']}. The dispatcher already claimed it. "
        f"The trusted controller verified owner and exact revision {row['meta']['_claim_revision']}. "
        f"Your exact workspace source revision is {job['base_revision']}; a review uses its candidate head, not its historical base. "
        "Read the immutable controller-provided card snapshot /run/fleet/card.json. "
        "Read AGENTS.md, the assigned TDD, current criteria and dependencies before task actions. "
        "If any binding or claim differs, STOP and report BLOCKED. "
        "After every file write run ls. After any authorized commit run git rev-parse HEAD and echo its hash. "
        "Stop immediately if tool output looks wrong. After the second compaction write .handoff.md, finish the current step and stop. "
        "No push, application deployment, external legal actions, or HammerTime Inbox access. Commit only if the card explicitly requests it. "
        "Use only /work. You have no host privileges, SSH, Docker socket, direct provider access or coordination credentials. "
        "Do not attempt to obtain them. If dependencies or tools are unavailable, report BLOCKED, not a workaround. "
        "Tests must use task-owned databases and outputs, port 0 or an isolated dynamic port, and simulation mode. "
        "Use exact artifact paths in card links; never search the entire filesystem or unrelated workspaces. "
        "Never touch another task's processes, claims, workspaces, databases, or ports. "
        f"Write exact completion evidence to docs/evidence/agents/{job['card']}/COMPLETION-EVIDENCE.md. "
        "Board operations are performed only by the trusted controller outside the sandbox. "
        "Write your proposed outcome and evidence in the worktree; the controller records execution, not acceptance. "
        "Never mark a producer card complete on its own review or fabricate checks. Report files, exact tests/results, limitations, and verified artifacts, then stop."
    ) + review_prompt(job)


def probe():
    """Require namespaces and actual isolated Pi startup before admission."""
    result = subprocess.run(['/usr/bin/bwrap', '--unshare-all', '--unshare-user', '--disable-userns',
        '--die-with-parent', '--cap-drop', 'ALL', '--ro-bind', '/usr', '/usr',
        '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
        '--proc', '/proc', '/usr/bin/python3', '-c',
        "from pathlib import Path; s=Path('/proc/self/status').read_text(); assert 'NoNewPrivs:\\t1' in s"],
        capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise Refused('worker-sandbox-unqualified')
    with tempfile.TemporaryDirectory(prefix='fiber-runtime-') as td:
        work, control = Path(td) / 'work', Path(td) / 'control'
        (work / '.fiber-runtime/home').mkdir(parents=True)
        control.mkdir()
        runtime = subprocess.run(command(work, control, Path.home() / '.npm-global/lib/node_modules',
            [NODE, PI_CLI, '--no-extensions', '--no-skills', '--version']),
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, capture_output=True, text=True, timeout=15)
        if runtime.returncode:
            raise Refused('worker-runtime-startup-failed')


def prepare_control(control, workspace, row, catalog):
    """Write only public run descriptors outside model-writable task storage."""
    control.mkdir(mode=0o700)
    (control / 'card.json').write_text(json.dumps(row, sort_keys=True))
    home = workspace / '.fiber-runtime/home'
    agent = home / '.pi/agent'
    agent.mkdir(parents=True)
    (agent / 'models.json').write_text(json.dumps(catalog, sort_keys=True))
    (workspace / '.fiber-runtime/output').mkdir()


def run_logged(argv, destination, *, limit=16 * 1024 * 1024):
    """Bound host-side transcript growth and stop only this sandbox process."""
    with destination.open('xb') as output, subprocess.Popen(argv,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT) as process:
        total = 0
        try:
            while chunk := process.stdout.read1(65536):
                remaining = limit - total
                output.write(chunk[:remaining])
                total += len(chunk)
                if total > limit:
                    raise Refused('worker-transcript-limit')
            return process.wait()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
