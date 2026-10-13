"""External verifier must not accept a missing or nonterminal board read."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get('FIBER_VERIFIER_TEST_SCRIPT', str(ROOT / 'verify-card.candidate.sh')))


class VerifierTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.git('init', '-q', str(self.repo))
        self.commit('base')
        self.base = self.git('-C', str(self.repo), 'rev-parse', 'HEAD').stdout.strip()
        self.commit('candidate')
        self.evidence = self.repo / 'docs/evidence/agents/1234abcd/COMPLETION-EVIDENCE.md'
        self.evidence.parent.mkdir(parents=True)
        self.evidence.write_text('Exact fixture evidence\n')
        binary = self.root / 'bin'
        binary.mkdir()
        stub = binary / 'skcapstone'
        stub.write_text('#!/usr/bin/env python3\nimport os,sys\n'
                        'assert sys.argv[1:] == ["coord","show","1234abcd","--json"]\n'
                        'if os.environ.get("TEST_CLI_FAIL"): sys.exit(70)\n'
                        'print(os.environ["TEST_CARD_JSON"])\n')
        stub.chmod(0o700)
        # Test-only: do not let the login shell's BASH_ENV rewrite the stub PATH.
        self.env = dict(os.environ, BASH_ENV='/dev/null', PATH=str(binary) + ':' + os.environ['PATH'])

    def git(self, *args):
        return subprocess.run(['git', *args], check=True, capture_output=True, text=True)

    def commit(self, message):
        self.git('-C', str(self.repo), '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                 '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '--allow-empty', '-qm', message)

    def verify(self, row, base=None):
        return subprocess.run(['bash', str(SCRIPT), '1234abcd', str(self.repo), base or self.base],
                              env=dict(self.env, TEST_CARD_JSON=json.dumps(row)), capture_output=True, text=True, timeout=10)

    def test_board_identity_and_state_are_mandatory(self):
        for status, passes in [('review', True), ('done', True), ('doing', False), ('backlog', False)]:
            result = self.verify({'id': '1234abcd', 'status': status, 'links': {}})
            with self.subTest(status=status):
                self.assertEqual(result.returncode == 0, passes, result.stdout)
        self.assertNotEqual(self.verify({'id': 'deadbeef', 'status': 'done'}).returncode, 0)
        self.assertNotEqual(self.verify(None).returncode, 0)
        self.env['TEST_CLI_FAIL'] = '1'
        result = self.verify({'id': '1234abcd', 'status': 'done'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('board read failed', result.stderr)

    def test_unrelated_candidate_history_is_refused(self):
        self.git('-C', str(self.repo), 'checkout', '--orphan', 'unrelated')
        self.commit('unrelated root')
        result = self.verify({'id': '1234abcd', 'status': 'review'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('does not descend from base', result.stderr)

    def test_missing_empty_or_external_evidence_and_bad_base_refuse(self):
        row = {'id': '1234abcd', 'status': 'review'}
        self.assertNotEqual(self.verify(row, 'nonexistent-base').returncode, 0)
        self.evidence.write_text('')
        self.assertNotEqual(self.verify(row).returncode, 0)
        self.evidence.unlink()
        outside = self.root / 'outside.md'
        outside.write_text('not this worktree')
        self.evidence.symlink_to(outside)
        self.assertNotEqual(self.verify(row).returncode, 0)

    def test_blocked_requires_matching_board_record(self):
        head = self.git('-C', str(self.repo), 'rev-parse', 'HEAD').stdout.strip()
        self.evidence.write_text('BLOCKED: cannot satisfy card\n')
        row = {'id': '1234abcd', 'status': 'ready', 'links': {}}
        self.assertNotEqual(self.verify(row, head).returncode, 0)
        row['links']['verdict'] = 'BLOCKED blocked_on=capability referent=ac:1'
        result = self.verify(row, head)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('not successful completion', result.stdout)


if __name__ == '__main__':
    unittest.main()
