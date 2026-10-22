"""Only exact current controller-held test evidence can qualify acceptance."""
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


class TestAcceptance(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.producer = {'job': {'card': '1234abcd', 'card_fingerprint': 'a' * 64,
                                'repository': 'https://example.invalid/repo.git'},
                         'candidate': {'commit': 'b' * 40}}
        binding = {'source_card': '1234abcd', 'source_head': 'b' * 40,
                   'source_fingerprint': 'a' * 64, 'repository': self.producer['job']['repository']}
        self.plan = {'schema': 'skfleet.test-plan/v1', **binding,
                     'qualified_at': 1000, 'expires_at': 1100, 'evidence_sha256': 'c' * 64,
                     'checks': [{'id': 'required-unit', 'argv': ['make', 'unit-test'],
                                 'minimum_passed': 12, 'allowed_skips': []}]}
        self.log = self.root / 'required-unit.log'
        self.log.write_text('Synthetic trusted-runner fixture: 12 passed\n')
        self.log.chmod(0o600)
        self.receipt = {'schema': 'skfleet.test-receipt/v1', **binding,
                        'plan_sha256': digest(self.plan), 'started_at': 1001, 'finished_at': 1050,
                        'checks': [{'id': 'required-unit', 'argv': ['make', 'unit-test'],
                                    'passed': 12, 'failed': 0, 'errors': 0, 'skipped': [],
                                    'exit_code': 0, 'host': 'chiap04',
                                    'unit': 'skfleet-test-1234abcd-' + 'd' * 32 + '.service',
                                    'invocation': 'e' * 32, 'log_file': self.log.name,
                                    'log_sha256': hashlib.sha256(self.log.read_bytes()).hexdigest()}]}

    def write(self):
        for name, value in [('test-plan.json', self.plan), ('test-receipt.json', self.receipt)]:
            path = self.root / name
            path.write_text(json.dumps(value))
            path.chmod(0o600)

    def accept(self, now=1060):
        return verified_tests(self.root, self.producer, now=now)

    def test_missing_receipt_waits_and_exact_qualified_receipt_passes(self):
        self.assertIsNone(self.accept())
        self.write()
        result = self.accept()
        self.assertEqual(result['source_head'], 'b' * 40)
        self.assertEqual(result['receipt_sha256'], digest(self.receipt))
        self.assertEqual(result['checks'], ['required-unit'])

    def test_different_head_task_contract_or_repository_refuses(self):
        for key, value in [('source_card', 'bad'), ('source_head', 'f' * 40),
                           ('source_fingerprint', 'f' * 64), ('repository', 'other')]:
            for target in (self.plan, self.receipt):
                original = target[key]
                target[key] = value
                self.write()
                with self.subTest(key=key, target=target['schema']), self.assertRaises(Refused):
                    self.accept()
                target[key] = original

    def test_required_command_cannot_be_changed_omitted_or_replaced(self):
        original = json.loads(json.dumps(self.receipt))
        for change in ('argv', 'id', 'missing', 'extra'):
            self.receipt = json.loads(json.dumps(original))
            if change == 'argv':
                self.receipt['checks'][0]['argv'] = ['true']
            elif change == 'id':
                self.receipt['checks'][0]['id'] = 'easier-check'
            elif change == 'missing':
                self.receipt['checks'] = []
            else:
                self.receipt['checks'].append(self.receipt['checks'][0])
            self.write()
            with self.subTest(change=change), self.assertRaises(Refused):
                self.accept()

    def test_failed_empty_or_boolean_counts_never_pass(self):
        row = self.receipt['checks'][0]
        for key, value in [('passed', 0), ('passed', True), ('failed', 1), ('errors', 1),
                           ('exit_code', 1), ('exit_code', False)]:
            original = row[key]
            row[key] = value
            self.write()
            with self.subTest(key=key, value=value), self.assertRaises(Refused):
                self.accept()
            row[key] = original

    def test_skips_require_exact_prior_qualification(self):
        self.receipt['checks'][0]['skipped'] = ['suite::test_external']
        self.write()
        with self.assertRaisesRegex(Refused, 'unqualified-skip'):
            self.accept()
        self.plan['checks'][0]['allowed_skips'] = ['suite::test_external']
        self.receipt['plan_sha256'] = digest(self.plan)
        self.write()
        self.assertIsNotNone(self.accept())
        self.receipt['checks'][0]['skipped'].append('suite::test_external')
        self.write()
        with self.assertRaisesRegex(Refused, 'unqualified-skip'):
            self.accept()

    def test_expired_and_future_evidence_is_not_current(self):
        self.write()
        for now in (999, 1049, 1100, float('nan'), True):
            with self.subTest(now=now), self.assertRaisesRegex(Refused, 'stale'):
                self.accept(now)

    def test_untrusted_paths_permissions_and_changed_logs_refuse(self):
        self.write()
        self.log.write_text('altered log')
        with self.assertRaisesRegex(Refused, 'digest'):
            self.accept()
        original = self.root / 'retained.log'
        self.log.rename(original)
        self.log.symlink_to(original)
        with self.assertRaises((Refused, OSError)):
            self.accept()
        self.log.unlink()
        original.rename(self.log)
        (self.root / 'test-receipt.json').chmod(0o666)
        with self.assertRaisesRegex(Refused, 'unsafe-file'):
            self.accept()

    def test_execution_identity_and_log_selector_are_bounded(self):
        row = self.receipt['checks'][0]
        for key, value in [('host', 'chiap01'), ('unit', 'unrelated.service'),
                           ('invocation', ''), ('log_file', '../elsewhere.log'),
                           ('log_sha256', 'not-a-hash')]:
            original = row[key]
            row[key] = value
            self.write()
            with self.subTest(key=key), self.assertRaises(Refused):
                self.accept()
            row[key] = original


if __name__ == '__main__':
    unittest.main()
