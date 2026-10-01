"""Terminal exports wait for exact execution evidence on the authority."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fiber_control as control
from fiber_admission import Refused
from test_fiber_worker import request


class ExecutionLinkTest(unittest.TestCase):
    def run_record(self, stage):
        """Build the same persisted identity used by both lifecycle exports."""
        item = request()
        item['job']['stage'] = stage
        if stage == 'review':
            item['job']['producer_provider'] = 'zai'
        return dict(item['receipt'], invocation='c' * 32, binding=json.dumps(item['job']))

    def test_missing_or_other_execution_link_refuses_both_stages(self):
        for stage in ('implementation', 'review'):
            run = self.run_record(stage)
            with tempfile.TemporaryDirectory() as td, patch.object(control, 'STATE', Path(td)):
                expected = str(Path(td) / 'controls' / run['token'] / 'execution.json')
                for links in ({}, {'execution_record': None},
                              {'execution_record': expected.replace(run['token'], 'b' * 32)},
                              {'execution_record': expected + '.old'}):
                    with self.subTest(stage=stage, links=links), \
                         patch.object(control, 'remote', return_value={'accepted': False}), \
                         patch.object(control, 'cards', return_value={run['card']: {'links': links}}), \
                         self.assertRaisesRegex(Refused, 'candidate-execution-link-not-authoritative'):
                        control.collect_candidate(run)

    def test_delayed_replication_retries_then_returns_same_proof_both_stages(self):
        for stage in ('implementation', 'review'):
            run = self.run_record(stage)
            proof = {'accepted': False, 'execution_sha256': 'f' * 64}
            events = []
            with tempfile.TemporaryDirectory() as td, patch.object(control, 'STATE', Path(td)):
                expected = str(Path(td) / 'controls' / run['token'] / 'execution.json')
                rows = iter([{'links': {}}, {'links': {'execution_record': expected}}])
                def export(mode, payload):
                    self.assertEqual(mode, 'candidate')
                    self.assertEqual(payload['receipt']['invocation'], run['invocation'])
                    events.append('terminal-export')
                    return proof
                def authority(card_ids):
                    self.assertEqual(card_ids, [run['card']])
                    events.append('authority-read')
                    return {run['card']: next(rows)}
                with self.subTest(stage=stage), patch.object(control, 'remote', side_effect=export), \
                     patch.object(control, 'cards', side_effect=authority), \
                     patch.object(control.subprocess, 'run') as native_write:
                    with self.assertRaisesRegex(Refused, 'candidate-execution-link-not-authoritative'):
                        control.collect_candidate(run)
                    self.assertIs(control.collect_candidate(run), proof)
                    self.assertEqual(events, ['terminal-export', 'authority-read'] * 2)
                    native_write.assert_not_called()

    def test_invalid_token_refuses_before_export_or_board_read(self):
        run = self.run_record('implementation')
        run['token'] = '../other'
        with patch.object(control, 'remote') as remote, patch.object(control, 'cards') as cards, \
             self.assertRaisesRegex(Refused, 'invalid-job-identity'):
            control.collect_candidate(run)
        remote.assert_not_called()
        cards.assert_not_called()

    def test_remote_refusal_is_preserved_without_authority_link_read(self):
        with patch.object(control, 'remote', side_effect=Refused('remote-candidate-failed')), \
             patch.object(control, 'cards') as cards, \
             self.assertRaisesRegex(Refused, 'remote-candidate-failed'):
            control.collect_candidate(self.run_record('review'))
        cards.assert_not_called()

    def test_authority_read_failure_never_returns_terminal_proof(self):
        with patch.object(control, 'remote', return_value={'accepted': False}), \
             patch.object(control, 'cards', side_effect=Refused('board-read-failed')), \
             self.assertRaisesRegex(Refused, 'board-read-failed'):
            control.collect_candidate(self.run_record('implementation'))


if __name__ == '__main__':
    unittest.main()
