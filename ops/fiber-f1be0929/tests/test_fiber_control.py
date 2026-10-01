"""Controller integration checks without production mutations or model calls."""
from pathlib import Path
import sys
import tempfile
import unittest
import json
import subprocess
import io
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fiber_control as control
from fiber_admission import Refused, card_fingerprint
from test_fiber_admission import job, snapshot


class ControlTest(unittest.TestCase):
    def test_one_cycle_launches_each_distinct_eligible_queued_job(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); queue = root / 'jobs'; queue.mkdir(mode=0o700)
            for cid in ('1234abcd', '1234abce'):
                (queue / (cid + '.json')).write_text(json.dumps(job(cid)))
            rows = {cid: {'id': cid} for cid in ('1234abcd', '1234abce')}
            with patch.object(control, 'STATE', root / 'state'), patch.object(control, 'QUEUE', queue), \
                 patch.object(control, 'HOLD', root / 'HOLD'), \
                 patch.object(control.socket, 'gethostname', return_value='chiap08'), \
                 patch.object(control, 'cards', return_value=rows), \
                 patch.object(control, 'eligible', return_value=True), \
                 patch.object(control, 'validate'), patch.object(control, 'verify_source'), \
                 patch.object(control, 'dispatch_one', return_value={'state': 'running', 'new_launch': True}) as launch:
                result = control.cycle()
            self.assertEqual(len(result['jobs']), 2)
            self.assertEqual([call.args[1]['card'] for call in launch.call_args_list], ['1234abcd', '1234abce'])

    def test_review_gate_refreshes_actual_gateway_truth_and_claim_forces_refresh(self):
        with patch.object(control, '_review_routes_at', None), \
             patch.object(control.time, 'time', return_value=1000), \
             patch.object(control.socket, 'gethostname', return_value='chiap08'), \
             patch('skcapstone.fleet.review_capacity.acquire_review_route_snapshot', return_value={'error': None}) as acquire:
            control.refresh_review_routes()
            control.refresh_review_routes()
            self.assertEqual(acquire.call_count, 1)
            control.refresh_review_routes(force=True)
            self.assertEqual(acquire.call_count, 2)
            acquire.return_value = {'error': 'probe failed'}
            with self.assertRaisesRegex(Refused, 'review-gateway-observation-unavailable'):
                control.refresh_review_routes(force=True)

    def test_route_probe_reuse_requires_fresh_health_same_revision_and_age(self):
        class Response(io.BytesIO):
            headers = {'x-sk-provider': 'deepseek', 'x-sk-model-requested': 'deepseek-flash',
                       'x-sk-model-served': 'deepseek-flash'}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); catalog = root / '.pi/agent/models.json'
            catalog.parent.mkdir(parents=True)
            catalog.write_text(json.dumps({'providers': {'skgw-deepseek': {
                'baseUrl': control.GATEWAY, 'models': [{'id': 'deepseek-flash'}],
                'apiKey': 'fixture-key'}}}))
            (root / 'observations').mkdir()
            truth = {'runtime_revision': 'first'}
            payload = json.dumps({'model': 'deepseek-flash', 'choices': [{'message': {'content': 'OK'}}]}).encode()
            with patch.object(control.Path, 'home', return_value=root), \
                 patch.object(control, 'STATE', root), patch.object(control, '_route_proofs', {}), \
                 patch.object(control.time, 'time', return_value=1000) as clock, \
                 patch('skcapstone.fleet_lane_health.acquire_lane_snapshot', return_value=truth), \
                 patch('skcapstone.fleet_lane_health.lane_health', return_value=(True, 'ready')) as health, \
                 patch.object(control.urllib.request, 'urlopen', side_effect=lambda *a, **kw: Response(payload)) as request:
                task = dict(job(), model='deepseek-flash', pi_provider='skgw-deepseek')
                control.qualify_route(task); control.qualify_route(task)
                self.assertEqual(request.call_count, 1)
                self.assertEqual(health.call_count, 2)
                truth['runtime_revision'] = 'changed'
                control.qualify_route(task)
                self.assertEqual(request.call_count, 2)
                clock.return_value = 1061
                control.qualify_route(task)
                self.assertEqual(request.call_count, 3)
                health.return_value = (False, 'unavailable')
                with self.assertRaisesRegex(Refused, 'lane-health:unavailable'):
                    control.qualify_route(task)
                self.assertEqual(request.call_count, 3)

    def test_describe_review_binds_candidate_and_worker_rechecks_same_source(self):
        row = {'id': '1234abcd', 'title': '[S][REVIEW] fixture', 'labels': ['source-only'],
               'meta': {'repository': 'https://example.invalid/repo.git', 'base_ref': 'main',
                        'base_revision': 'a' * 40, 'link_head_revision': 'b' * 40,
                        'link_source_card': 'abcdef12', 'producer_identity': 'pi-producer',
                        'candidate_evidence_sha256': 'c' * 64}}
        with patch.object(control, 'cards', return_value={'1234abcd': row}):
            task = control.describe('1234abcd', 'review', 'zai', producer_family='deepseek')
        self.assertEqual(task['base_revision'], 'b' * 40)
        control.verify_source(row, task)
        row['meta']['link_head_revision'] = 'd' * 40
        with self.assertRaisesRegex(Refused, 'task-contract-changed'):
            control.verify_source(row, task)
        for variant in ('missing', 'conflicting'):
            row['meta']['link_head_revision'] = 'b' * 40
            row['links'] = {}
            if variant == 'missing':
                del row['meta']['link_head_revision']
            else:
                row['links']['link_head_revision'] = 'c' * 40
            with patch.object(control, 'cards', return_value={'1234abcd': row}), \
                 self.subTest(variant=variant), self.assertRaises(Refused):
                control.describe('1234abcd', 'review', 'zai', producer_family='deepseek')

    def test_chiwk12_execution_uses_the_migrated_worker_account(self):
        request = {'job': dict(job(), host='chiwk12')}
        with patch.object(control.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{}', '')) as run:
            control.remote('check', request)
        argv = run.call_args.args[0]
        self.assertIn('skuser01@chiwk12', argv)
        self.assertIn('/home/skuser01/.skenv/bin/python', argv[-1])
        self.assertNotIn('mrarch', ' '.join(argv))

    def test_observe_forwards_only_recorded_invocation_for_recovery(self):
        run = {'binding': json.dumps(job()), 'card': '1234abcd', 'host': 'chiap02',
               'token': 'a' * 32, 'unit': 'synthetic.service', 'invocation': 'b' * 32}
        with patch.object(control, 'remote', return_value={'state': 'unknown'}) as remote:
            control.observe(run)
        self.assertEqual(remote.call_args.args[1]['receipt']['invocation'], 'b' * 32)

    def test_stopped_unknown_exit_does_not_stop_unrelated_admission(self):
        with tempfile.TemporaryDirectory() as td, patch.object(control, 'STATE', Path(td)), \
             patch.object(control, 'HOLD', Path(td) / 'HOLD'), \
             patch.object(control, 'QUEUE', Path(td) / 'jobs'), \
             patch.object(control.socket, 'gethostname', return_value='chiap08'), \
             patch.object(control, 'reconcile', return_value=[{'card': '1234abcd', 'state': 'terminal', 'exit_code': None}]), \
             patch.object(control, 'cards') as cards:
            self.assertEqual(control.cycle()['state'], 'waiting-for-reviewed-requests')
            self.assertFalse(control.HOLD.exists())
            cards.assert_not_called()

    def test_failed_worker_is_retained_without_a_global_hold(self):
        with tempfile.TemporaryDirectory() as td, patch.object(control, 'STATE', Path(td)), \
             patch.object(control, 'HOLD', Path(td) / 'HOLD'), \
             patch.object(control, 'QUEUE', Path(td) / 'jobs'), \
             patch.object(control.socket, 'gethostname', return_value='chiap08'), \
             patch.object(control, 'reconcile', return_value=[{'card': '1234abcd', 'state': 'terminal', 'exit_code': 1}]), \
             patch.object(control, 'cards') as cards:
            self.assertEqual(control.cycle()['state'], 'waiting-for-reviewed-requests')
            self.assertFalse(control.HOLD.exists())
            cards.assert_not_called()

    def test_repeated_failures_are_recorded_without_global_admission_hold(self):
        with tempfile.TemporaryDirectory() as td, patch.object(control, 'STATE', Path(td)), \
             patch.object(control, 'HOLD', Path(td) / 'HOLD'):
            task = dict(job(), card_fingerprint='a' * 64)
            for n in range(1, 4):
                self.assertEqual(control.record_attempt(task, 'remote-check-failed'), n)
            self.assertFalse(control.HOLD.exists())

    def test_capacity_waits_do_not_exhaust_fault_budget(self):
        with tempfile.TemporaryDirectory() as td, patch.object(control, 'STATE', Path(td)), \
             patch.object(control, 'HOLD', Path(td) / 'HOLD'):
            for reason in ('estate-capacity', 'host-capacity', 'provider-capacity', 'actual-start-spacing', 'card-not-claimable'):
                for _ in range(4):
                    self.assertEqual(control.record_attempt(job(), reason), 0)
            self.assertFalse(control.HOLD.exists())

    def test_ambiguous_launch_does_not_write_global_hold(self):
        with tempfile.TemporaryDirectory() as td, patch.object(control, 'STATE', Path(td)), \
             patch.object(control, 'HOLD', Path(td) / 'HOLD'):
            control.record_attempt(job(), 'launch-unacknowledged')
            self.assertFalse(control.HOLD.exists())

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
        truth['hosts'] = {'chiap02': truth['hosts']['chiap02']}
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

    def test_refill_rechecks_before_and_after_provider_probe_under_authority(self):
        task = dict(job(), model='deepseek-flash')
        events = []
        def check(request):
            events.append('qualification')
            if len(events) > 1:
                raise Refused('refill-stale-qualification')
        def probe(request):
            events.append('probe')
            return {'ready': True, 'cap': 3}
        with tempfile.TemporaryDirectory() as td, patch.object(control, 'STATE', Path(td)), \
             patch.object(control.time, 'time', return_value=1000), \
             patch.object(control, 'recheck_qualification', side_effect=check), \
             patch.object(control, 'collect', return_value=snapshot()), \
             patch.object(control, 'remote', return_value={'state': 'ready', 'card': task['card'],
                 'host': task['host'], 'model': task['model']}), \
             patch.object(control, 'qualify_route', side_effect=probe):
            with self.assertRaisesRegex(Refused, 'refill-stale'):
                control.prepare_job(task)
        self.assertEqual(events, ['qualification', 'probe', 'qualification'])

    def test_refill_is_inside_existing_authority_and_checks_hold_first(self):
        def refill(*args, **kwargs):
            with self.assertRaisesRegex(Refused, 'authority-busy'):
                with control.authority(control.STATE):
                    self.fail('second controller acquired authority')
            return {'state': 'refill-checked'}
        with tempfile.TemporaryDirectory() as td, patch.object(control, 'STATE', Path(td) / 'state'), \
             patch.object(control, 'QUEUE', Path(td) / 'jobs'), \
             patch.object(control, 'HOLD', Path(td) / 'HOLD'), \
             patch.object(control.socket, 'gethostname', return_value='chiap08'), \
             patch.object(control, 'refill_queue', side_effect=refill) as mocked:
            self.assertEqual(control.cycle()['state'], 'waiting-for-reviewed-requests')
            self.assertEqual(mocked.call_count, 1)
            control.set_hold()
            self.assertEqual(control.cycle()['state'], 'admission-held')
            self.assertEqual(mocked.call_count, 1)

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
             patch.object(control, 'verify_refill_request') as verify, patch.object(control, 'remote', return_value={'state': 'running'}) as remote:
            control.launch_claimed(task, {'token': 'b' * 32})
            verify.assert_called_once_with(row, task)
            claim.assert_called_once_with(task['card'], task['owner'])
            self.assertEqual(remote.call_args.args[1]['claim'],
                             {'authority': 'chiap08', 'owner': 'fiber-test', 'revision': 'a' * 32})
            claim.side_effect = Refused('claim-refused')
            remote.reset_mock()
            with self.assertRaisesRegex(Refused, 'claim-refused'):
                control.launch_claimed(task, {'token': 'c' * 32})
            remote.assert_not_called()

    def test_post_claim_verification_failure_never_launches(self):
        with patch.object(control.socket, 'gethostname', return_value='chiap08'), \
             patch.object(control, 'claim_for_worker', return_value={}), \
             patch.object(control, 'verify_refill_request', side_effect=Refused('lifecycle-source-superseded')), \
             patch.object(control, 'remote') as remote:
            with self.assertRaisesRegex(Refused, 'source-superseded'):
                control.launch_claimed(dict(job(), owner='fiber-test'), {'token': 'b' * 32})
            remote.assert_not_called()

    def test_candidate_and_bundle_transports_use_only_fixed_programs(self):
        for mode, program in [('candidate', 'fiber_candidate.py --bundle'), ('bundle', 'fiber_bundle.py')]:
            request = {'job': dict(job(), host='ziowk01'), 'bundle': {'sha256': 'a' * 64, 'bundle_b64': 'YWJj'}}
            with patch.object(control.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{}', '')) as run:
                control.remote(mode, request)
            self.assertTrue(run.call_args.args[0][-1].endswith('/.local/lib/skfleet-fiber/' + program))
            self.assertEqual(json.loads(run.call_args.kwargs['input']), request['bundle'] if mode == 'bundle' else request)

    def test_bundle_transfer_requires_exact_storage_acknowledgement(self):
        task = dict(job(), source_bundle={'sha256': 'a' * 64})
        with patch.object(control.fiber_bundle, 'select', return_value=task['source_bundle']), \
             patch.object(control.fiber_bundle, '_directory'), \
             patch.object(control.fiber_bundle, 'read_blob', return_value=b'abc'), \
             patch.object(control, 'remote', return_value={'state': 'stored', 'sha256': 'a' * 64, 'bytes': 3}) as remote:
            control.stage_bundle(task)
            self.assertEqual(remote.call_args.args[1]['bundle']['bundle_b64'], 'YWJj')
            remote.return_value['bytes'] = 2
            with self.assertRaisesRegex(Refused, 'transfer-unverified'):
                control.stage_bundle(task)

    def test_legacy_estate_size_does_not_block_gateway_qualification(self):
        truth = snapshot()
        truth['workers'] = [{'key': str(i)} for i in range(100)]
        task = dict(job(), model='deepseek-flash')
        with patch.object(control, 'collect', return_value=truth), \
             patch.object(control, 'remote', return_value={'state': 'ready', 'card': task['card'], 'host': task['host'], 'model': task['model']}), \
             patch.object(control, 'qualify_route', return_value={'ready': True}) as probe:
            self.assertTrue(control.observation(task)['providers']['deepseek']['ready'])
            probe.assert_called_once()

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

    def test_missing_board_client_is_explicitly_refused_after_local_holds(self):
        with patch('builtins.__import__', side_effect=ImportError('synthetic missing client')):
            self.assertFalse(control.eligible({'owner': 'another', 'status': 'doing'}, {}))
            self.assertFalse(control.eligible({'status': 'ready', 'dependencies': ['missing']}, {}))
            with self.assertRaisesRegex(Refused, 'board-gate-unavailable'):
                control.eligible({'id': '1234abcd', 'status': 'ready'}, {})

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

    def test_controller_decodes_only_a_bounded_fair_queue_page(self):
        # 40 queued names with recorded runs are skipped before JSON decoding;
        # 40 unlaunched names consume at most one MAX_INPUTS page. The rest are
        # reached on later passes via the persisted wrap-around cursor.
        import sqlite3
        with tempfile.TemporaryDirectory() as td, patch.object(control, 'STATE', Path(td) / 'state'), \
             patch.object(control, 'QUEUE', Path(td) / 'jobs'), \
             patch.object(control.socket, 'gethostname', return_value='chiap08'), \
             patch.object(control, 'reconcile', return_value=[]), \
             patch.object(control, 'connect_reviews', return_value={}), \
             patch.object(control, 'select_requests', return_value={}), \
             patch.object(control, 'refill_queue', return_value={}), \
             patch.object(control, 'cards') as cards, \
             patch.object(control, 'validate'), \
             patch.object(control, 'verify_source'), \
             patch.object(control, 'eligible', return_value=True), \
             patch.object(control, 'dispatch_one', return_value={'state': 'waiting', 'card': '0'}) as dispatch:
            control.STATE.mkdir(parents=True)
            with sqlite3.connect(control.STATE / 'admission.sqlite3') as db:
                db.execute('CREATE TABLE runs (card TEXT PRIMARY KEY, state TEXT, token TEXT)')
                for index in range(40):
                    db.execute('INSERT INTO runs VALUES (?, ?, ?)', (f'{index:08x}', 'terminal', 'c' * 32))
            control.QUEUE.mkdir(parents=True)
            for index in range(40):
                (control.QUEUE / f'{index:08x}.json').write_text('not json, must not be read')
            for index in range(40, 80):
                (control.QUEUE / f'{index:08x}.json').write_text(json.dumps(job(f'{index:08x}')))
            cards.return_value = {f'{index:08x}': {'id': f'{index:08x}'} for index in range(40, 80)}
            control.cycle()
            self.assertLessEqual(dispatch.call_count, control.MAX_INPUTS)
            self.assertEqual(cards.call_count, 1)
            decoded = sorted(cards.call_args.args[0])
            self.assertEqual(decoded, [f'{index:08x}' for index in range(40, 72)])

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
