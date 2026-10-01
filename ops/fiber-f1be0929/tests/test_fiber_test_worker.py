"""Service identity and framework evidence, without fleet or native mutations."""
import copy
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
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
    def test_actual_framework_runs_overlap_without_reexecuting_exact_request(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            legacy = state / 'tests'; legacy.mkdir(mode=0o700)
            legacy_lock = os.open(legacy / 'execution.lock', os.O_CREAT | os.O_RDWR, 0o600)
            self.addCleanup(os.close, legacy_lock)
            fcntl.flock(legacy_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            source = state / 'source'; source.mkdir(mode=0o700)
            (source / 'test_fixture.py').write_text(
                'import unittest\nclass Proof(unittest.TestCase):\n'
                '    def test_sum(self): self.assertEqual(2 + 3, 5)\n')
            requests = [request_fixture(), request_fixture()]
            for index, request in enumerate(requests):
                card = '1234abcd' if index == 0 else '1234abce'
                request['producer']['job']['card'] = card
                request['requirements']['source_card'] = card
                request['requirements']['checks'][0]['argv'] += ['-s', str(source)]
                request['plan'] = make_plan(request['requirements'], request['producer'], 1000)
                request['token'] = str(index + 1) * 32
                request['unit'] = 'skfleet-test-' + card + '-' + request['token'] + '.service'
            barrier = threading.Barrier(2)
            materialized = []
            def materialize(request):
                materialized.append(request['token'])
                barrier.wait(timeout=5)
                return source, None
            harness = str(Path(worker.__file__).with_name('fiber_test_harness.py'))
            def command(workspace, directory, output, argv, environment):
                program = ('import io,json,runpy,sys; '
                    'sys.stdin=io.TextIOWrapper(io.BytesIO(sys.argv[3].encode())); '
                    'harness=sys.argv[1]; sys.argv=[harness,sys.argv[2]]; '
                    'runpy.run_path(harness,run_name="__main__")')
                return [sys.executable, '-c', program, harness,
                        str(output / 'report.json'), json.dumps(argv)]
            with patch.object(worker, 'STATE', state), \
                 patch.object(worker.time, 'time', return_value=1000), \
                 patch.object(worker.socket, 'gethostname', return_value='chiap04'), \
                 patch.dict(os.environ, {'INVOCATION_ID': '9' * 32}), \
                 patch.object(worker, 'verify_resource_limits'), \
                 patch.object(worker, 'authority_check'), \
                 patch.object(worker, 'materialize', side_effect=materialize), \
                 patch.object(worker, 'sandbox_command', side_effect=command):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    self.assertEqual(list(pool.map(worker.execute, requests)), [0, 0])
                self.assertEqual(len(materialized), 2)
                for request in requests:
                    result = json.loads((worker.paths(request) / 'result.json').read_text())
                    self.assertEqual(result['request_sha256'], digest(request))
                    self.assertEqual(result['checks'][0]['passed'], 1)
                    with self.assertRaisesRegex(Refused, 'test-execution-already-started'):
                        worker.execute(request)
                self.assertEqual(len(materialized), 2)

    def test_exact_execution_lock_blocks_duplicate_before_materialization(self):
        request = request_fixture()
        with tempfile.TemporaryDirectory() as td, \
             patch.object(worker, 'STATE', Path(td)), \
             patch.object(worker.time, 'time', return_value=1000), \
             patch.object(worker.socket, 'gethostname', return_value='chiap04'), \
             patch.dict(os.environ, {'INVOCATION_ID': '9' * 32}), \
             patch.object(worker, 'verify_resource_limits'), \
             patch.object(worker, 'authority_check'), \
             patch.object(worker, 'materialize') as materialize:
            with (worker.paths(request) / 'execution.lock').open('w') as locked:
                fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(Refused, 'test-request-executing'):
                    worker.execute(request)
            materialize.assert_not_called()

    def test_preflight_does_not_apply_host_cpu_memory_or_load_caps(self):
        request = request_fixture()
        with patch.object(worker.time, 'time', return_value=1000), \
             patch.object(worker.socket, 'gethostname', return_value='chiap04'), \
             patch('skcapstone.fleet.capacity.admit_headroom', return_value=(False, 'unsafe-memory', {})) as memory, \
             patch.object(worker.os, 'cpu_count', return_value=1) as cpus, \
             patch.object(worker.os, 'getloadavg', return_value=(999, 999, 999)) as load:
            self.assertEqual(worker.check(request), {'request_sha256': digest(request), 'state': 'ready'})
            memory.assert_not_called(); cpus.assert_not_called(); load.assert_not_called()

    def test_start_retains_service_resource_limits_without_host_admission_caps(self):
        request = request_fixture()
        with tempfile.TemporaryDirectory() as td, \
             patch.object(worker, 'STATE', Path(td)), \
             patch.object(worker.time, 'time', return_value=1000), \
             patch.object(worker.socket, 'gethostname', return_value='chiap04'), \
             patch.object(worker, 'authority_check'), \
             patch.object(worker, 'observe', return_value={'state': 'running'}), \
             patch.object(worker.subprocess, 'run') as launch:
            launch.return_value.returncode = 0
            self.assertEqual(worker.start(request), {'state': 'running'})
            command = launch.call_args.args[0]
            for flag in ['--property=CPUQuota=200%', '--property=MemoryMax=3G',
                         '--property=MemorySwapMax=512M', '--property=TasksMax=256',
                         '--property=RuntimeMaxSec=1200', '--property=NoNewPrivileges=yes']:
                self.assertIn(flag, command)
            self.assertEqual(worker.start(request), {'state': 'running'})
            self.assertEqual(launch.call_count, 1)

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
