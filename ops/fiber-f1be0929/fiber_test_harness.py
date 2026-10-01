"""Observe real Python framework results inside the private test sandbox.

This read-only adapter is not a receipt issuer. The trusted outer test plane
checks the actual exit, exact service invocation, source and plan separately.
"""
import json
import os
from pathlib import Path
import sys
import unittest


class UnitResult(unittest.TextTestResult):
    """Count successful methods and retain exact skipped test identifiers."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = 0

    def addSuccess(self, test):
        self.passed += 1
        super().addSuccess(test)


class PytestResult:
    """Observe framework reports; printed summaries carry no authority."""
    def __init__(self):
        self.passed = set()
        self.failed = set()
        self.errors = set()
        self.skipped = set()

    def pytest_collectreport(self, report):
        if report.failed:
            self.errors.add(report.nodeid)

    def pytest_runtest_logreport(self, report):
        if report.failed:
            (self.failed if report.when == 'call' else self.errors).add(report.nodeid)
        elif report.skipped:
            self.skipped.add(report.nodeid)
        elif report.passed and report.when == 'call':
            self.passed.add(report.nodeid)


def run_framework(argv):
    """Run the qualified Python module with only reporting instrumentation."""
    if not isinstance(argv, list) or len(argv) < 3 or argv[1] != '-m':
        raise ValueError('unsupported framework invocation')
    if argv[2] == 'unittest':
        runner = unittest.TextTestRunner(verbosity=2, resultclass=UnitResult)
        program = unittest.main(module=None, argv=list(argv[2:]), exit=False, testRunner=runner)
        result = program.result
        # Subtest failures name individual subtests, not independent methods.
        method_id = lambda test: getattr(test, 'test_case', test).id()
        failed = {method_id(test) for test, _ in result.failures}
        errors = {method_id(test) for test, _ in result.errors}
        # Unexpected successes are failures under unittest's success contract.
        failed.update(method_id(test) for test in result.unexpectedSuccesses)
        skipped = {test.id() for test, _ in result.skipped}
        skipped.update(test.id() for test, _ in result.expectedFailures)
        values = {'passed': result.passed, 'failed': len(failed),
                  'errors': len(errors), 'skipped': sorted(skipped)}
        return 0 if result.wasSuccessful() else 1, values
    if argv[2] == 'pytest':
        import pytest
        observer = PytestResult()
        code = int(pytest.main(list(argv[3:]), plugins=[observer]))
        passed = observer.passed - observer.failed - observer.errors - observer.skipped
        return code, {'passed': len(passed), 'failed': len(observer.failed),
                      'errors': len(observer.errors), 'skipped': sorted(observer.skipped)}
    raise ValueError('unsupported framework invocation')


def main():
    """Write a fresh bounded framework report only after execution returns."""
    if len(sys.argv) != 2:
        raise ValueError('one private output path required')
    path = Path(sys.argv[1])
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        raise ValueError('request too large')
    argv = json.loads(raw)
    # Match `python -m` import semantics although this adapter is a script.
    sys.path.insert(0, os.getcwd())
    code, counts = run_framework(argv)
    report = {'schema': 'skfleet.framework-report/v1', 'argv': argv,
              'exit_code': code, **counts}
    data = json.dumps(report, sort_keys=True).encode()
    if len(data) > 65536:
        raise ValueError('framework report too large')
    # A test precreating this path cannot substitute a fabricated report.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return code


if __name__ == '__main__':
    raise SystemExit(main())
