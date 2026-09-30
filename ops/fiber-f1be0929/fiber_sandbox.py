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


def command(workspace, control, pi_root, argv, *, identity=None):
    """Build a root containing system binaries and exactly one writable repo."""
    result = ['/usr/bin/bwrap', '--unshare-all', '--unshare-user', '--disable-userns', '--new-session',
              '--die-with-parent', '--cap-drop', 'ALL', '--clearenv',
              '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
              '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
              '--symlink', 'usr/bin', '/sbin', '--proc', '/proc', '--dev', '/dev',
              '--tmpfs', '/tmp', '--tmpfs', '/run', '--dir', '/etc', '--dir', '/home',
              '--bind', str(workspace), '/work', '--ro-bind', str(control), '/run/fleet',
              '--ro-bind', str(pi_root), '/opt/pi',
              '--ro-bind', str(node_executable()), NODE,
              '--ro-bind', str(Path(__file__).with_name('fiber_gateway_bridge.py')), '/opt/fiber_gateway_bridge.py',
              '--remount-ro', '/', '--remount-ro', '/run', '--chdir', '/work']
    # Host sudo wrappers, shell initialization, keys, DBus and Docker sockets
    # are deliberately not present. /usr contains binaries, not operator state.
    for name, value in {'HOME': '/work/.fiber-runtime/home', 'PATH': '/opt/node/bin:/usr/bin:/bin',
                        'LANG': 'C.UTF-8', 'TMPDIR': '/tmp', 'GIT_CONFIG_NOSYSTEM': '1',
                        'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
                        'SKLEGAL_SIMULATION': '1', 'SKLEGAL_TEST_PORT': '0',
                        'SKLEGAL_TEST_DB': '/work/.fiber-runtime/test.sqlite3',
                        'SKLEGAL_TEST_OUTPUT': '/work/.fiber-runtime/output'}.items():
        result += ['--setenv', name, value]
    for name, value in (identity or {}).items():
        if name not in {'SKFLEET_CARD_ID', 'SKFLEET_PROVIDER', 'SKFLEET_MODEL', 'SKAGENT', 'SKCAPSTONE_AGENT'}:
            raise Refused('sandbox-environment-refused')
        result += ['--setenv', name, value]
    return result + ['--', *argv]


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
