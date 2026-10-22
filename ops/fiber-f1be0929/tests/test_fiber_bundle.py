"""Unpublished candidates use the existing protected cache, never a push."""
import base64
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fiber_bundle as bundle
from fiber_admission import Refused


class BundleTest(unittest.TestCase):
    def test_selector_and_typed_review_binding(self):
        selector = {'sha256': 'd' * 64, 'base_revision': 'b' * 40,
                    'tree': 'c' * 40, 'ref': 'refs/heads/work/fiber-1234abcd-aaaaaaaa'}
        job = {'stage': 'review', 'base_revision': 'a' * 40, 'source_bundle': selector}
        row = {'meta': {'base_revision': 'b' * 40, 'candidate_tree': 'c' * 40,
                        'candidate_ref': selector['ref'], 'link_head_revision': 'a' * 40}}
        bundle.verify_binding(row, job)
        for key, value in [('sha256', '../bad'), ('base_revision', 'bad'),
                           ('tree', 'bad'), ('ref', '--unsafe'), ('path', '/host')]:
            with self.subTest(key=key), self.assertRaises(Refused):
                bundle.select(job | {'source_bundle': selector | {key: value}})
        with self.assertRaises(Refused):
            bundle.select(job | {'stage': 'implementation'})
        for key in ('base_revision', 'candidate_tree', 'candidate_ref', 'link_head_revision'):
            with self.subTest(key=key), self.assertRaisesRegex(Refused, 'binding'):
                bundle.verify_binding(row | {'links': {key: 'conflict'}}, job)
        self.assertIsNone(bundle.select({'stage': 'review'}))

    def test_receive_is_no_clobber_bounded_and_does_not_parse_git(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            raw = b'opaque bytes, not executable or parsed by receive'
            digest = hashlib.sha256(raw).hexdigest()
            packet = {'sha256': digest, 'bundle_b64': base64.b64encode(raw).decode()}
            first = bundle.receive(packet, data)
            self.assertEqual(first, bundle.receive(packet, data))
            path = data / 'bundles' / (digest + '.bundle')
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_bytes(b'changed')
            with self.assertRaisesRegex(Refused, 'digest'):
                bundle.receive(packet, data)
            self.assertEqual(path.read_bytes(), b'changed')
            path.unlink()
            target = data / 'untouched'
            target.write_bytes(b'preserve')
            path.symlink_to(target)
            with self.assertRaises((Refused, OSError)):
                bundle.receive(packet, data)
            self.assertEqual(target.read_bytes(), b'preserve')
            for changed in ({'bundle_b64': ''}, {'sha256': 'e' * 64},
                            {'bundle_b64': True}, {'bundle_b64': '!!!!'},
                            {'bundle_b64': 'x' * ((bundle.MAX_BYTES + 2) // 3 * 4 + 1)}):
                with self.subTest(keys=list(changed)), self.assertRaises(Refused):
                    bundle.receive(packet | changed, data)

    def test_real_import_uses_exact_local_bundle_and_preserves_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            source, cache = data / 'source', data / 'cache.git'
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
            calls = []
            def git(*args):
                calls.append(args)
                result = subprocess.run(['git', *map(str, args)], env=env,
                                        capture_output=True, text=True)
                if result.returncode:
                    raise Refused('fixture-git-failed')
                return result.stdout.strip()
            branch = 'work/fiber-1234abcd-aaaaaaaa'
            git('init', '-q', '-b', branch, source)
            def commit(message):
                git('-C', source, '-c', 'user.name=Fixture', '-c', 'user.email=test@source.invalid',
                    '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null',
                    'commit', '--allow-empty', '-qm', message)
            commit('base')
            base = git('-C', source, 'rev-parse', 'HEAD')
            git('init', '--bare', '-q', cache)
            git('-C', cache, 'remote', 'add', 'origin', 'https://example.invalid/repo.git')
            git('-C', cache, 'fetch', '-q', source, base)
            (source / 'change.txt').write_text('synthetic source\n')
            git('-C', source, 'add', 'change.txt')
            commit('candidate')
            head = git('-C', source, 'rev-parse', 'HEAD')
            tree = git('-C', source, 'rev-parse', 'HEAD^{tree}')
            raw = subprocess.run(['git', '-C', str(source), 'bundle', 'create', '-',
                                  branch, '^' + base], env=env, check=True, capture_output=True).stdout
            digest = hashlib.sha256(raw).hexdigest()
            bundle.receive({'sha256': digest, 'bundle_b64': base64.b64encode(raw).decode()}, data)
            job = {'stage': 'review', 'base_revision': head,
                   'source_bundle': {'sha256': digest, 'base_revision': base,
                                     'tree': tree, 'ref': 'refs/heads/' + branch}}
            from fiber_worker import ensure_commit
            calls.clear()
            bundle.import_source(cache, job, data, git, ensure_commit)
            bundle.import_source(cache, job, data, git, ensure_commit)
            self.assertFalse(any('origin' in call for call in calls))
            self.assertEqual(git('-C', cache, 'rev-parse', head + '^{tree}'), tree)
            self.assertEqual(git('-C', cache, 'config', '--get', 'remote.origin.url'),
                             'https://example.invalid/repo.git')
            for change in ({'tree': 'a' * 40}, {'ref': 'refs/heads/wrong'}):
                with self.subTest(change=change), self.assertRaises(Refused):
                    bundle.import_source(cache, job | {'source_bundle': job['source_bundle'] | change},
                                         data, git, ensure_commit)


if __name__ == '__main__':
    unittest.main()
