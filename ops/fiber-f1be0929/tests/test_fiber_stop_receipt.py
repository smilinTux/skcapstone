"""Stopped services require exact manager evidence, not an absent unit alone."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fiber_worker as worker
from test_fiber_worker import request


class StopReceiptTest(unittest.TestCase):
    def setUp(self):
        self.receipt = dict(request()['receipt'], invocation='b' * 32)
        self.values = {'Id': self.receipt['unit'], 'Description': self.receipt['unit'],
                       'LoadState': 'not-found', 'ActiveState': 'inactive', 'SubState': 'dead',
                       'MainPID': '0', 'InvocationID': '', 'ExecMainCode': '0',
                       'ExecMainStatus': '0', 'ControlGroup': ''}
        uid = str(os.getuid())
        self.event = {'MESSAGE_ID': '9d1aaa27d60140bd96365438aad20286', 'JOB_RESULT': 'done',
                      'USER_UNIT': self.receipt['unit'], 'USER_INVOCATION_ID': 'b' * 32,
                      '_BOOT_ID': 'c' * 32, '_UID': uid, '_EXE': '/usr/lib/systemd/systemd',
                      '_SYSTEMD_CGROUP': f'/user.slice/user-{uid}.slice/user@{uid}.service/init.scope',
                      '__CURSOR': 'synthetic-exact-stop', '__REALTIME_TIMESTAMP': '123456789'}

    def observe(self, *, events=None, populated=False, changes=None):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == 'journalctl':
                rows = [self.event] if events is None else events
                return subprocess.CompletedProcess(argv, 0, '\n'.join(json.dumps(row) for row in rows), '')
            values = dict(self.values)
            if changes and len(calls) > 1:
                values.update(changes)
            return subprocess.CompletedProcess(argv, 1 if values['LoadState'] == 'not-found' else 0,
                '\n'.join(k + '=' + v for k, v in values.items()), '')
        def read(path, *args, **kwargs):
            if str(path) == '/proc/sys/kernel/random/boot_id':
                return 'c' * 32
            if path.name == 'cgroup.events':
                return 'populated ' + ('1' if populated else '0') + '\nfrozen 0\n'
            raise AssertionError('unexpected read ' + str(path))
        with patch.object(worker.subprocess, 'run', side_effect=run), \
             patch.object(Path, 'read_text', read), patch.object(Path, 'exists', return_value=True):
            result = worker.read_unit(self.receipt)
        return result, calls

    def test_exact_manager_stop_recovers_without_inventing_exit_code(self):
        result, calls = self.observe()
        self.assertEqual(result['state'], 'stopped')
        self.assertIsNone(result['exit_code'])
        self.assertEqual(result['main_pid'], 0)
        self.assertEqual(result['invocation'], 'b' * 32)
        self.assertEqual(result['stop_receipt']['cursor'], 'synthetic-exact-stop')
        self.assertEqual(calls[-1][0], 'systemctl')

    def test_missing_or_ambiguous_evidence_stays_unknown(self):
        for rows in ([], [self.event, self.event]):
            with self.subTest(rows=rows):
                self.assertEqual(self.observe(events=rows)[0]['state'], 'unknown')

    def test_wrong_generation_boot_sender_or_stop_kind_stays_unknown(self):
        for key in ('USER_UNIT', 'USER_INVOCATION_ID', '_BOOT_ID', '_UID', '_EXE',
                    '_SYSTEMD_CGROUP', 'MESSAGE_ID', 'JOB_RESULT', '__CURSOR'):
            event = dict(self.event, **{key: ''})
            with self.subTest(key=key):
                self.assertEqual(self.observe(events=[event])[0]['state'], 'unknown')

    def test_live_group_or_restarted_unit_stays_unknown(self):
        self.assertEqual(self.observe(populated=True)[0]['state'], 'unknown')
        self.assertEqual(self.observe(changes={'ActiveState': 'active', 'MainPID': '42'})[0]['state'], 'unknown')

    def test_no_known_invocation_never_queries_journal(self):
        del self.receipt['invocation']
        result, calls = self.observe()
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
