"""Run disposable public source in the actual private root, without a model."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fiber_test_worker import sandbox_command, framework_report


@unittest.skipUnless(Path('/usr/bin/bwrap').exists() and shutil.which('node'), 'native private-root prerequisites absent')
class TestNativeSandbox(unittest.TestCase):
    def execute(self, source):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        work, control, output = (root / name for name in ('work', 'control', 'output'))
        for path in (work, control, output): path.mkdir(mode=0o700)
        (work / 'test_fixture.py').write_text(source)
        argv = ['python3', '-m', 'unittest', 'discover']
        (control / 'argv.json').write_text(json.dumps(argv))
        run = subprocess.run(sandbox_command(work, control, output, argv, None),
            capture_output=True, text=True, timeout=15, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        return run, output / 'report.json', argv, work

    def test_real_tests_readonly_source_and_denied_external_network(self):
        source = '''import unittest, socket
from pathlib import Path
class T(unittest.TestCase):
 def test_source_readonly(self):
  with self.assertRaises(OSError): Path('/work/forbidden').write_text('no')
 def test_external_network_denied(self):
  with socket.socket() as s:
   s.settimeout(1)
   with self.assertRaises(OSError): s.connect(('192.0.2.1', 80))
 def test_private_output_writable(self):
  Path('/test-output/fixture').write_text('yes')
'''
        run, path, argv, work = self.execute(source)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(framework_report(path, argv, run.returncode)['passed'], 3)
        self.assertFalse((work / 'forbidden').exists())

    def test_real_failure_is_reported(self):
        run, path, argv, _ = self.execute('import unittest\nclass T(unittest.TestCase):\n def test_bad(self): self.fail("fixture failure")\n')
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertEqual(framework_report(path, argv, run.returncode)['failed'], 1)

    def test_early_zero_exit_has_no_report(self):
        run, path, argv, _ = self.execute('import os\nos._exit(0)\n')
        self.assertEqual(run.returncode, 0, run.stderr)
        with self.assertRaises(OSError): framework_report(path, argv, run.returncode)


if __name__ == '__main__':
    unittest.main()
