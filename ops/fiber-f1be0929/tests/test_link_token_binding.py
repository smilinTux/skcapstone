"""Exercise the exact systemd shell body with only synthetic token bytes."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

CONFIG = Path(__file__).resolve().parents[1] / 'skfleet-link-producer-9173d235.conf'


class LinkTokenBinding(unittest.TestCase):
    def run_binding(self, value):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / '.config/skgit/token'
            token.parent.mkdir(parents=True)
            if value is not None:
                token.write_text(value)
                token.chmod(0o600)
            binary = root / '.skenv/bin/python3'
            binary.parent.mkdir(parents=True)
            binary.write_text('#!/bin/sh\n'
                              'test "$SKFLEET_SKGIT_READ_TOKEN" = "synthetic-token" || exit 71\n'
                              'test "$#" = 1 || exit 72\n'
                              'test "$1" = "$EXPECTED_PRODUCER" || exit 73\n'
                              'printf "producer started\\n"\n')
            binary.chmod(0o700)
            line = next(line for line in CONFIG.read_text().splitlines()
                        if line.startswith('ExecStart=/bin/sh'))
            # systemd unquotes the command and expands %%h and escaped dollars.
            argv = shlex.split(line.removeprefix('ExecStart=').replace('%h', str(root)))
            argv = [part.replace('$$', '$') for part in argv]
            self.assertNotIn('synthetic-token', ' '.join(argv))
            return subprocess.run(argv, env={'PATH': '/usr/bin:/bin',
                'EXPECTED_PRODUCER': str(root / '.skenv/bin/skfleet-link-producer.py')},
                capture_output=True, text=True, timeout=5)

    def test_existing_token_with_or_without_final_newline(self):
        for value in ('synthetic-token\n', 'synthetic-token'):
            with self.subTest(value=value):
                result = self.run_binding(value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'producer started\n')
                self.assertNotIn('synthetic-token', result.stdout + result.stderr)

    def test_missing_or_empty_token_refuses_before_producer(self):
        for value in (None, ''):
            with self.subTest(value=value):
                result = self.run_binding(value)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('producer started', result.stdout)

    def test_repository_scope_preserved_and_private_repo_added(self):
        line = next(line for line in CONFIG.read_text().splitlines()
                    if line.startswith('Environment=SKFLEET_LINK_REPOSITORIES='))
        self.assertEqual(line.split('=', 2)[2].split(','), [
            'smilinTux/skcapstone', 'smilinTux/skdashboard', 'smilinTux/skworld',
            'smilinTux/sk-standards', 'https://skgit.skstack01.douno.it/smilinTux/sklegal'])


if __name__ == '__main__':
    unittest.main()
