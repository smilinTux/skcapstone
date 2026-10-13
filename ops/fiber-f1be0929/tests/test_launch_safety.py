"""Regression tests for the two unsafe legacy launch entry points."""
import os
import hashlib
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest

ROOT = Path(os.environ.get("FIBER_CANDIDATE_DIR", "/home/skuser01/.skcapstone/runtime/llm-orch"))
FILL = ROOT / os.environ.get("FIBER_FILL_NAME", "fill-slots.sh")
PROMPT = ROOT / os.environ.get("FIBER_PROMPT_NAME", "make-prompt.sh")


class LaunchSafety(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('FIBER_EXPECT_CGROUP_LIMITS') == '1', 'remote resource check')
    def test_live_cgroup_limits(self):
        relative = next(line.split(':', 2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
        cgroup = Path('/sys/fs/cgroup') / relative.lstrip('/')
        self.assertEqual((cgroup / 'memory.max').read_text().strip(), '1073741824')
        self.assertEqual((cgroup / 'memory.swap.max').read_text().strip(), '268435456')
        self.assertEqual((cgroup / 'pids.max').read_text().strip(), '64')
        quota, period = (cgroup / 'cpu.max').read_text().split()
        self.assertEqual(int(quota), 2 * int(period))

    def test_backup_restore_roundtrip(self):
        backup = Path(__file__).resolve().parents[1] / 'backup'
        expected = {
            'fill-slots.sh': '3e156ba7c7238233277d1bfa06c0470707679f09c65a6763343ed44c0e39b599',
            'make-prompt.sh': '6e99bee5bb90656c875a172e272f58a4bf525f9e76e02eb500d340ec5b5e2e83',
        }
        with tempfile.TemporaryDirectory() as td:
            for name, candidate in [('fill-slots.sh', FILL), ('make-prompt.sh', PROMPT)]:
                target = Path(td) / name
                shutil.copy2(candidate, target)
                shutil.copy2(backup / name, target)
                self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), expected[name])
                self.assertEqual(subprocess.run(['bash', '-n', str(target)]).returncode, 0)
                # Never execute the unsafe backup, including during rollback tests.

    def test_dispatch_gate_behavior(self):
        # No real systemd mutations: exercise the exact entry point with a stub.
        for load, show_rc, start_rc, expected in (
            ('not-found', 0, 0, 78), ('loaded', 1, 0, 78),
            ('loaded', 0, 0, 0), ('loaded', 0, 5, 5),
        ):
            with self.subTest(load=load, show_rc=show_rc, start_rc=start_rc), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                stub = root / 'systemctl'
                stub.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALL_LOG"\n'
                                'if [[ "$2" == show ]]; then\n'
                                '  echo "$LOAD_STATE"\n'
                                '  exit "$SHOW_RC"\nfi\nexit "$START_RC"\n')
                stub.chmod(0o700)
                env = dict(os.environ, PATH=td + ':' + os.environ['PATH'],
                           CALL_LOG=str(root / 'calls'), LOAD_STATE=load,
                           SHOW_RC=str(show_rc), START_RC=str(start_rc))
                result = subprocess.run(['bash', str(FILL)], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stderr)
                calls = (root / 'calls').read_text().splitlines()
                self.assertEqual(len(calls), 2 if load == 'loaded' and show_rc == 0 else 1)
                self.assertTrue(all('skfleet-fiber-dispatch.service' in call for call in calls))

    def test_no_workspace_or_branch_destruction(self):
        text = FILL.read_text()
        self.assertNotIn('rm -rf', text)
        self.assertNotIn('worktree add --quiet --force', text)
        self.assertNotIn(' -B ', text)

    def test_single_managed_dispatch_entry(self):
        text = FILL.read_text()
        self.assertIn('skfleet-fiber-dispatch.service', text)
        self.assertNotIn('herdr agent start', text)

    def test_prompt_refuses_failed_claim(self):
        result = subprocess.run(['bash', str(PROMPT), '1234abcd', 'Bounded test', 'fiber-test-1234abcd'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('release-claim', result.stdout)
        self.assertNotIn('If still refused, proceed', result.stdout)
        self.assertIn('STOP', result.stdout)
        self.assertIn('Never release or replace another', result.stdout)
        self.assertIn('skcapstone coord show 1234abcd', result.stdout)
        self.assertNotIn('/core.json', result.stdout)

    def test_default_identity_works(self):
        result = subprocess.run(['bash', str(PROMPT), '1234abcd'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('fiber-worker-1234abcd', result.stdout)

    def test_invalid_identity_and_card_rejected(self):
        for args in (['../bad'], ['1234abcd', 'scope', 'x;whoami']):
            with self.subTest(args=args):
                result = subprocess.run(['bash', str(PROMPT), *args], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('Sequence:', result.stdout)


if __name__ == '__main__':
    unittest.main()
