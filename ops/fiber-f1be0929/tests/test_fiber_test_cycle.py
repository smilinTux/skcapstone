"""Durable test execution recovery and real receipt validation on private fixtures."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fiber_admission import Refused
from fiber_refill import digest
from fiber_test_acceptance import verified_tests
from fiber_test_cycle import ensure_tests
from fiber_test_worker import identity
from test_fiber_test_worker import request_fixture


class TestCycle(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / 'state'; self.state.mkdir(mode=0o700)
        self.directory = self.state / 'lifecycle'; self.directory.mkdir(mode=0o700)
        self.requirements_path = self.root / 'requirements.json'
        fixture = request_fixture()
        self.producer = fixture['producer']; self.requirements = fixture['requirements']
        self.save_requirements()
        self.review = {'source_revision': fixture['source_revision'], 'proposal': {'verdict': 'PASS'}}
        self.calls = []; self.transfers = []; self.guards = 0
        self.terminal = False; self.lost_ack = False; self.invocation = '2' * 32
        self.capacity = False
        self.mutate_packet = lambda packet: None
        self.now = 1000

    def save_requirements(self):
        self.requirements_path.write_text(json.dumps(self.requirements))
        self.requirements_path.chmod(0o600)

    def guard(self):
        self.guards += 1

    def transport(self, mode, request):
        self.calls.append(mode)
        if mode == 'check':
            return {'request_sha256': digest(request), 'state': 'capacity' if self.capacity else 'ready'}
        observed = dict(identity(request), state='terminal' if self.terminal else 'running',
                        invocation=self.invocation, exit_code=0)
        if mode == 'start' and self.lost_ack:
            raise Refused('test-transport-start-unacknowledged')
        if mode != 'collect':
            return observed
        raw = b'Actual test fixture output\n'
        check = {'id': 'unit', 'argv': request['plan']['checks'][0]['argv'], 'passed': 1,
                 'failed': 0, 'errors': 0, 'skipped': [], 'exit_code': 0,
                 'log_file': 'unit.log', 'log_sha256': hashlib.sha256(raw).hexdigest()}
        result = dict(identity(request), invocation=self.invocation, started_at=999, finished_at=1000,
                      checks=[check])
        packet = {'observation': observed, 'result': result, 'logs': {'unit.log': base64.b64encode(raw).decode()}}
        self.mutate_packet(packet)
        return packet

    def cycle(self):
        return ensure_tests(self.directory, self.producer, self.review, state=self.state,
            requirements_path=self.requirements_path, stage_bundle=self.transfers.append,
            guard=self.guard, transport=self.transport, clock=lambda: self.now)

    def test_missing_requirements_never_launch_or_create_plan(self):
        self.requirements_path.unlink()
        self.assertIsNone(self.cycle())
        self.assertEqual(self.calls, [])
        self.assertFalse((self.directory / 'test-plan.json').exists())

    def test_start_once_collect_verify_and_reuse_without_rerun(self):
        self.assertIsNone(self.cycle())
        self.assertEqual(self.calls, ['check', 'start']); self.assertEqual(len(self.transfers), 1)
        self.terminal = True
        accepted = self.cycle()
        self.assertEqual(accepted['source_head'], self.producer['candidate']['commit'])
        self.assertEqual(self.calls, ['check', 'start', 'observe', 'collect', 'observe'])
        self.assertEqual(verified_tests(self.directory, self.producer, now=1000), accepted)
        self.assertFalse((self.state / 'test-reservations' / 'chiap04.json').exists())
        before = list(self.calls)
        self.assertEqual(self.cycle(), accepted); self.assertEqual(self.calls, before)

    def test_lost_launch_ack_observes_original_intent_without_relaunch(self):
        self.lost_ack = True
        with self.assertRaises(Refused): self.cycle()
        intent = (self.directory / 'test-execution-intent.json').read_bytes()
        self.terminal = True
        self.assertIsNotNone(self.cycle())
        self.assertEqual(self.calls.count('start'), 1)
        self.assertEqual((self.directory / 'test-execution-intent.json').read_bytes(), intent)

    def test_lost_acceptance_ack_recollects_without_relaunch(self):
        self.terminal = True
        accepted = self.cycle()
        (self.directory / 'test-accepted.json').unlink()
        self.assertEqual(self.cycle(), accepted)
        self.assertEqual(self.calls.count('start'), 1)
        self.assertEqual(self.calls.count('collect'), 2)

    def test_changed_retained_result_cannot_reuse_success(self):
        self.terminal = True; self.cycle()
        path = self.directory / 'test-execution-result.json'
        result = json.loads(path.read_text()); result['invocation'] = '9' * 32
        path.write_text(json.dumps(result))
        with self.assertRaises(Refused): self.cycle()

    def test_invocation_change_refuses_before_collection(self):
        self.cycle(); self.invocation = '3' * 32; self.terminal = True
        with self.assertRaises(Refused): self.cycle()
        self.assertNotIn('collect', self.calls)
        self.assertFalse((self.directory / 'test-receipt.json').exists())

    def test_changed_claim_candidate_review_or_requirements_refuses(self):
        self.cycle()
        originals = copy.deepcopy((self.producer, self.review, self.requirements))
        for which, key, value in [('producer', 'claim_revision', '4' * 32),
                                  ('review', 'source_revision', '5' * 64),
                                  ('requirements', 'evidence_sha256', '6' * 64)]:
            self.producer, self.review, self.requirements = copy.deepcopy(originals)
            getattr(self, which)[key] = value; self.save_requirements()
            with self.subTest(which=which), self.assertRaises(Refused): self.cycle()
        self.assertEqual(self.calls, ['check', 'start'])

    def test_failed_counts_unqualified_skips_and_tampered_logs_never_verify(self):
        for key, value in [('failed', 1), ('exit_code', 1), ('passed', 0), ('skipped', ['unqualified']),
                           ('log_sha256', '0' * 64)]:
            with self.subTest(key=key):
                # Independent private namespace for each retained rejected report.
                directory = self.state / key; directory.mkdir(mode=0o700); self.directory = directory
                self.mutate_packet = lambda packet, k=key, v=value: packet['result']['checks'][0].update({k: v})
                self.terminal = True
                with self.assertRaises(Refused): self.cycle()
                if (directory / 'test-receipt.json').exists():
                    with self.assertRaises(Refused): verified_tests(directory, self.producer, now=1000)
                else:
                    self.assertIsNone(verified_tests(directory, self.producer, now=1000))
                # Test fixture releases its own reservation only for next negative case.
                (self.state / 'test-reservations' / 'chiap04.json').unlink()

    def test_other_host_reservation_prevents_any_launch(self):
        root = self.state / 'test-reservations'; root.mkdir(mode=0o700)
        path = root / 'chiap04.json'; path.write_text('{"request_sha256":"other"}'); path.chmod(0o600)
        self.assertIsNone(self.cycle()); self.assertEqual(self.calls, [])
        self.assertFalse((self.directory / 'test-execution-intent.json').exists())

    def test_capacity_wait_does_not_consume_launch_intent(self):
        self.capacity = True
        self.assertIsNone(self.cycle())
        self.assertEqual(self.calls, ['check'])
        self.assertFalse((self.directory / 'test-execution-intent.json').exists())
        self.capacity = False
        self.assertIsNone(self.cycle())
        self.assertEqual(self.calls, ['check', 'check', 'start'])

    def test_hold_guard_prevents_start(self):
        def held(): raise Refused('admission-held')
        self.guard = held
        with self.assertRaises(Refused): self.cycle()
        self.assertEqual(self.calls, [])

    def test_expiry_after_run_and_symlink_policy_refuse(self):
        self.cycle(); self.now = 1100
        with self.assertRaises(Refused): self.cycle()
        self.now = 1000
        alias = self.root / 'alias.json'; alias.symlink_to(self.requirements_path); self.requirements_path = alias
        with self.assertRaises((Refused, OSError)): self.cycle()
        self.assertEqual(self.calls, ['check', 'start'])


if __name__ == '__main__':
    unittest.main()
