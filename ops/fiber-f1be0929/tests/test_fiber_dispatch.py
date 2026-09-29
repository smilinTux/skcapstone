"""Launch lifecycle tests, including lost acknowledgments and crash recovery."""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fiber_admission import Refused
from fiber_dispatch import authority, dispatch_one
from test_fiber_admission import job, snapshot


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000
        self.observed = {}
        self.starts = []

    def launch(self, task, receipt):
        self.starts.append(task['card'])
        result = dict(receipt, invocation='a' * 32, state='running', main_pid=42)
        self.observed[task['card']] = result
        return result

    def run_job(self, task=None, launch=None):
        return dispatch_one(self.root, task or job(), lambda: snapshot(self.now),
                            launch or self.launch, lambda run: self.observed.get(run['card']),
                            clock=lambda: self.now)

    def test_retry_does_not_start_second_worker(self):
        self.assertTrue(self.run_job()['new_launch'])
        self.assertFalse(self.run_job()['new_launch'])
        self.assertEqual(len(self.starts), 1)

    def test_hold_blocks_before_observation_and_reservation(self):
        marker = self.root / 'HOLD'
        marker.touch()
        def forbidden():
            self.fail('held admission must not collect or probe')
        with self.assertRaisesRegex(Refused, 'admission-held'):
            dispatch_one(self.root, job(), forbidden, self.launch, lambda run: None,
                         clock=lambda: self.now, hold_file=marker)
        self.assertEqual(self.starts, [])
        self.assertFalse((self.root / 'admission.sqlite3').exists())

    def test_completed_before_ack_is_a_terminal_launch(self):
        def quick(task, receipt):
            result = self.launch(task, receipt)
            result.update(state='terminal', main_pid=0, exit_code=0)
            return result
        result = self.run_job(launch=quick)
        self.assertTrue(result['new_launch'])
        self.assertEqual(result['state'], 'terminal')
        self.assertFalse(self.run_job()['new_launch'])

    def test_unclaimable_card_does_not_collect_or_probe(self):
        task = dict(job(), claimable=False)
        def forbidden():
            self.fail('Unclaimable card must not trigger a model probe')
        with self.assertRaisesRegex(Refused, 'card-not-claimable'):
            dispatch_one(self.root, task, forbidden, self.launch, lambda run: None, clock=lambda: self.now)

    def test_lost_ack_recovers_existing_unit(self):
        def lost(task, receipt):
            self.launch(task, receipt)
            raise TimeoutError()
        with self.assertRaisesRegex(Refused, 'launch-unacknowledged'):
            self.run_job(launch=lost)
        self.now += 5
        self.assertFalse(self.run_job()['new_launch'])
        self.assertEqual(len(self.starts), 1)

    def test_missing_handle_never_restarts_or_releases(self):
        def absent(task, receipt):
            raise TimeoutError()
        with self.assertRaises(Refused):
            self.run_job(launch=absent)
        self.now += 3600
        result = self.run_job()
        self.assertEqual(result['state'], 'launching')
        self.assertFalse(result['new_launch'])
        with self.assertRaisesRegex(Refused, 'unresolved-launch'):
            self.run_job(job('1234abce'))
        self.assertEqual(self.starts, [])

    def test_spacing_measured_after_slow_remote_start(self):
        def slow(task, receipt):
            self.now += 100
            return self.launch(task, receipt)
        self.run_job(launch=slow)
        self.now += 74
        with self.assertRaisesRegex(Refused, 'actual-start-spacing'):
            self.run_job(job('1234abce'))
        self.now += 1
        self.assertTrue(self.run_job(job('1234abce'))['new_launch'])

    def test_terminal_receipt_releases_slot_not_history_or_spacing(self):
        self.run_job()
        self.observed['1234abcd'].update(state='terminal', main_pid=0, exit_code=0)
        self.now += 10
        result = self.run_job()
        self.assertEqual(result['state'], 'terminal')
        self.assertFalse(result['new_launch'])
        with sqlite3.connect(self.root / 'admission.sqlite3') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 1)
        with self.assertRaisesRegex(Refused, 'actual-start-spacing'):
            self.run_job(job('1234abce'))

    def test_wrong_invocation_and_live_pid_do_not_release(self):
        self.run_job()
        self.observed['1234abcd'].update(state='terminal', main_pid=0, exit_code=0, invocation='b' * 32)
        self.assertEqual(self.run_job()['state'], 'running')
        self.observed['1234abcd'].update(invocation='a' * 32, main_pid=42)
        self.assertEqual(self.run_job()['state'], 'running')

    def test_request_change_refused_and_authority_excludes_competitor(self):
        self.run_job()
        changed = dict(job(), model='other')
        with self.assertRaisesRegex(Refused, 'request-binding-changed'):
            self.run_job(changed)
        with authority(self.root), self.assertRaisesRegex(Refused, 'authority-busy'):
            self.run_job(job('1234abce'))


if __name__ == '__main__':
    unittest.main()
