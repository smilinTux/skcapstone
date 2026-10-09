"""Remote adapter refusal and exact service-manager receipt tests."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fiber_worker as worker
from fiber_admission import Refused


def request():
    token = 'a' * 32
    job = {'card': '1234abcd', 'owner': 'fiber-test', 'host': 'chiap02',
           'provider': 'deepseek', 'pi_provider': 'skgw-deepseek', 'model': 'deepseek-flash',
           'repository': 'https://example.invalid/approved/repo.git',
           'base_ref': 'refs/heads/main', 'base_revision': 'b' * 40, 'stage': 'implementation'}
    receipt = {'card': job['card'], 'host': job['host'], 'provider': job['provider'],
               'token': token, 'unit': 'skfleet-fiber-1234abcd-' + token + '.service'}
    return {'job': job, 'receipt': receipt}


class WorkerTest(unittest.TestCase):
    def test_actual_worker_resource_budget_is_required(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name, value in {'memory.max': '3221225472', 'memory.swap.max': '536870912',
                                'pids.max': '256', 'cpu.max': '200000 100000'}.items():
                (root / name).write_text(value)
            worker.verify_resource_limits(root)
            (root / 'memory.max').write_text('max')
            with self.assertRaisesRegex(Refused, 'worker-resource-limits'):
                worker.verify_resource_limits(root)

    def test_small_host_rejects_forged_work_class(self):
        item = request()['job']
        item.update(host='chiwk13', stage='tests', work_class='S')
        row = {'title': '[M] substantial test work', 'labels': ['source-only'],
               'meta': {key: item[key] for key in ['repository', 'base_ref', 'base_revision']}}
        item['card_fingerprint'] = worker.card_fingerprint(row)
        with self.assertRaisesRegex(Refused, 'work-class-mismatch'):
            worker.verify_source(row, item)

    def test_authority_and_local_claim_must_both_match(self):
        item = request()
        item['claim'] = {'authority': 'chiap08', 'owner': item['job']['owner'], 'revision': 'a' * 32}
        row = {'owner': item['job']['owner']}
        with patch.object(worker, 'read_claim', return_value=row) as read, patch.object(worker, 'verify_source'):
            self.assertEqual(worker.verify_claim(item), row)
            self.assertEqual(read.call_count, 2)
            for call in read.call_args_list:
                self.assertEqual(call.kwargs['revision'], 'a' * 32)
            read.reset_mock()
            read.side_effect = [row, Refused('claim-owner-or-revision-mismatch')]
            with self.assertRaisesRegex(Refused, 'claim-owner-or-revision-mismatch'):
                worker.verify_claim(item)
            self.assertEqual(read.call_count, 2)
            read.reset_mock()
            read.side_effect = Refused('claim-readback-failed')
            with self.assertRaisesRegex(Refused, 'claim-readback-failed'):
                worker.verify_claim(item)
            self.assertEqual(read.call_count, 1)

    def test_authority_read_uses_only_mediated_cli_with_worker_identity(self):
        item = request()
        item['claim'] = {'authority': 'chiap08', 'owner': 'fiber-test', 'revision': 'a' * 32}
        def read(card, owner, **kwargs):
            if 'runner' in kwargs:
                kwargs['runner'](['skcapstone', 'coord', 'show', card, '--json'], capture_output=True, text=True, timeout=60)
            return {}
        with patch.object(worker, 'read_claim', side_effect=read), patch.object(worker, 'verify_source'), \
             patch.object(worker.subprocess, 'run') as run:
            worker.verify_claim(item)
            argv = run.call_args.args[0]
            self.assertEqual(argv[:6], ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', 'chiap08'])
            self.assertIn('SKAGENT=fiber-test', argv[-1])
            self.assertIn('coord show 1234abcd --json', argv[-1])
            self.assertNotIn('coord claim', argv[-1])

    def test_missing_claim_refuses_before_request_or_materialization(self):
        with tempfile.TemporaryDirectory() as td, patch.object(worker, 'STATE', Path(td)), \
             patch.object(worker.socket, 'gethostname', return_value='chiap02'), \
             patch.object(worker, 'check_catalog'), patch.object(worker, 'read_claim') as read:
            with self.assertRaisesRegex(Refused, 'authority-claim-required'):
                worker.start(request())
            self.assertEqual(list(Path(td).iterdir()), [])
            read.assert_not_called()

    def test_source_metadata_and_links_must_agree(self):
        item = request()['job']
        binding = {key: item[key] for key in ['repository', 'base_ref', 'base_revision']}
        self.assertEqual(worker.source_binding({'links': binding}), tuple(binding.values()))
        self.assertEqual(worker.source_binding({'meta': binding}), tuple(binding.values()))
        self.assertEqual(worker.source_binding({'meta': binding, 'links': binding}), tuple(binding.values()))
        conflict = dict(binding, base_revision='c' * 40)
        with self.assertRaisesRegex(Refused, 'conflicting-source-binding'):
            worker.source_binding({'meta': conflict, 'links': binding})

    def test_binding_and_route_validation(self):
        self.assertEqual(worker.validate(request())[0]['card'], '1234abcd')
        for section, field, value in [('receipt', 'token', '../bad'),
                                      ('receipt', 'host', 'chiap08'),
                                      ('job', 'model', 'kimi-for-coding'),
                                      ('job', 'repository', 'https://secret@example.invalid/repo'),
                                      ('job', 'base_revision', 'main')]:
            item = request()
            item[section][field] = value
            with self.subTest(field=field), self.assertRaises(Refused):
                worker.validate(item)

    def test_start_refuses_missing_catalog_before_request_write(self):
        with tempfile.TemporaryDirectory() as td, patch.object(worker, 'STATE', Path(td)), \
             patch.object(worker.socket, 'gethostname', return_value='chiap02'), \
             patch.object(worker, 'check_catalog', side_effect=Refused('model-not-in-agent-catalog')):
            with self.assertRaisesRegex(Refused, 'model-not-in-agent-catalog'):
                worker.start(request())
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_start_uses_resource_limits_and_refuses_duplicate_request(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, '', '')
        with tempfile.TemporaryDirectory() as td, patch.object(worker, 'STATE', Path(td)), \
             patch.object(worker.socket, 'gethostname', return_value='chiap02'), \
             patch.object(worker, 'check_catalog'), patch.object(worker, 'verify_claim'), \
             patch.object(worker, 'read_unit', return_value={'state': 'running'}), \
             patch.object(worker.subprocess, 'run', side_effect=run):
            worker.start(request())
            with self.assertRaises(FileExistsError):
                worker.start(request())
        self.assertEqual(len(calls), 1)
        for flag in ['--property=CPUQuota=200%', '--property=MemoryMax=3G',
                     '--property=MemorySwapMax=512M', '--property=TasksMax=256',
                     '--property=RuntimeMaxSec=3600', '--property=RemainAfterExit=yes']:
            self.assertIn(flag, calls[0])
        self.assertNotIn('--collect', calls[0])
        self.assertIn('--setenv=PATH=' + str(Path.home() / '.skenv/bin') + ':'
                      + str(Path.home() / '.npm-global/bin')
                      + ':' + str(Path.home() / '.local/bin')
                      + ':/usr/local/bin:/usr/bin:/bin', calls[0])

    def test_service_receipt_requires_exact_description_and_exit(self):
        receipt = request()['receipt']
        values = {'Id': receipt['unit'], 'Description': 'SKFleet fiber 1234abcd ' + receipt['token'],
                  'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'exited',
                  'MainPID': '0', 'InvocationID': 'b' * 32, 'ExecMainCode': '1', 'ExecMainStatus': '0'}
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, '\n'.join(k + '=' + v for k, v in values.items()), '')
        with patch.object(worker.subprocess, 'run', side_effect=run):
            self.assertEqual(worker.read_unit(receipt)['state'], 'terminal')
            values['ExecMainCode'] = '0'
            self.assertEqual(worker.read_unit(receipt)['state'], 'unknown')
            values['Description'] = 'unrelated service'
            self.assertEqual(worker.read_unit(receipt)['state'], 'unknown')

    def test_source_and_policy_refusal_precede_materialization(self):
        item = request()
        row = {'labels': ['source-only'], 'links': {'repository': item['job']['repository'],
               'base_ref': item['job']['base_ref'], 'base_revision': 'c' * 40}, 'meta': {}}
        item['job']['card_fingerprint'] = worker.card_fingerprint(row)
        with patch.object(worker.socket, 'gethostname', return_value='chiap02'), \
             patch.object(worker, 'check_catalog'), patch.object(worker, 'verify_claim', side_effect=Refused('source-binding-changed')), \
             patch.object(worker.subprocess, 'run') as run:
            with self.assertRaisesRegex(Refused, 'source-binding-changed'):
                worker.execute(item)
            run.assert_not_called()
        row['labels'].append('no-egress')
        item['job']['card_fingerprint'] = worker.card_fingerprint(row)
        with self.assertRaisesRegex(Refused, 'source-lane-policy'):
            worker.verify_source(row, item['job'])


if __name__ == '__main__':
    unittest.main()
