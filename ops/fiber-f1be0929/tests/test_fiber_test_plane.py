"""Prequalified test requirements bind the actual candidate, never model prose."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fiber_admission import Refused
from fiber_test_plane import qualified_plan, test_plan as make_plan


class TestPlanePlan(unittest.TestCase):
    def setUp(self):
        self.producer = {'job': {'card': '1234abcd', 'card_fingerprint': 'a' * 64,
                                'repository': 'https://example.invalid/repo.git'},
                         'candidate': {'commit': 'b' * 40}}
        self.requirements = {
            'schema': 'skfleet.test-requirements/v1', 'source_card': '1234abcd',
            'source_fingerprint': 'a' * 64, 'repository': 'https://example.invalid/repo.git',
            'qualified_at': 1000, 'expires_at': 1100, 'evidence_sha256': 'c' * 64,
            'host': 'chiap04', 'test_environment': None,
            'checks': [{'id': 'required-unit',
                        'argv': ['python3', '-m', 'unittest', 'discover', '-s', 'tests'],
                        'minimum_passed': 12, 'allowed_skips': []}]}

    def test_exact_head_is_bound_without_mutating_or_aliasing_requirements(self):
        before = copy.deepcopy(self.requirements)
        plan = make_plan(self.requirements, self.producer, 1050)
        self.assertEqual(plan['source_head'], 'b' * 40)
        self.assertEqual(plan['schema'], 'skfleet.test-plan/v1')
        self.assertNotIn('host', plan)
        self.assertNotIn('test_environment', plan)
        self.assertEqual(plan['checks'], before['checks'])
        plan['checks'][0]['argv'].append('changed')
        self.assertEqual(self.requirements, before)

    def test_changed_source_requirements_or_expiry_refuse(self):
        for key, value in [('source_card', 'deadbeef'), ('source_fingerprint', 'f' * 64),
                           ('repository', 'https://other.invalid/repo.git'),
                           ('schema', 'model-report/v1'), ('qualified_at', 1051),
                           ('expires_at', 1050), ('expires_at', 999999),
                           ('evidence_sha256', 'bad'), ('host', 'chiap08'),
                           ('source_head', 'd' * 40), ('qualified_at', True), ('host', [])]:
            requirements = copy.deepcopy(self.requirements)
            requirements[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(Refused):
                make_plan(requirements, self.producer, 1050)
        for now in [True, float('nan'), float('inf'), 999, 1100]:
            with self.subTest(now=now), self.assertRaises(Refused):
                make_plan(self.requirements, self.producer, now)

    def test_unqualified_commands_and_coverage_refuse(self):
        check = self.requirements['checks'][0]
        for key, value in [('argv', ['sh', '-c', 'true']),
                           ('argv', ['python3', '-c', 'print("PASS")']),
                           ('argv', ['python3', '-m', 'pytest', 'tests']),
                           ('argv', ['python3', '-m', 'unittest', '\n']),
                           ('minimum_passed', 0), ('minimum_passed', True),
                           ('allowed_skips', ['same', 'same']), ('id', '../escape'),
                           ('extra', 'unqualified')]:
            requirements = copy.deepcopy(self.requirements)
            requirements['checks'][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(Refused):
                make_plan(requirements, self.producer, 1050)
        for checks in [[], [check, check], [check] * 17]:
            requirements = copy.deepcopy(self.requirements)
            requirements['checks'] = checks
            with self.subTest(check_count=len(checks)), self.assertRaises(Refused):
                make_plan(requirements, self.producer, 1050)

    def test_pytest_requires_exact_qualified_prefix_selector(self):
        self.requirements['checks'][0]['argv'] = ['/opt/test-env/bin/python3.12', '-m', 'pytest', 'tests']
        with self.assertRaises(Refused):
            make_plan(self.requirements, self.producer, 1050)
        self.requirements['test_environment'] = {'lock_sha256': 'd' * 64, 'python': '3.12'}
        self.assertEqual(make_plan(self.requirements, self.producer, 1050)['checks'], self.requirements['checks'])
        self.requirements['test_environment']['venv'] = '/arbitrary/host/path'
        with self.assertRaises(Refused):
            make_plan(self.requirements, self.producer, 1050)

    def test_private_file_reader_refuses_symlink_and_writable_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / '1234abcd.json'
            path.write_text(json.dumps(self.requirements))
            path.chmod(0o600)
            requirements, plan = qualified_plan(path, self.producer, 1050)
            self.assertEqual(requirements, self.requirements)
            self.assertEqual(plan['source_head'], 'b' * 40)
            path.chmod(0o666)
            with self.assertRaises(Refused):
                qualified_plan(path, self.producer, 1050)
            path.chmod(0o600)
            alias = root / 'alias.json'
            alias.symlink_to(path)
            with self.assertRaises((Refused, OSError)):
                qualified_plan(alias, self.producer, 1050)


if __name__ == '__main__':
    unittest.main()
