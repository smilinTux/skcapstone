"""A new host key may read native card metadata, never run an arbitrary shell."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fiber_return_board import command


class ReturnBoardTest(unittest.TestCase):
    def test_exact_controller_read(self):
        value = 'env SKAGENT=pi-fiber-tests-1234abcd SKCAPSTONE_AGENT=pi-fiber-tests-1234abcd /home/skuser01/.skenv/bin/skcapstone coord show 1234abcd --json'
        argv, env = command(value)
        self.assertEqual(argv[1:], ['coord', 'show', '1234abcd', '--json'])
        self.assertEqual(env['SKAGENT'], 'pi-fiber-tests-1234abcd')
        self.assertNotIn('SSH_ORIGINAL_COMMAND', env)

    def test_mutation_shell_and_identity_mismatch_are_refused(self):
        good = 'env SKAGENT=jarvis SKCAPSTONE_AGENT=jarvis /home/skuser01/.skenv/bin/skcapstone coord show 1234abcd --json'
        for value in ('hostname', '', good + '; id', good + ' --home /tmp',
                      good.replace('show', 'claim'), good.replace('SKAGENT=jarvis', 'SKAGENT=other'),
                      good.replace('1234abcd', '../secret'), good.replace('/home/skuser01/.skenv/bin/skcapstone', '/bin/sh')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                command(value)


if __name__ == '__main__':
    unittest.main()
