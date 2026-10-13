"""Controller integration checks without production mutations or model calls."""
from pathlib import Path
import sys
import tempfile
import unittest
import json
import subprocess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fiber_control as control
from fiber_admission import Refused, card_fingerprint
from test_fiber_admission import job, snapshot


class ControlTest(unittest.TestCase):
    def test_board_reads_only_requested_cards_and_direct_dependencies(self):
        rows = {'1234abcd': {'id': '1234abcd', 'owner': None, 'dependencies': ['1234abce']},
                '1234abce': {'id': '1234abce', 'owner': None, 'status': 'done'}}
        def run(argv, **kwargs):
            self.assertEqual(argv[:3], ['skcapstone', 'coord', 'show'])
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows[argv[3]]), '')
        with patch.object(control.subprocess, 'run', side_effect=run) as read:
            self.assertEqual(set(control.cards(['1234abcd'])), set(rows))
            self.assertEqual(read.call_count, 2)

    def test_review_description_requires_independence_and_qualified_identity(self):
        row = {'id': '1234abcd', 'title': '[REVIEW][M] audit', 'owner': None}
        with patch.object(control, 'cards', return_value={'1234abcd': row}), \
             patch.object(control, 'source_binding', return_value=('https://example.invalid/repo', 'main', 'b' * 40)):
            with self.assertRaisesRegex(Refused, 'review-independence'):
                control.describe('1234abcd', 'review', 'deepseek')
            task = control.describe('1234abcd', 'review', 'deepseek', producer_family='codex')
            self.assertEqual(task['owner'], 'pi-seraph-fiber-1234abcd')
            self.assertEqual(task['producer_provider'], 'codex')
            self.assertEqual(task['host'], 'auto')
            self.assertEqual(task['work_class'], 'M')

    def test_automatic_placement_skips_unqualified_runtime_without_model_probe(self):
        truth = snapshot()
        truth['hosts']['chiwk12'] = {'ready': True, 'slots': 2}
        task = dict(job(), host='auto', model='deepseek-flash')
        def remote(mode, request):
            chosen = request['job']
            if chosen['host'] == 'chiap02':
                raise Refused('remote-check-failed')
            return {'state': 'ready', 'host': chosen['host'], 'card': chosen['card'], 'model': chosen['model']}
        with tempfile.TemporaryDirectory() as td, patch.object(control, 'STATE', Path(td)), \
             patch.object(control.time, 'time', return_value=1000), \
             patch.object(control, 'collect', side_effect=[truth, snapshot() | {'hosts': truth['hosts']}]), \
             patch.object(control, 'remote', side_effect=remote), \
             patch.object(control, 'qualify_route', return_value={'ready': True, 'cap': 3}) as probe:
            chosen, fresh = control.prepare_job(task)
            self.assertEqual(chosen['host'], 'chiwk12')
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(task['host'], 'auto')

    def test_served_model_must_match_exact_routed_member(self):
        task = {'provider': 'zai', 'model': 'sk-zai-m'}
        headers = {'x-sk-provider': 'zai', 'x-sk-model-requested': 'sk-zai-m',
                   'x-sk-bucket-member': 'glm-4.6', 'x-sk-model-served': 'glm-5.3-flash'}
        answer = {'model': 'glm-5.3-flash', 'choices': [{'message': {'content': 'OK'}}]}
        with self.assertRaisesRegex(Refused, 'completion-model-substitution'):
            control.validate_completion(task, answer, headers)
        answer['model'] = headers['x-sk-model-served'] = 'glm-4.6'
        self.assertEqual(control.validate_completion(task, answer, headers), 'glm-4.6')
        del headers['x-sk-bucket-member']
        with self.assertRaises(Refused):
            control.validate_completion(task, answer, headers)

    def test_pinned_completion_requires_exact_provider_and_model(self):
        task = {'provider': 'deepseek', 'model': 'deepseek-flash'}
        headers = {'x-sk-provider': 'deepseek', 'x-sk-model-requested': 'deepseek-flash',
                   'x-sk-model-served': 'deepseek-flash'}
        answer = {'model': 'deepseek-flash', 'choices': [{'message': {'content': 'OK'}}]}
        self.assertEqual(control.validate_completion(task, answer, headers), 'deepseek-flash')
        backend_headers = dict(headers, **{'x-sk-backend': 'deepseek'})
        del backend_headers['x-sk-provider']
        self.assertEqual(control.validate_completion(task, answer, backend_headers), 'deepseek-flash')
        for field in ['x-sk-provider', 'x-sk-model-requested', 'x-sk-model-served']:
            bad = dict(headers, **{field: 'other'})
            with self.subTest(field=field), self.assertRaises(Refused):
                control.validate_completion(task, answer, bad)

    def test_authority_claim_precedes_remote_launch_and_binds_revision(self):
        task = dict(job(), owner='fiber-test')
        row = {'owner': 'fiber-test', 'meta': {'_claim_revision': 'a' * 32}}
        with patch.object(control.socket, 'gethostname', return_value='chiap08'), \
             patch.object(control, 'claim_for_worker', return_value=row) as claim, \
             patch.object(control, 'verify_source'), patch.object(control, 'remote', return_value={'state': 'running'}) as remote:
            control.launch_claimed(task, {'token': 'b' * 32})
            claim.assert_called_once_with(task['card'], task['owner'])
            self.assertEqual(remote.call_args.args[1]['claim'],
                             {'authority': 'chiap08', 'owner': 'fiber-test', 'revision': 'a' * 32})
            claim.side_effect = Refused('claim-refused')
            remote.reset_mock()
            with self.assertRaisesRegex(Refused, 'claim-refused'):
                control.launch_claimed(task, {'token': 'c' * 32})
            remote.assert_not_called()

    def test_full_estate_does_not_probe_models(self):
        truth = snapshot()
        truth['workers'] = [{'key': str(i)} for i in range(12)]
        with patch.object(control, 'collect', return_value=truth), patch.object(control, 'qualify_route') as probe:
            with self.assertRaisesRegex(Refused, 'estate-capacity'):
                control.observation(job())
            probe.assert_not_called()

    def test_recollect_after_model_probe(self):
        before, after = snapshot(1000), snapshot(1020)
        task = dict(job(), model='deepseek-flash')
        with patch.object(control, 'collect', side_effect=[before, after]) as collect, \
             patch.object(control, 'remote', return_value={'state': 'ready', 'card': task['card'], 'host': task['host'], 'model': task['model']}), \
             patch.object(control, 'qualify_route', return_value={'ready': True, 'cap': 3}):
            value = control.observation(task)
            self.assertEqual(value['observed_at'], 1020)
            self.assertEqual(collect.call_count, 2)

    def test_owned_or_missing_dependency_never_admitted(self):
        self.assertFalse(control.eligible({'owner': 'another', 'status': 'doing'}, {}))
        self.assertFalse(control.eligible({'status': 'ready', 'dependencies': ['missing']}, {}))

    def test_task_fingerprint_ignores_claim_not_task_changes(self):
        row = {'id': '1234abcd', 'title': 'test', 'description': 'bounded', 'acceptance_criteria': ['pass'],
               'labels': ['source-only'], 'dependencies': [], 'meta': {}, 'links': {}}
        original = card_fingerprint(row)
        row.update(owner='fiber-test', status='doing')
        row['meta']['_claim_revision'] = 'a' * 32
        row['links']['evidence'] = 'new-evidence'
        self.assertEqual(card_fingerprint(row), original)
        row['acceptance_criteria'].append('different work')
        self.assertNotEqual(card_fingerprint(row), original)

    def test_wrong_host_refuses_before_observation(self):
        with patch.object(control.socket, 'gethostname', return_value='chiap02'), patch.object(control, 'collect') as collect:
            with self.assertRaisesRegex(Refused, 'not-authority-host'):
                control.cycle(check=True)
            collect.assert_not_called()

    def test_empty_queue_does_not_start_or_claim(self):
        with tempfile.TemporaryDirectory() as td, patch.object(control, 'STATE', Path(td) / 'state'), \
             patch.object(control, 'QUEUE', Path(td) / 'queue'), \
             patch.object(control, 'HOLD', Path(td) / 'HOLD'), \
             patch.object(control.socket, 'gethostname', return_value='chiap08'), \
             patch.object(control, 'remote') as remote, patch.object(control, 'cards') as cards:
            self.assertEqual(control.cycle()['state'], 'waiting-for-reviewed-requests')
            remote.assert_not_called()
            cards.assert_not_called()

    def test_hold_is_idempotent_and_preserves_workers(self):
        with tempfile.TemporaryDirectory() as td, patch.object(control, 'STATE', Path(td) / 'state'), \
             patch.object(control, 'HOLD', Path(td) / 'HOLD'), \
             patch.object(control.socket, 'gethostname', return_value='chiap08'), \
             patch.object(control, 'remote') as remote, patch.object(control, 'cards') as cards:
            self.assertEqual(control.set_hold()['workers_stopped'], 0)
            self.assertEqual(control.set_hold()['state'], 'admission-held')
            self.assertEqual(control.cycle()['state'], 'admission-held')
            remote.assert_not_called()
            cards.assert_not_called()


if __name__ == '__main__':
    unittest.main()
