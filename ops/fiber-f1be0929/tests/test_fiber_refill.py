"""Bounded refill checks use synthetic files and no live board or providers."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fiber_refill as refill
from fiber_admission import Refused, card_fingerprint


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class RefillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ready = self.root / 'qualified'
        self.queue = self.root / 'jobs'
        self.state = self.root / 'state'
        self.hold = self.root / 'HOLD'
        for path in (self.ready, self.queue, self.state):
            path.mkdir(mode=0o700)
        self.rows = {}
        self.reads = []
        self.db = sqlite3.connect(self.state / 'admission.sqlite3')
        self.db.execute('CREATE TABLE runs (card TEXT PRIMARY KEY, state TEXT, token TEXT)')
        self.addCleanup(self.db.close)

    def request(self, cid='1234abcd'):
        row = {'id': cid, 'title': '[S] synthetic', 'description': 'bounded',
               'status': 'ready', 'owner': None, 'dependencies': [], 'labels': ['source-only'],
               'acceptance_criteria': ['evidence'], 'meta': {}, 'links': {}}
        self.rows[cid] = row
        job = {'card': cid, 'owner': 'pi-fiber-implementation-' + cid,
               'stage': 'implementation', 'host': 'chiap02', 'work_class': 'S',
               'provider': 'deepseek', 'pi_provider': 'skgw-deepseek', 'model': 'deepseek-flash',
               'repository': 'https://example.invalid/repo.git', 'base_ref': 'main',
               'base_revision': 'a' * 40, 'card_fingerprint': card_fingerprint(row)}
        receipt = {'schema': 'skfleet.qualified-request/v1', 'job': job,
                   'job_sha256': digest(job), 'card_links_sha256': digest(row['links']),
                   'qualified_at': 100, 'expires_at': 3700, 'evidence_sha256': 'b' * 64}
        self.save(cid, receipt)
        return receipt

    def save(self, cid, receipt):
        path = self.ready / (cid + '.json')
        path.write_text(json.dumps(receipt))
        path.chmod(0o600)

    def read(self, ids):
        self.reads.append(ids)
        return {cid: self.rows[cid] for cid in ids}

    def run_refill(self, **kwargs):
        return refill.refill_queue(self.state, self.queue, self.ready, self.hold,
                                   read_cards=self.read, eligible=lambda row, board: not row['owner'],
                                   verify=lambda row, job: None, now=200, **kwargs)

    def test_adds_exact_job_once_and_keeps_qualification(self):
        receipt = self.request()
        self.assertEqual(self.run_refill()['added'], ['1234abcd'])
        self.assertEqual(json.loads((self.queue / '1234abcd.json').read_text()),
                         dict(receipt['job'], _qualification=receipt))
        self.assertEqual(self.run_refill()['added'], [])
        self.assertTrue((self.ready / '1234abcd.json').exists())

    def test_hold_precedes_all_reads_and_writes(self):
        self.request()
        self.hold.write_text('operator hold')
        self.assertEqual(self.run_refill()['state'], 'admission-held')
        self.assertEqual(self.reads, [])
        self.assertEqual(list(self.queue.iterdir()), [])

    def test_pending_queue_is_bounded_and_next_request_refills_after_terminal(self):
        for cid in ('1234abc1', '1234abc2', '1234abc3'):
            self.request(cid)
        self.assertEqual(self.run_refill()['added'], ['1234abc1', '1234abc2'])
        original = (self.queue / '1234abc1.json').read_bytes()
        self.db.execute('INSERT INTO runs VALUES (?, ?, ?)', ('1234abc1', 'terminal', 'c' * 32))
        self.db.commit()
        result = self.run_refill()
        self.assertEqual(result['added'], ['1234abc3'])
        self.assertEqual(result['archived'], ['1234abc1'])
        archived = list((self.queue / 'terminal').iterdir())
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), original)

    def test_any_prior_run_is_never_replayed(self):
        self.request()
        for state in ('launching', 'running', 'terminal'):
            self.db.execute('INSERT OR REPLACE INTO runs VALUES (?, ?, ?)', ('1234abcd', state, 'c' * 32))
            self.db.commit()
            self.assertEqual(self.run_refill()['added'], [])
        self.assertEqual(self.reads, [])

    def test_stale_receipt_changed_job_card_or_links_and_owner_are_rejected(self):
        for variant in ('expired', 'future', 'nan', 'long', 'job', 'contract', 'links', 'owner'):
            with self.subTest(variant=variant):
                r = self.request()
                if variant == 'expired': r['expires_at'] = 199
                if variant == 'future': r['qualified_at'] = 201
                if variant == 'nan': r['expires_at'] = float('nan')
                if variant == 'long': r['expires_at'] = 999999
                if variant == 'job': r['job']['model'] = 'unapproved'
                if variant == 'contract': self.rows['1234abcd']['description'] = 'changed'
                if variant == 'links': self.rows['1234abcd']['links']['commit'] = 'c' * 40
                if variant == 'owner': self.rows['1234abcd']['owner'] = 'someone-else'
                self.save('1234abcd', r)
                self.assertEqual(self.run_refill()['added'], [])

    def test_bad_file_does_not_block_later_valid_request(self):
        (self.ready / '00000000.json').write_text('invalid JSON')
        self.request()
        self.assertEqual(self.run_refill()['added'], ['1234abcd'])

    def test_symlink_world_writable_oversized_and_extra_fields_refused(self):
        for variant in ('symlink', 'writable', 'oversized', 'extra'):
            with self.subTest(variant=variant):
                path = self.ready / '1234abcd.json'
                if path.exists() or path.is_symlink(): path.unlink()
                r = self.request()
                if variant == 'symlink':
                    copy = self.root / 'outside'; copy.write_text(path.read_text()); path.unlink(); path.symlink_to(copy)
                if variant == 'writable': path.chmod(0o666)
                if variant == 'oversized': path.write_text(' ' * 65537)
                if variant == 'extra': r['unexpected'] = True; self.save('1234abcd', r)
                self.assertEqual(self.run_refill()['added'], [])

    def test_existing_request_is_not_overwritten(self):
        self.request()
        target = self.queue / '1234abcd.json'; target.write_text('{"operator":"preserve"}')
        self.run_refill()
        self.assertEqual(target.read_text(), '{"operator":"preserve"}')

    def test_missing_qualified_directory_is_disabled(self):
        self.ready.rmdir()
        self.assertEqual(self.run_refill()['state'], 'disabled')

    def test_preview_performs_no_writes(self):
        self.request()
        self.assertEqual(self.run_refill(dry_run=True)['added'], ['1234abcd'])
        self.assertEqual(list(self.queue.iterdir()), [])

    def test_native_dependency_or_source_refusal_does_not_queue(self):
        self.request()
        with patch.object(refill, 'card_fingerprint', side_effect=Refused('test-source-refusal')):
            self.assertEqual(self.run_refill()['added'], [])

    def check(self, task, clock=lambda: 200):
        return refill.check_qualification(task, self.ready, read_cards=self.read,
            eligible=lambda row, board: not row['owner'], verify=lambda row, job: None, clock=clock)

    def test_queued_qualification_expires_or_is_revoked_and_never_downgrades(self):
        receipt = self.request()
        task = dict(receipt['job'], _qualification=receipt, claimable=True)
        self.check(task)
        with self.assertRaisesRegex(Refused, 'stale'):
            self.check(task, clock=lambda: 3700)
        replacement = dict(receipt, evidence_sha256='c' * 64)
        self.save('1234abcd', replacement)
        with self.assertRaisesRegex(Refused, 'qualification-changed'):
            self.check(task)
        (self.ready / '1234abcd.json').unlink()
        with self.assertRaises(FileNotFoundError):
            self.check(task)

    def test_queued_card_evidence_owner_and_job_changes_refuse(self):
        for variant in ('links', 'owner', 'contract', 'job'):
            with self.subTest(variant=variant):
                receipt = self.request()
                task = dict(receipt['job'], _qualification=receipt)
                if variant == 'links': self.rows['1234abcd']['links']['evidence'] = 'new'
                if variant == 'owner': self.rows['1234abcd']['owner'] = 'another'
                if variant == 'contract': self.rows['1234abcd']['description'] = 'new'
                if variant == 'job': task['model'] = 'different'
                with self.assertRaises(Refused):
                    self.check(task)

    def test_auto_placement_is_the_only_allowed_job_change(self):
        receipt = self.request()
        receipt['job']['host'] = 'auto'
        receipt['job_sha256'] = digest(receipt['job'])
        self.save('1234abcd', receipt)
        self.check(dict(receipt['job'], host='chiap03', _qualification=receipt))
        times = iter([200, 3700])
        with self.assertRaisesRegex(Refused, 'stale'):
            self.check(dict(receipt['job'], _qualification=receipt), clock=lambda: next(times))

    def test_legacy_jobs_are_unchanged(self):
        self.check({'card': '1234abcd'})
        self.assertEqual(self.reads, [])

    def test_publish_crash_and_occupied_names_do_not_clobber_or_duplicate(self):
        receipt = self.request()
        with patch.object(refill.os, 'link', side_effect=OSError('simulated crash')):
            self.assertEqual(self.run_refill()['added'], [])
        self.assertEqual(list(self.queue.iterdir()), [])
        self.assertEqual(self.run_refill()['added'], ['1234abcd'])
        target = self.queue / '1234abcd.json'
        original = target.read_bytes()
        with self.assertRaises(FileExistsError):
            refill._publish(target, receipt['job'])
        self.assertEqual(target.read_bytes(), original)

    def test_terminal_archive_crash_retains_original_and_recovers_idempotently(self):
        self.request()
        self.run_refill()
        target = self.queue / '1234abcd.json'
        original = target.read_bytes()
        self.db.execute('INSERT INTO runs VALUES (?, ?, ?)', ('1234abcd', 'terminal', 'c' * 32))
        self.db.commit()
        with patch.object(refill, '_sync_directory', side_effect=OSError('simulated crash')):
            with self.assertRaises(OSError):
                self.run_refill()
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.run_refill()['archived'], ['1234abcd'])
        self.assertEqual(self.run_refill()['archived'], [])
        self.assertEqual(next((self.queue / 'terminal').iterdir()).read_bytes(), original)

    def test_overfull_inbox_and_unsafe_directory_refuse(self):
        for index in range(33):
            self.request(f'{index:08x}')
        with self.assertRaisesRegex(Refused, 'too-many'):
            self.run_refill()
        self.ready.chmod(0o777)
        with self.assertRaisesRegex(Refused, 'unsafe-directory'):
            self.run_refill()

    def test_running_requests_remain_untouched(self):
        self.request()
        self.run_refill()
        target = self.queue / '1234abcd.json'
        original = target.read_bytes()
        self.db.execute('INSERT INTO runs VALUES (?, ?, ?)', ('1234abcd', 'running', 'c' * 32))
        self.db.commit()
        self.assertEqual(self.run_refill()['archived'], [])
        self.assertEqual(target.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
