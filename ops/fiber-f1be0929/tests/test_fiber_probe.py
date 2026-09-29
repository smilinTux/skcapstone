"""Provider attribution must use live route evidence, never worker names."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fiber_probe import process_provider, footer_provider, logical_workers


class ProbeTest(unittest.TestCase):
    def test_explicit_worker_route_matches_reservation_family(self):
        identity = {'SKFLEET_PROVIDER': 'deepseek', 'SKFLEET_MODEL': 'deepseek-flash'}
        self.assertEqual(process_provider('pi', identity), 'deepseek')
        identity['SKFLEET_MODEL'] = 'sk-zai-m'
        self.assertIsNone(process_provider('pi', identity))

    def test_native_codex_known_but_pi_profile_or_name_is_not_evidence(self):
        self.assertEqual(process_provider('codex', {}), 'codex')
        self.assertIsNone(process_provider('pi', {'name': 'ds2-example', 'PI_CODING_AGENT_DIR': 'deepseek'}))

    def test_only_final_live_footer_attributes_legacy_pi(self):
        self.assertEqual(footer_provider('cwd\n1% (auto)    (skgateway) deepseek-flash • high\n'), 'deepseek')
        self.assertEqual(footer_provider('cwd\n1% (auto)    (skgw-zai) sk-zai-m • high'), 'zai')
        self.assertEqual(footer_provider('cwd\n1% (auto)    (skgw-codex) gpt-5.6-sol • high'), 'codex')
        self.assertEqual(footer_provider('(skgw-deepseek) deepseek-flash • high\n🔌 MCP: 4 servers enabled'), 'deepseek')
        for text in ['ds2-example', '(skgateway) deepseek-flash • high\nnew unrecognized footer',
                     '(skgw-zai) deepseek-flash • high', '(unapproved) sk-zai-m • high',
                     '(skgateway) kimi-for-coding • high']:
            with self.subTest(text=text):
                self.assertIsNone(footer_provider(text))

    def test_only_attached_codex_app_servers_are_components(self):
        rows = [{'pid': 10, 'parent_pid': 1, 'runtime': 'codex', 'mode': 'agent-cli'},
                {'pid': 11, 'parent_pid': 10, 'runtime': 'codex', 'mode': 'app-server'},
                {'pid': 12, 'parent_pid': 11, 'runtime': 'codex', 'mode': 'app-server'},
                {'pid': 13, 'parent_pid': 12, 'runtime': 'codex', 'mode': 'exec'},
                {'pid': 14, 'parent_pid': 99, 'runtime': 'codex', 'mode': 'app-server'}]
        self.assertEqual([row['pid'] for row in logical_workers(rows)], [10, 13, 14])


if __name__ == '__main__':
    unittest.main()
