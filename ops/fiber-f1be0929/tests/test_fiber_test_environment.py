"""Refusal and read-only mount tests for optional qualified test environments.

These tests are stdlib-only and never touch a real host path. The optional
selector binds a queue job to an operator-built, read-only Python dependency
prefix; every absent, malformed, symlinked or mismatched case must fail closed
before any model execution.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fiber_sandbox as sandbox
import fiber_test_environment as testenv
from fiber_admission import Refused

LOCK = 'a' * 64
REPO = 'https://example.invalid/approved/repo.git'


def manifest(lock=LOCK, repository=REPO, ready=True, source_dirs=None):
    return {'schema': 'schema1', 'repository': repository, 'lock_sha256': lock,
            'python': 'python3.12', 'ready': ready,
            'source_dirs': list(source_dirs or ['src', 'tests'])}


class SelectorTest(unittest.TestCase):
    def test_absent_selector_is_backward_compatible(self):
        self.assertIsNone(testenv.select(None))
        self.assertIsNone(testenv.select({}))
        self.assertIsNone(testenv.select({'test_environment': None}))

    def test_valid_selector_returns_exact_fields_only(self):
        selected = testenv.select({'test_environment': {'lock_sha256': LOCK, 'python': '3.12'}})
        self.assertEqual(selected, {'lock_sha256': LOCK, 'python': '3.12'})

    def test_malformed_selectors_are_refused(self):
        bad = [
            'not-a-dict',
            {'python': '3.12'},
            {'lock_sha256': LOCK},
            {'lock_sha256': 'A' * 64, 'python': '3.12'},
            {'lock_sha256': LOCK[:-1], 'python': '3.12'},
            {'lock_sha256': LOCK, 'python': '3.11'},
            {'lock_sha256': LOCK, 'python': '3.12', 'venv': '/tmp/evil'},
            {'lock_sha256': LOCK, 'python': 3.12},
        ]
        for selector in bad:
            with self.subTest(selector=selector), self.assertRaises(Refused):
                testenv.select({'test_environment': selector})


class ManifestTest(unittest.TestCase):
    def _install(self, root, document, *, symlink_manifest=False, symlink_tree=False):
        artifact = root / 'test-environments' / LOCK
        (artifact / 'venv/bin').mkdir(parents=True)
        (artifact / 'venv/bin/python3.12').write_text('')
        if symlink_tree:
            real = root / 'real'
            (real / 'venv/bin').mkdir(parents=True)
            if artifact.exists():
                import shutil
                shutil.rmtree(artifact)
            artifact.symlink_to(real, target_is_directory=True)
        else:
            (artifact / 'manifest.json').write_text(json.dumps(document))
        return artifact

    def test_missing_manifest_refuses(self):
        with tempfile.TemporaryDirectory() as td, patch.object(testenv, 'DATA', Path(td)):
            with self.assertRaisesRegex(Refused, 'test-environment-unavailable'):
                testenv.qualify({'lock_sha256': LOCK, 'python': '3.12'}, REPO)

    def test_malformed_and_unready_and_wrong_repo_manifests_refuse(self):
        cases = [
            ('not json', 'test-environment-manifest-invalid'),
            (json.dumps({'schema': 'schema2', 'repository': REPO, 'lock_sha256': LOCK,
                         'python': 'python3.12', 'ready': True, 'source_dirs': []}),
             'test-environment-manifest-mismatch'),
            (json.dumps(manifest(repository='https://evil.invalid/repo.git')),
             'test-environment-repository-mismatch'),
            (json.dumps(manifest(ready=False)), 'test-environment-not-ready'),
            (json.dumps(manifest(lock='b' * 64)), 'test-environment-manifest-mismatch'),
            (json.dumps(manifest(source_dirs=['../escape'])), 'test-environment-source-dir'),
            (json.dumps(manifest(source_dirs=['/abs'])), 'test-environment-source-dir'),
            (json.dumps(manifest(source_dirs=['a/../../b'])), 'test-environment-source-dir'),
            (json.dumps(dict(manifest(), extra='x')), 'test-environment-manifest-mismatch'),
        ]
        for document, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as td, \
                 patch.object(testenv, 'DATA', Path(td)):
                artifact = Path(td) / 'test-environments' / LOCK
                (artifact).mkdir(parents=True)
                (artifact / 'manifest.json').write_text(document)
                with self.assertRaisesRegex(Refused, reason):
                    testenv.qualify({'lock_sha256': LOCK, 'python': '3.12'}, REPO)

    def test_symlinked_artifact_or_manifest_refuses(self):
        with tempfile.TemporaryDirectory() as td, patch.object(testenv, 'DATA', Path(td)):
            root = Path(td)
            real = root / 'real'
            (real / 'venv/bin').mkdir(parents=True)
            (real / 'manifest.json').write_text(json.dumps(manifest()))
            artifact = root / 'test-environments' / LOCK
            artifact.parent.mkdir(parents=True)
            artifact.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(Refused, 'test-environment-path'):
                testenv.qualify({'lock_sha256': LOCK, 'python': '3.12'}, REPO)

    def test_symlinked_manifest_refuses(self):
        with tempfile.TemporaryDirectory() as td, patch.object(testenv, 'DATA', Path(td)):
            root = Path(td)
            artifact = root / 'test-environments' / LOCK
            (artifact / 'venv/bin').mkdir(parents=True)
            real = root / 'manifest-real.json'
            real.write_text(json.dumps(manifest()))
            (artifact / 'manifest.json').symlink_to(real)
            with self.assertRaisesRegex(Refused, 'test-environment-path'):
                testenv.qualify({'lock_sha256': LOCK, 'python': '3.12'}, REPO)

    def test_missing_venv_bin_refuses(self):
        with tempfile.TemporaryDirectory() as td, patch.object(testenv, 'DATA', Path(td)):
            artifact = Path(td) / 'test-environments' / LOCK
            artifact.mkdir(parents=True)
            (artifact / 'manifest.json').write_text(json.dumps(manifest()))
            with self.assertRaisesRegex(Refused, 'test-environment-unavailable'):
                testenv.qualify({'lock_sha256': LOCK, 'python': '3.12'}, REPO)

    def test_valid_manifest_returns_read_only_plan(self):
        with tempfile.TemporaryDirectory() as td, patch.object(testenv, 'DATA', Path(td)):
            artifact = self._install(Path(td), manifest())
            plan = testenv.qualify({'lock_sha256': LOCK, 'python': '3.12'}, REPO)
            self.assertEqual(plan['venv'], str(artifact / 'venv'))
            self.assertEqual(plan['lock_sha256'], LOCK)
            self.assertEqual(plan['source_dirs'], ['src', 'tests'])


class LockBindingTest(unittest.TestCase):
    def test_matching_lock_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / 'uv.lock'
            lock.write_text('content\n')
            digest = hashlib.sha256(lock.read_bytes()).hexdigest()
            self.assertEqual(testenv.verify_lock(lock, digest), digest)

    def test_absent_or_mismatched_lock_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / 'uv.lock'
            with self.assertRaisesRegex(Refused, 'test-environment-lock-missing'):
                testenv.verify_lock(lock, LOCK)
            lock.write_text('content\n')
            with self.assertRaisesRegex(Refused, 'test-environment-lock-mismatch'):
                testenv.verify_lock(lock, LOCK)

    def test_symlinked_lock_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / 'real.lock'
            real.write_text('content\n')
            link = Path(td) / 'uv.lock'
            link.symlink_to(real)
            with self.assertRaisesRegex(Refused, 'test-environment-lock-missing'):
                testenv.verify_lock(link, hashlib.sha256(real.read_bytes()).hexdigest())


class SandboxMountTest(unittest.TestCase):
    def test_no_environment_keeps_current_path_and_no_mount(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            argv = sandbox.command(root / 'work', root / 'control', root / 'pi', ['/usr/bin/true'])
            self.assertIn('/opt/node/bin:/usr/bin:/bin', argv)
            self.assertNotIn('/opt/test-env', ' '.join(argv))
            self.assertFalse(any(a.endswith('/opt/test-env') for a in argv))

    def test_environment_binds_venv_read_only_and_prepends_bin(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            venv = root / 'venv'
            (venv / 'bin').mkdir(parents=True)
            argv = sandbox.command(root / 'work', root / 'control', root / 'pi', ['/usr/bin/true'],
                                   test_environment={'venv': str(venv), 'source_dirs': ['src', 'tests']})
            binds = [argv[i + 1:i + 3] for i, arg in enumerate(argv) if arg == '--ro-bind']
            self.assertIn([str(venv), '/opt/test-env'], binds)
            path = argv[argv.index('PATH') + 1] if 'PATH' in argv else None
            # PATH is passed as the value after --setenv PATH.
            setenv = [argv[i + 1:i + 3] for i, arg in enumerate(argv) if arg == '--setenv']
            environment = {name: value for name, value in setenv}
            self.assertTrue(environment['PATH'].startswith('/opt/test-env/bin:'))
            self.assertIn('/opt/node/bin:/usr/bin:/bin', environment['PATH'])
            self.assertEqual(environment['PYTHONPATH'], '/work/src:/work/tests')

    def test_environment_never_grants_writable_mount(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            venv = root / 'venv'
            (venv / 'bin').mkdir(parents=True)
            argv = sandbox.command(root / 'work', root / 'control', root / 'pi', ['/usr/bin/true'],
                                   test_environment={'venv': str(venv), 'source_dirs': ['src']})
            writable = [argv[i + 1:i + 3] for i, arg in enumerate(argv) if arg in {'--bind', '--dev-bind'}]
            # The one writable bind is the task repository /work, unchanged.
            self.assertEqual(writable, [[str(root / 'work'), '/work']])
            read_only = [argv[i + 1:i + 3] for i, arg in enumerate(argv) if arg == '--ro-bind']
            self.assertIn([str(venv), '/opt/test-env'], read_only)

    def test_environment_probe_qualifies_private_prefix_inside_sandbox(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            venv = root / 'venv'
            (venv / 'bin').mkdir(parents=True)
            with patch.object(sandbox.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
                sandbox.environment_probe(root / 'work', root / 'control', root / 'pi',
                                          {'venv': str(venv), 'source_dirs': ['src']})
            argv = run.call_args.args[0]
            self.assertEqual(argv[0], '/usr/bin/bwrap')
            self.assertIn('--unshare-all', argv)
            self.assertIn('/opt/test-env/bin/python3.12', argv)
            self.assertIn([str(venv), '/opt/test-env'],
                          [argv[i + 1:i + 3] for i, arg in enumerate(argv) if arg == '--ro-bind'])

    def test_environment_probe_fails_closed_on_import_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            venv = root / 'venv'
            (venv / 'bin').mkdir(parents=True)
            with patch.object(sandbox.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
                with self.assertRaisesRegex(Refused, 'test-environment-imports-failed'):
                    sandbox.environment_probe(root / 'work', root / 'control', root / 'pi',
                                              {'venv': str(venv), 'source_dirs': ['src']})

    def test_environment_probe_is_a_noop_without_environment(self):
        self.assertTrue(sandbox.environment_probe('/work', '/control', '/pi', None))


if __name__ == '__main__':
    unittest.main()
