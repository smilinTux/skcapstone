"""Behavioral checks for central admission and non-destructive workspaces."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

MODULE = Path(__file__).resolve().parents[1] / 'fiber_admission.py'
spec = importlib.util.spec_from_file_location('fiber_admission', MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def snapshot(now=1000):
    return {'observed_at': now, 'complete': True, 'workers': [],
            'hosts': {'chiap02': {'ready': True, 'slots': 2},
                      'chiap03': {'ready': True, 'slots': 1},
                      'chiap04': {'ready': True, 'slots': 1}},
            'providers': {'deepseek': {'ready': True, 'cap': 3},
                          'codex': {'ready': True, 'cap': 2}}}


def job(card='1234abcd'):
    return {'card': card, 'host': 'chiap02', 'stage': 'implementation',
            'provider': 'deepseek', 'claimable': True}


class AdmissionTest(unittest.TestCase):
    def test_trial_limit_is_twelve_not_provider_entitlement(self):
        self.assertEqual(module.ESTATE_CAP, 12)

    def test_placement_spreads_work_and_counts_pending_reservations(self):
        truth = snapshot()
        truth['hosts']['chiwk12'] = {'ready': True, 'slots': 2}
        truth['workers'] = [{'key': 'native', 'host': 'chiap02', 'provider': 'codex'}]
        task = dict(job(), host='auto')
        self.assertEqual(module.select_host(self.db, task, truth, now=1000), 'chiwk12')
        self.reserve(dict(job(), host='chiwk12'), truth)
        truth['workers'].append({'key': 'native2', 'host': 'chiwk12', 'provider': 'codex'})
        self.assertEqual(module.select_host(self.db, task, truth, now=1000), 'chiap02')

    def test_placement_requires_complete_fresh_truth_and_never_auto_wan(self):
        truth = snapshot()
        truth['hosts'] = {'ziowk01': {'ready': True, 'slots': 2},
                          'chiap01': {'ready': True, 'slots': 20},
                          'chiap08': {'ready': True, 'slots': 20}}
        with self.assertRaisesRegex(module.Refused, 'no-eligible-host'):
            module.select_host(self.db, dict(job(), host='auto'), truth, now=1000)
        truth['complete'] = False
        with self.assertRaisesRegex(module.Refused, 'incomplete-observation'):
            module.select_host(self.db, dict(job(), host='auto'), truth, now=1000)
        with self.assertRaisesRegex(module.Refused, 'stale-observation'):
            module.select_host(self.db, dict(job(), host='auto'), snapshot(900), now=1000)

    def test_light_host_requires_small_card_and_wan_requires_explicit_pin(self):
        truth = snapshot()
        truth['hosts']['chiwk13'] = {'ready': True, 'slots': 1}
        truth['hosts']['chiap04']['ready'] = False
        truth['hosts']['chiap02']['ready'] = False
        task = dict(job(), stage='tests', host='auto', work_class='S')
        self.assertEqual(module.select_host(self.db, task, truth, now=1000), 'chiwk13')
        task['work_class'] = 'M'
        with self.assertRaisesRegex(module.Refused, 'no-eligible-host'):
            module.select_host(self.db, task, truth, now=1000)
        task.update(host='ziowk01', stage='implementation')
        truth['hosts']['ziowk01'] = {'ready': True, 'slots': 2}
        self.assertEqual(module.select_host(self.db, task, truth, now=1000), 'ziowk01')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'admission.sqlite3'

    def reserve(self, task=None, truth=None, now=1000):
        return module.reserve(self.db, task or job(), truth or snapshot(now), now=now)

    def test_existing_workers_count_without_name_prefix_filter(self):
        truth = snapshot()
        truth['workers'] = [{'key': f'legacy:{i}', 'host': 'chiap08', 'provider': 'deepseek'} for i in range(12)]
        with self.assertRaisesRegex(module.Refused, 'estate-capacity'):
            self.reserve(truth=truth)

    def test_spacing_and_duplicate_are_persistent(self):
        self.reserve()
        with self.assertRaisesRegex(module.Refused, 'duplicate-card'):
            self.reserve(now=1075)
        with self.assertRaisesRegex(module.Refused, 'launch-spacing'):
            self.reserve(job('1234abce'), now=1074)
        self.reserve(job('1234abce'), now=1075)

    def test_running_worker_and_reservation_count_once(self):
        from fiber_probe import process_provider
        self.reserve()
        truth = snapshot(1075)
        truth['workers'] = [{'key': 'card:1234abcd', 'host': 'chiap02',
                             'provider': process_provider('pi', {'SKFLEET_PROVIDER': 'deepseek',
                                                                  'SKFLEET_MODEL': 'deepseek-flash'})}]
        # One running worker plus its reservation is one slot, not ambiguous
        # occupancy and not two workers on this two-slot implementation host.
        self.assertEqual(self.reserve(job('1234abce'), truth=truth, now=1075)['card'], '1234abce')

    def test_concurrent_reservations_have_one_winner(self):
        def attempt(_):
            try:
                self.reserve()
                return True
            except module.Refused:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(attempt, range(4))), 1)

    def test_unknown_stale_and_future_truth_refuse(self):
        for delta in (-31, 1):
            with self.subTest(delta=delta), self.assertRaisesRegex(module.Refused, 'stale-observation'):
                self.reserve(truth=snapshot(1000 + delta))
        truth = snapshot()
        truth['complete'] = False
        with self.assertRaisesRegex(module.Refused, 'incomplete-observation'):
            self.reserve(truth=truth)

    def test_failed_claim_gate_unavailable_host_and_provider(self):
        task = job()
        task['claimable'] = False
        with self.assertRaisesRegex(module.Refused, 'card-not-claimable'):
            self.reserve(task)
        for field, key in [('hosts', 'chiap02'), ('providers', 'deepseek')]:
            truth = snapshot()
            truth[field][key]['ready'] = False
            with self.subTest(field=field), self.assertRaises(module.Refused):
                self.reserve(truth=truth)

    def test_host_caps_and_stage_placement(self):
        self.reserve()
        self.reserve(job('1234abce'), now=1075)
        with self.assertRaisesRegex(module.Refused, 'host-capacity'):
            self.reserve(job('1234abcf'), now=1150)
        task = job('1234abc0')
        task['host'] = 'chiap08'
        with self.assertRaisesRegex(module.Refused, 'stage-placement'):
            self.reserve(task, now=1150)

    def test_same_provider_review_and_kimi_are_refused(self):
        task = job()
        task.update(host='chiap03', stage='review', producer_provider='deepseek')
        with self.assertRaisesRegex(module.Refused, 'review-independence'):
            self.reserve(task)
        task = job()
        task['provider'] = 'kimi'
        with self.assertRaisesRegex(module.Refused, 'provider-not-qualified'):
            self.reserve(task)

    def test_reservation_and_live_worker_are_counted_once(self):
        self.reserve()
        truth = snapshot(1075)
        truth['workers'] = [{'key': 'card:1234abcd', 'host': 'chiap02', 'provider': 'deepseek'}]
        self.reserve(job('1234abce'), truth, now=1075)


class WorkspaceTest(unittest.TestCase):
    def test_existing_paths_and_branches_are_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / 'repo'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)
            revision = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
            occupied = root / 'occupied'
            occupied.mkdir()
            sentinel = occupied / 'unfinished.txt'
            sentinel.write_text('user work')
            with self.assertRaisesRegex(module.Refused, 'occupied-workspace'):
                module.prepare_worktree(repo, occupied, 'work/test', revision)
            self.assertEqual(sentinel.read_text(), 'user work')
            link = root / 'dangling'
            link.symlink_to(root / 'absent')
            with self.assertRaisesRegex(module.Refused, 'occupied-workspace'):
                module.prepare_worktree(repo, link, 'work/test', revision)
            branch = subprocess.check_output(['git', '-C', str(repo), 'branch', '--show-current'], text=True).strip()
            with self.assertRaisesRegex(module.Refused, 'existing-branch'):
                module.prepare_worktree(repo, root / 'new', branch, revision)
            self.assertFalse((root / 'new').exists())
            module.prepare_worktree(repo, root / 'new', 'work/test', revision)
            self.assertEqual(subprocess.check_output(['git', '-C', str(root / 'new'), 'rev-parse', 'HEAD'], text=True).strip(), revision)


class ClaimTest(unittest.TestCase):
    def test_read_only_requires_supplied_generation_without_claiming(self):
        calls = []
        row = {'id': '1234abcd', 'owner': 'fiber-test', 'status': 'doing',
               'meta': {'_claim_revision': 'b' * 32}}
        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([row]), stderr='')
        with self.assertRaisesRegex(module.Refused, 'claim-owner-or-revision-mismatch'):
            module.read_claim('1234abcd', 'fiber-test', revision='a' * 32, runner=run)
        self.assertEqual(module.read_claim('1234abcd', 'fiber-test', revision='b' * 32, runner=run), row)
        self.assertTrue(all(argv == ['skcapstone', 'coord', 'show', '1234abcd', '--json'] for argv in calls))

    def test_refused_claim_stops_before_readback(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, stdout='refused', stderr='')
        with self.assertRaisesRegex(module.Refused, 'claim-refused'):
            module.claim_for_worker('1234abcd', 'fiber-test', runner=run)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('--force', calls[0])

    def test_exact_owner_revision_and_active_state_required(self):
        for owner, revision, status, passes in [('someone-else', 'a' * 32, 'doing', False),
                                              ('fiber-test', '', 'doing', False),
                                              ('fiber-test', 'a' * 32, 'done', False),
                                              ('fiber-test', 'a' * 32, 'ready', True)]:
            row = {'id': '1234abcd', 'owner': owner, 'status': status, 'meta': {'_claim_revision': revision}}
            def run(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({'feature': {'ready': [row]}}), stderr='')
            with self.subTest(owner=owner, revision=revision, status=status):
                if passes:
                    self.assertEqual(module.claim_for_worker('1234abcd', 'fiber-test', runner=run), row)
                else:
                    with self.assertRaises(module.Refused):
                        module.claim_for_worker('1234abcd', 'fiber-test', runner=run)


if __name__ == '__main__':
    unittest.main()
