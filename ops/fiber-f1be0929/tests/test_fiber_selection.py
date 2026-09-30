"""Selection reuses the existing promoter and cannot grant launch authority."""
import copy
import importlib.util
import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fiber_selection as selection
from fiber_admission import Refused, card_fingerprint
from fiber_refill import digest, _qualified


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ready = self.root / 'qualified'
        self.ready.mkdir(mode=0o700)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.hold = self.root / 'HOLD'
        self.policy = self.root / 'selection.json'
        self.row = {'id': '1234abcd', 'kind': 'task', 'title': '[SKLEGAL-W7][S] Synthetic test',
                    'description': 'Source-only synthetic test', 'acceptance_criteria': ['Test passes'],
                    'status': 'ready', 'owner': None, 'labels': ['source-only', 'sklegal', 'w7'],
                    'dependencies': [], 'meta': {}, 'links': {}}
        self.job = {'card': self.row['id'], 'host': 'auto', 'stage': 'implementation',
                    'provider': 'deepseek', 'repository': 'https://example.invalid/sklegal.git',
                    'base_revision': 'a' * 40, 'card_fingerprint': card_fingerprint(self.row)}
        self.config = {'schema': 'skfleet.selection/v1', 'cards': {self.row['id']: card_fingerprint(self.row)},
                       'repository': self.job['repository'], 'qualified_at': 100, 'expires_at': 1000,
                       'evidence_sha256': 'b' * 64}
        self.save()
        promoter_path = Path(__file__).resolve().parents[3] / 'scripts/fleet/skfleet-backlog-promoter.py'
        spec = importlib.util.spec_from_file_location('test_existing_promoter', promoter_path)
        self.promoter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.promoter)

    def save(self):
        self.policy.write_text(json.dumps(self.config))
        self.policy.chmod(0o600)

    def run_selection(self, **kwargs):
        kwargs.setdefault('clock', lambda: 201)
        return selection.select_requests(self.state, self.ready, self.policy, self.hold,
            read_cards=lambda ids: {self.row['id']: self.row},
            describe=lambda *args: copy.deepcopy(self.job), eligible=lambda row, board: True,
            verify=lambda row, job: None, promoter=self.promoter, now=200, **kwargs)

    def test_selects_exact_source_once_without_claiming(self):
        self.assertEqual(self.run_selection()['added'], ['1234abcd'])
        receipt = _qualified(self.ready / '1234abcd.json', 201)
        self.assertEqual(receipt['job'], self.job)
        self.assertEqual(receipt['card_links_sha256'], digest(self.row['links']))
        self.assertEqual(self.run_selection()['added'], [])
        self.assertIsNone(self.row['owner'])

    def test_disabled_and_hold_are_noops(self):
        self.policy.unlink()
        self.assertEqual(self.run_selection()['state'], 'disabled')
        self.save()
        self.hold.write_text('hold')
        self.assertEqual(self.run_selection()['state'], 'admission-held')
        self.assertEqual(list(self.ready.iterdir()), [])

    def test_preview_writes_nothing(self):
        self.assertEqual(self.run_selection(dry_run=True)['added'], ['1234abcd'])
        self.assertEqual(list(self.ready.iterdir()), [])

    def test_uses_existing_promoter_holds_and_no_action_rules(self):
        for label in ('human-gate', 'no-action-authorized', 'do-not-claim', 'review'):
            with self.subTest(label=label):
                self.row['labels'] = ['source-only', 'sklegal', 'w7', label]
                self.config['cards'][self.row['id']] = card_fingerprint(self.row)
                self.save()
                self.assertEqual(self.run_selection()['added'], [])

    def test_owned_changed_and_prior_output_never_selected(self):
        self.row['owner'] = 'another-worker'
        self.assertEqual(self.run_selection()['added'], [])
        self.row['owner'] = None
        self.row['description'] = 'changed contract'
        self.assertEqual(self.run_selection()['added'], [])
        self.config['cards'][self.row['id']] = card_fingerprint(self.row)
        self.save()
        for key in ('commit', 'evidence', 'verdict', 'candidate', 'unexpected_output'):
            self.row['links'] = {key: 'preserved'}
            self.assertEqual(self.run_selection()['added'], [])

    def test_blocked_dependency_and_seat_are_refused(self):
        for change in ({'dependencies': ['deadbeef']}, {'labels': ['source-only', 'sklegal', 'w7', 'seat-atlas']}):
            self.row.update(change)
            self.config['cards'][self.row['id']] = card_fingerprint(self.row)
            self.save()
            self.assertEqual(self.run_selection()['added'], [])

    def test_foreign_repository_and_non_breadth_work_are_refused(self):
        self.job['repository'] = 'https://example.invalid/other.git'
        self.assertEqual(self.run_selection()['added'], [])
        self.job['repository'] = self.config['repository']
        self.row['title'] = '[SKLEGAL-W7][L] Heavy integration'
        self.config['cards'][self.row['id']] = card_fingerprint(self.row)
        self.save()
        self.assertEqual(self.run_selection()['added'], [])

    def test_policy_is_bounded_and_expires(self):
        for value in (float('nan'), 199, 100000):
            self.config['expires_at'] = value
            self.save()
            with self.assertRaises(Refused):
                self.run_selection()
        self.config['expires_at'] = 1000
        self.config['cards'] = {f'{n:08x}': 'a' * 64 for n in range(33)}
        self.save()
        with self.assertRaises(Refused):
            self.run_selection()

    def test_symlink_policy_is_rejected(self):
        original = self.root / 'original.json'
        self.policy.rename(original)
        self.policy.symlink_to(original)
        with self.assertRaises(OSError):
            self.run_selection()

    def test_prior_runs_never_replayed(self):
        with sqlite3.connect(self.state / 'admission.sqlite3') as db:
            db.execute('CREATE TABLE runs (card TEXT PRIMARY KEY, state TEXT)')
            db.execute('INSERT INTO runs VALUES (?, ?)', (self.row['id'], 'terminal'))
        self.assertEqual(self.run_selection()['added'], [])
        self.assertEqual(list(self.ready.iterdir()), [])

    def test_native_gate_and_verifier_refusals_are_preserved(self):
        with patch.object(selection, 'load_promoter', return_value=self.promoter):
            result = selection.select_requests(self.state, self.ready, self.policy, self.hold,
                read_cards=lambda ids: {self.row['id']: self.row}, describe=lambda *args: self.job,
                eligible=lambda row, board: False, verify=lambda row, job: None, now=200)
        self.assertEqual(result['skipped'][0]['reason'], 'selection-native-gate')
        self.assertEqual(list(self.ready.iterdir()), [])

    def test_two_pending_receipts_stop_new_selection(self):
        self.run_selection()
        receipt = json.loads((self.ready / '1234abcd.json').read_text())
        receipt['qualified_at'] = 100
        (self.ready / '1234abcd.json').unlink()
        for cid in ('abcdef01', 'abcdef02'):
            receipt['job']['card'] = cid
            receipt['job_sha256'] = digest(receipt['job'])
            (self.ready / (cid + '.json')).write_text(json.dumps(receipt))
            (self.ready / (cid + '.json')).chmod(0o600)
        self.assertEqual(self.run_selection()['added'], [])

    def test_expiry_during_slow_checks_publishes_nothing(self):
        result = self.run_selection(clock=lambda: 1000)
        self.assertEqual(result['added'], [])
        self.assertEqual(result['skipped'][0]['reason'], 'selection-expired-during-check')
        self.assertEqual(list(self.ready.iterdir()), [])

    def test_expired_receipts_are_preserved_without_blocking_new_scope(self):
        self.run_selection()
        original = self.ready / '1234abcd.json'
        receipt = json.loads(original.read_text())
        original.unlink()
        for cid in ('abcdef01', 'abcdef02'):
            receipt['job']['card'] = cid
            receipt['job_sha256'] = digest(receipt['job'])
            receipt['qualified_at'], receipt['expires_at'] = 100, 199
            (self.ready / (cid + '.json')).write_text(json.dumps(receipt))
            (self.ready / (cid + '.json')).chmod(0o600)
        self.assertEqual(self.run_selection()['added'], ['1234abcd'])
        self.assertEqual(len(list(self.ready.iterdir())), 3)

    def test_malformed_labels_and_wrong_describe_identity_are_refused(self):
        self.job['card'] = 'deadbeef'
        self.assertEqual(self.run_selection()['added'], [])
        self.job['card'] = self.row['id']
        self.row['labels'] = [12, 13]
        self.config['cards'][self.row['id']] = card_fingerprint(self.row)
        self.save()
        self.assertEqual(self.run_selection()['skipped'][0]['reason'], 'selection-invalid-labels')

    def test_real_describe_verify_and_dependency_closure(self):
        import fiber_control as control
        self.row['dependencies'] = ['abcdef01']
        self.row['links'] = {'repository': self.config['repository'], 'base_ref': 'main', 'base_revision': 'a' * 40}
        self.config['cards'][self.row['id']] = card_fingerprint(self.row)
        self.save()
        board = {self.row['id']: self.row, 'abcdef01': {'id': 'abcdef01', 'status': 'done', 'links': {'verdict': 'PASS'}}}
        with patch.object(control, 'cards', return_value=board):
            result = selection.select_requests(self.state, self.ready, self.policy, self.hold,
                read_cards=lambda ids: board, describe=control.describe,
                eligible=lambda row, board: True, verify=control.verify_refill_request,
                promoter=self.promoter, now=200, clock=lambda: 201)
        self.assertEqual(result['added'], ['1234abcd'])
        receipt = _qualified(self.ready / '1234abcd.json', 202)
        self.assertEqual(receipt['job']['model'], 'deepseek-flash')
        self.assertEqual(receipt['job']['base_revision'], 'a' * 40)

    def test_controller_checks_selection_inside_authority_before_refill(self):
        import fiber_control as control
        events = []
        class Authority:
            def __enter__(self):
                events.append('locked')
            def __exit__(self, *args):
                events.append('unlocked')
        queue = self.root / 'jobs'
        with patch.object(control, 'QUEUE', queue), patch.object(control, 'STATE', self.state), \
             patch.object(control, 'HOLD', self.hold), \
             patch.object(control.socket, 'gethostname', return_value='chiap08'), \
             patch.object(control, 'authority', return_value=Authority()), \
             patch.object(control, 'reconcile', return_value=[]), \
             patch.object(control, 'select_requests', side_effect=lambda *a, **k: events.append('select') or {}), \
             patch.object(control, 'refill_queue', side_effect=lambda *a, **k: events.append('refill') or {}):
            result = control.cycle()
        self.assertEqual(events, ['locked', 'select', 'refill', 'unlocked'])
        self.assertEqual(result['state'], 'waiting-for-reviewed-requests')

    def test_regular_crew_card_does_not_require_workspace_label(self):
        self.row['labels'] = ['source-only', 'sklegal']
        self.row['links'] = {'tdd_custody': 'Immutable task instructions on the card.'}
        self.config['cards'][self.row['id']] = card_fingerprint(self.row)
        self.job['card_fingerprint'] = card_fingerprint(self.row)
        self.save()
        self.assertEqual(self.run_selection()['added'], ['1234abcd'])
        receipt = _qualified(self.ready/'1234abcd.json', 201)
        self.assertEqual(receipt['card_links_sha256'], digest(self.row['links']))

    def test_project_and_source_only_labels_remain_required(self):
        for labels in (['source-only', 'w7'], ['sklegal', 'w7'], ['w7']):
            with self.subTest(labels=labels):
                self.row['labels'] = labels
                self.config['cards'][self.row['id']] = card_fingerprint(self.row)
                self.job['card_fingerprint'] = card_fingerprint(self.row)
                self.save()
                result = self.run_selection(dry_run=True)
                self.assertEqual(result['added'], [])
                self.assertEqual(result['skipped'][0]['reason'], 'selection-outside-scope')

    def test_tdd_custody_drift_invalidates_scope_before_selection(self):
        self.row['links'] = {'tdd_custody': 'Approved instructions.'}
        self.config['cards'][self.row['id']] = card_fingerprint(self.row)
        self.job['card_fingerprint'] = card_fingerprint(self.row)
        self.save()
        self.row['links']['tdd_custody'] = 'Changed instructions.'
        self.assertEqual(self.run_selection()['skipped'][0]['reason'], 'selection-contract-changed')

    def test_absent_tdd_custody_preserves_existing_fingerprints(self):
        import hashlib
        material = {key:self.row.get(key) for key in ['id', 'title', 'description', 'acceptance_criteria']}
        material.update(labels=sorted(self.row['labels']), dependencies=[], meta={},
                        links={key:None for key in ['repository', 'base_ref', 'base_revision', 'assigned_tdd']})
        legacy = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(card_fingerprint(self.row), legacy)


if __name__ == '__main__':
    unittest.main()
