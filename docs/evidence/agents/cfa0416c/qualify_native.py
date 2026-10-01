"""Public fixture qualification only; never issues fleet acceptance receipts."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'lib'))
from fiber_test_worker import sandbox_command, framework_report, verify_resource_limits


def main():
    verify_resource_limits()
    invocation = os.environ['INVOCATION_ID']
    results = []
    fixtures = {
        'pass': '''import unittest, socket
from pathlib import Path
class Fixture(unittest.TestCase):
 def test_readonly(self):
  with self.assertRaises(OSError): Path('/work/no-write').write_text('no')
 def test_no_external_network(self):
  with socket.socket() as s:
   s.settimeout(1)
   with self.assertRaises(OSError): s.connect(('192.0.2.1', 80))
 def test_output(self): Path('/test-output/proof').write_text('yes')
''',
        'fail': 'import unittest\nclass Fixture(unittest.TestCase):\n def test_bad(self): self.fail("intentional public fixture")\n',
        'early-zero': 'import os\nos._exit(0)\n',
    }
    for name, source in fixtures.items():
        root = ROOT / name
        root.mkdir(mode=0o700)
        work, control, output = (root / key for key in ('work', 'control', 'output'))
        for path in (work, control, output): path.mkdir(mode=0o700)
        (work / 'test_fixture.py').write_text(source)
        argv = ['python3', '-m', 'unittest', 'discover']
        (control / 'argv.json').write_text(json.dumps(argv))
        before = time.time()
        run = subprocess.run(sandbox_command(work, control, output, argv, None),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        (root / 'test.log').write_bytes(run.stdout)
        report = None
        if name == 'early-zero':
            assert run.returncode == 0 and not (output / 'report.json').exists()
        else:
            report = framework_report(output / 'report.json', argv, run.returncode)
            assert run.returncode == (0 if name == 'pass' else 1)
            assert report['passed'] == (3 if name == 'pass' else 0)
            assert report['failed'] == (0 if name == 'pass' else 1)
        assert not (work / 'no-write').exists()
        results.append({'fixture': name, 'exit_code': run.returncode, 'report': report,
            'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
            'log_sha256': hashlib.sha256(run.stdout).hexdigest(),
            'started_at': before, 'finished_at': time.time()})
    result = {'schema': 'skfleet.native-test-qualification/v1', 'invocation': invocation,
              'resource_limits_verified': True, 'results': results,
              'limitation': 'Public fixture sandbox and resource qualification. No model, board completion, source bundle materialization or live lifecycle is asserted.'}
    with (ROOT / 'RESULT.json').open('x') as stream:
        json.dump(result, stream, sort_keys=True, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
