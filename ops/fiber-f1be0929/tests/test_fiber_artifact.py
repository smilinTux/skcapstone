"""Native Git bundles carry exact unpublished candidates without a push."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fiber_worker import ensure_commit
from fiber_admission import Refused, prepare_worktree


def git(*args):
    result = subprocess.run(['git', *map(str, args)], capture_output=True, text=True)
    if result.returncode:
        raise Refused('fixture-git-failed')
    return result.stdout.strip()


class ArtifactTest(unittest.TestCase):
    def test_bundle_import_materializes_exact_candidate_without_origin_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, bundle = root / 'source', root / 'cache.git', root / 'candidate.bundle'
            git('init', '-q', '-b', 'main', source)
            (source / 'candidate.txt').write_text('source-only canary fixture\n')
            git('-C', source, 'add', 'candidate.txt')
            git('-C', source, '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'candidate')
            revision = git('-C', source, 'rev-parse', 'HEAD')
            tree = git('-C', source, 'rev-parse', 'HEAD^{tree}')
            git('-C', source, 'bundle', 'create', bundle, 'main')
            git('init', '--bare', '-q', cache)
            git('-C', cache, 'remote', 'add', 'origin', 'https://example.invalid/not-published.git')
            git('-C', cache, 'bundle', 'verify', bundle)
            git('-C', cache, 'fetch', '--quiet', bundle, 'refs/heads/main')
            def offline_git(*args):
                self.assertNotIn('fetch', args)
                return git(*args)
            ensure_commit(cache, revision, offline_git)
            workspace = root / 'review'
            prepare_worktree(cache, workspace, 'work/independent-review', revision)
            self.assertEqual(git('-C', workspace, 'rev-parse', 'HEAD'), revision)
            self.assertEqual(git('-C', workspace, 'rev-parse', 'HEAD^{tree}'), tree)
            self.assertEqual((workspace / 'candidate.txt').read_text(), 'source-only canary fixture\n')
            blob = git('-C', source, 'rev-parse', 'HEAD:candidate.txt')
            with self.assertRaisesRegex(Refused, 'source-revision-not-commit'):
                ensure_commit(cache, blob, offline_git)

    def test_missing_commit_fetches_only_exact_requested_revision(self):
        calls = []
        def run(*args):
            calls.append(args)
            if len(calls) == 1:
                raise Refused('source-materialization-failed')
            return 'commit' if 'cat-file' in args else ''
        ensure_commit(Path('/fixture/cache.git'), 'a' * 40, run)
        self.assertEqual(calls[1][-4:], ('fetch', '--quiet', 'origin', 'a' * 40))
        self.assertEqual(len(calls), 3)


if __name__ == '__main__':
    unittest.main()
