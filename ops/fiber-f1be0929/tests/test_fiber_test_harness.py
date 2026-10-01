"""Real disposable framework runs, without providers or native board writes."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HARNESS = Path(__file__).resolve().parents[1] / 'fiber_test_harness.py'


class HarnessTest(unittest.TestCase):
    def execute(self, code, framework='unittest', precreate=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        files = {'test_fixture.py': code} if isinstance(code, str) else code
        for name, source in files.items():
            (root / name).write_text(source)
        # Do not inherit an unrelated ancestor's pytest configuration in /tmp.
        (root / 'pytest.ini').write_text('[pytest]\n')
        output = root / 'report.json'
        if precreate:
            output.write_text('{"passed":999}')
        argv = ['python3', '-m', framework, 'discover'] if framework == 'unittest' else ['python3', '-m', 'pytest', '-q', *sorted(files)]
        run = subprocess.run([sys.executable, str(HARNESS), str(output)], input=json.dumps(argv),
                             capture_output=True, text=True, cwd=root, timeout=20)
        return run, json.loads(output.read_text()) if output.exists() else None

    def test_real_unittest_success_ignores_printed_fake_counts(self):
        run, report = self.execute('import unittest\nclass T(unittest.TestCase):\n def test_ok(self):\n  print("999 passed")\n  self.assertTrue(True)\n')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(report['passed'], 1)
        self.assertEqual(report['failed'], 0)
        self.assertEqual(report['skipped'], [])

    def test_subtest_failures_are_one_failed_method(self):
        code = 'import unittest\nclass T(unittest.TestCase):\n def test_bad(self):\n  for i in range(3):\n   with self.subTest(i=i): self.fail("bad")\n def test_ok(self): pass\n'
        run, report = self.execute(code)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(report['passed'], 1)
        self.assertEqual(report['failed'], 1)

    def test_zero_exit_without_framework_report_is_not_success_evidence(self):
        run, report = self.execute('import os\nos._exit(0)\n')
        self.assertEqual(run.returncode, 0)
        self.assertIsNone(report)

    def test_forged_precreated_report_causes_failure(self):
        run, report = self.execute('import unittest\nclass T(unittest.TestCase):\n def test_ok(self): pass\n', precreate=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(report, {'passed': 999})

    def test_skip_and_expected_failure_are_explicit_ids(self):
        code = 'import unittest\nclass T(unittest.TestCase):\n @unittest.skip("fixture")\n def test_skip(self): pass\n @unittest.expectedFailure\n def test_expected(self): self.fail("expected")\n'
        run, report = self.execute(code)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(report['passed'], 0)
        self.assertEqual(report['skipped'], ['test_fixture.T.test_expected', 'test_fixture.T.test_skip'])

    def test_real_pytest_success_failure_and_skip(self):
        try:
            import pytest
        except ImportError:
            self.skipTest('pytest unavailable in this test interpreter')
        code = 'import pytest\ndef test_ok(): pass\ndef test_bad(): assert False\n@pytest.mark.skip(reason="fixture")\ndef test_skip(): pass\n'
        run, report = self.execute(code, 'pytest')
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertEqual(report['passed'], 1)
        self.assertEqual(report['failed'], 1)
        self.assertEqual(report['skipped'], ['test_fixture.py::test_skip'])

    def test_pytest_module_collection_skip_remains_explicit(self):
        files = {'test_fixture.py': 'def test_ok(): pass\n',
                 'test_optional.py': 'import pytest\npytest.skip("optional fixture", allow_module_level=True)\n'}
        run, report = self.execute(files, 'pytest')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(report['passed'], 1)
        self.assertEqual(report['skipped'], ['test_optional.py'])


if __name__ == '__main__':
    unittest.main()
