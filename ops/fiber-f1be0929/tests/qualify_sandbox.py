"""Run real kernel denial checks, not a mock of bubblewrap arguments."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fiber_sandbox

CHECKS = r'''
import json, os, socket, subprocess
from pathlib import Path
passed = []
status = Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\t1' in status
assert 'CapEff:\t0000000000000000' in status
passed.append('no-new-privileges-and-no-capabilities')
assert not Path('/proc/HOST_PID/cmdline').exists()
passed.append('host-process-hidden')
for path in ['/home/HOST_USER/.ssh', '/home/HOST_USER/.pi', '/home/HOST_USER/.skcapstone',
             '/run/user', '/var/run/docker.sock', '/root/.ssh']:
    assert not Path(path).exists(), path
passed.append('host-credentials-and-management-sockets-hidden')
for path in ['/usr/fiber-deny-probe', '/home/fiber-deny-probe', '/run/fleet/fiber-deny-probe']:
    try:
        Path(path).write_text('must fail')
    except OSError:
        continue
    raise AssertionError('outside write permitted: ' + path)
passed.append('outside-writes-denied')
Path('/work/test.txt').write_text('task-only')
Path('/work/outside').symlink_to('/home')
try:
    Path('/work/outside/fiber-deny-probe').write_text('must fail')
except OSError:
    pass
else:
    raise AssertionError('symlink escaped')
passed.append('symlink-escape-denied')
try:
    assert subprocess.run(['/usr/bin/sudo', '-n', '/usr/bin/id'], capture_output=True).returncode != 0
except FileNotFoundError:
    pass  # A missing sudo executable or interpreter is also a denied operation.
assert subprocess.run(['/usr/bin/unshare', '-Ur', '/usr/bin/true'], capture_output=True).returncode != 0
passed.append('sudo-and-nested-userns-denied')
s = socket.socket()
s.settimeout(1)
assert s.connect_ex(('127.0.0.1', HOST_PORT)) != 0
s.close()
assert [line.split(':')[0].strip() for line in Path('/proc/net/dev').read_text().splitlines()[2:]] == ['lo']
passed.append('host-network-hidden')
subprocess.run(['git', 'init', '-q', '/work/repo'], check=True)
subprocess.run(['git', '-C', '/work/repo', '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                'commit', '--allow-empty', '-qm', 'sandbox qualification'], check=True)
head = subprocess.check_output(['git', '-C', '/work/repo', 'rev-parse', 'HEAD'], text=True).strip()
assert len(head) == 40
passed.append('task-write-and-git-commit-allowed')
result = subprocess.run(['/usr/bin/node', '/opt/pi/@earendil-works/pi-coding-agent/dist/bundle/cli.js',
                         '--no-extensions', '--no-skills', '--version'], capture_output=True, text=True)
assert result.returncode == 0, result.stderr[:1000]
passed.append('pi-startup-in-sandbox')
print(json.dumps({'passed': passed, 'checks':len(passed), 'pi_version': result.stdout.strip()}))
'''


def main():
    """Create disposable test fixtures; never touch real credentials or claims."""
    fiber_sandbox.probe()
    with tempfile.TemporaryDirectory(prefix='fiber-denial-') as td, socket.socket() as listener:
        root = Path(td)
        work, control = root / 'work', root / 'control'
        work.mkdir()
        control.mkdir()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        code = CHECKS.replace('HOST_PID', str(os.getpid())).replace('HOST_USER', Path.home().name)
        code = code.replace('HOST_PORT', str(listener.getsockname()[1]))
        command = fiber_sandbox.command(work, control, Path.home() / '.npm-global/lib/node_modules',
                                       ['/usr/bin/python3', '-c', code])
        return subprocess.run(command, env={'PATH': '/usr/bin:/bin'}, timeout=30).returncode


if __name__ == '__main__':
    sys.exit(main())
