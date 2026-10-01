"""Service identity and framework evidence, without fleet or native mutations."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fiber_admission import Refused
from fiber_refill import digest
import fiber_test_worker as worker
from fiber_test_plane import test_plan as make_plan


def request_fixture(now=1000):
    producer = {'job': {'card': '1234abcd', 'card_fingerprint': 'a' * 64,
                       'repository': 'https://example.invalid/repo.git', 'base_revision': 'c' * 40,
                       'owner': 'fixture-worker'},
                'claim_revision': 'd' * 32,
                'candidate': {'commit': 'b' * 40, 'tree': 'e' * 40,
                              'bundle_sha256': 'f' * 64, 'ref': 'refs/heads/work/fiber-1234abcd-11111111'}}
    requirements = {'schema': 'skfleet.test-requirements/v1', 'source_card': '1234abcd',
        'source_fingerprint': 'a' * 64, 'repository': producer['job']['repository'],
        'qualified_at': now - 10, 'expires_at': now + 100, 'evidence_sha256': 'e' * 64,
        'host': 'chiap04', 'test_environment': None,
        'checks': [{'id': 'unit', 'argv': ['python3', '-m', 'unittest', 'discover'],
                    'minimum_passed': 1, 'allowed_skips': []}]}
    return {'schema': 'skfleet.test-execution/v1', 'producer': producer,
        'source_revision': 'c' * 64, 'review_sha256': 'd' * 64,
        'requirements': requirements, 'plan': make_plan(requirements, producer, now),
        'token': '1' * 32, 'unit': 'skfleet-test-1234abcd-' + '1' * 32 + '.service'}


class TestWorker(unittest.TestCase):
    def test_headroom_reserves_actual_service_memory_and_cpu(self):
        for available, cpus, load, ok in [(4 * 1024 * 1024, 4, 0, True),
                                          (4 * 1024 * 1024 - 1, 4, 0, False),
                                          (8 * 1024 * 1024, 1, 0, False),
                                          (8 * 1024 * 1024, 4, 3, False)]:
            with self.subTest(available=available, cpus=cpus, load=load), patch('skcapstone.fleet.capacity.admit_headroom', return_value=(True, 'ok', {'MemAvailable': available})), patch.object(worker.os, 'cpu_count', return_value=cpus), patch.object(worker.os, 'getloadavg', return_value=(load, 0, 0)):
                if ok: worker.headroom()
                else:
                    with self.assertRaises(Refused): worker.headroom()

    def test_request_changes_and_expiry_refuse(self):
        request = request_fixture()
        self.assertEqual(worker.validate(request, 1000)['base_revision'], 'b' * 40)
        for key, value in [('unit', 'arbitrary.service'), ('source_revision', 'bad'),
                           ('review_sha256', 'bad'), ('token', '../escape'), ('extra', True)]:
            changed = copy.deepcopy(request); changed[key] = value
            with self.subTest(key=key), self.assertRaises(Refused):
                worker.validate(changed, 1000)
        with self.assertRaises(Refused): worker.validate(request, 1100)

    def test_unit_requires_exact_description_and_normal_exit(self):
        request = request_fixture()
        values = {'Id': request['unit'], 'LoadState': 'loaded',
            'Description': 'SKFleet test ' + digest(request), 'InvocationID': '2' * 32,
            'MainPID': '0', 'ExecMainCode': '1', 'ExecMainStatus': '0',
            'ActiveState': 'active', 'SubState': 'exited'}
        with patch.object(worker.time, 'time', return_value=1000), patch.object(worker.socket, 'gethostname', return_value='chiap04'):
            with patch.object(worker, 'unit_values', return_value=(0, values)):
                self.assertEqual(worker.observe(request)['state'], 'terminal')
            for key, value in [('Description', 'other'), ('InvocationID', ''), ('ExecMainCode', '2'),
                               ('LoadState', 'not-found')]:
                with self.subTest(key=key), patch.object(worker, 'unit_values', return_value=(0, dict(values, **{key: value}))):
                    self.assertEqual(worker.observe(request)['state'], 'unknown')

    def test_existing_request_never_relaunches_missing_unit(self):
        request = request_fixture()
        with tempfile.TemporaryDirectory() as td:
            state = Path(td); directory = state / 'tests' / request['token']; directory.mkdir(parents=True)
            directory.parent.chmod(0o700); directory.chmod(0o700)
            (directory / 'request.json').write_text(json.dumps(request))
            (directory / 'request.json').chmod(0o600)
            with patch.object(worker, 'STATE', state), patch.object(worker.time, 'time', return_value=1000), patch.object(worker.socket, 'gethostname', return_value='chiap04'), patch.object(worker, 'observe', return_value={'state': 'unknown'}), patch.object(worker.subprocess, 'run') as launch:
                self.assertEqual(worker.start(request), {'state': 'unknown'})
                launch.assert_not_called()

    def test_framework_missing_mismatch_symlink_and_boolean_counts_refuse(self):
        argv = ['python3', '-m', 'unittest', 'discover']
        good = {'schema': 'skfleet.framework-report/v1', 'argv': argv,
                'exit_code': 0, 'passed': 1, 'failed': 0, 'errors': 0, 'skipped': []}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'report.json'
            with self.assertRaises(OSError): worker.framework_report(path, argv, 0)
            path.write_text(json.dumps(good))
            path.chmod(0o600)
            self.assertEqual(worker.framework_report(path, argv, 0), good)
            with self.assertRaises(Refused): worker.framework_report(path, argv, 1)
            for key, value in [('passed', True), ('argv', ['false']), ('skipped', ['same', 'same']), ('extra', 1)]:
                path.write_text(json.dumps(dict(good, **{key: value})))
                with self.subTest(key=key), self.assertRaises(Refused): worker.framework_report(path, argv, 0)
            alias = Path(td) / 'alias'; alias.symlink_to(path)
            with self.assertRaises(OSError): worker.framework_report(alias, argv, 0)

    def test_sandbox_source_readonly_no_network_and_fixed_harness(self):
        with patch.object(worker.fiber_sandbox, 'node_executable', return_value=Path('/usr/bin/node')):
            argv = worker.sandbox_command(Path('/work-source'), Path('/control'), Path('/output'),
                                          ['python3', '-m', 'unittest', 'discover'], None)
        self.assertIn('--unshare-all', argv)
        self.assertIn('--clearenv', argv)
        self.assertNotIn('--share-net', argv)
        self.assertTrue(any(argv[i:i+2] == ['--remount-ro', '/work'] for i in range(len(argv))))
        self.assertEqual(argv[-3:], ['/usr/bin/python3', '/opt/test-harness.py', '/test-output/report.json'])
        self.assertFalse(any('gateway.sock' in s for s in argv))


if __name__ == '__main__':
    unittest.main()
