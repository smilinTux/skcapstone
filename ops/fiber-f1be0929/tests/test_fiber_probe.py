"""Provider attribution must use live route evidence, never worker names."""
from pathlib import Path
import sys
import unittest
import json
import subprocess
import io
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fiber_probe import process_provider, footer_provider, logical_workers
import fiber_probe as probe


class ProbeTest(unittest.TestCase):
    def test_only_exact_codex_pid_updater_is_maintenance(self):
        command = [b'/usr/bin/codex', b'app-server', b'daemon', b'pid-update-loop']
        encoded = b'\0'.join(command) + b'\0'
        self.assertEqual(probe.codex_mode(encoded), 'daemon-pid-updater')
        self.assertNotEqual(probe.codex_mode(encoded[:-1]), 'daemon-pid-updater')
        near_matches = [
            [], command[:-1], command + [b'--unknown'],
            [b'codex', b'app-server'],
            [b'codex', b'app-server', b'daemon', b'start'],
            [b'codex', b'app-server', b'pid-update-loop'],
            [b'codex', b'exec', b'app-server', b'daemon', b'pid-update-loop'],
            [b'codex', b'resume', b'pid-update-loop'],
            [b'codex', b'app-server daemon pid-update-loop'],
            [b'other-program', *command[1:]], command + [b''],
        ]
        for args in near_matches:
            with self.subTest(args=args):
                self.assertNotEqual(probe.codex_mode(b'\0'.join(args) + b'\0'), 'daemon-pid-updater')

    def test_ambiguous_helper_children_are_not_deduplicated(self):
        parent = {'pid': 30, 'parent_pid': 1, 'runtime': 'codex', 'mode': 'app-server'}
        commands = [
            b'codex\0app-server\0daemon\0pid-update-loop\0extra\0',
            b'codex\0app-server\0daemon\0pid-update-loop',
            b'codex\0app-server', b'',
            b'other-program\0app-server\0daemon\0pid-update-loop\0',
            b'codex\0app-server\0daemon\0start\0',
            b'codex\0exec\0app-server\0daemon\0pid-update-loop\0',
        ]
        for command in commands:
            with self.subTest(command=command):
                child = {'pid': 31, 'parent_pid': 30, 'runtime': 'codex',
                         'mode': probe.codex_mode(command)}
                self.assertEqual(child['mode'], 'unknown')
                self.assertEqual(logical_workers([parent, child]), [parent, child])

    def test_node_observation_keeps_real_server_child_not_updater(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = root / 'proc'
            proc.mkdir()
            (proc / 'meminfo').write_text('MemAvailable: 9000000 kB\n')
            for pid, parent, args in [
                (42, 1, b'/usr/bin/codex\0app-server\0daemon\0pid-update-loop\0'),
                (43, 42, b'/usr/bin/codex\0app-server\0'),
                (44, 43, b'/usr/bin/codex\0app-server\0daemon\0pid-update-loop'),
                (45, 43, b''),
                (46, 43, b'/usr/bin/codex\0app-server\0daemon\0pid-update-loop\0extra\0'),
                (47, 43, b'/usr/bin/other\0app-server\0daemon\0pid-update-loop\0'),
            ]:
                directory = proc / str(pid)
                directory.mkdir()
                (directory / 'comm').write_text('codex\n')
                (directory / 'status').write_text(f'PPid:\t{parent}\n')
                (directory / 'environ').write_bytes(b'')
                (directory / 'cmdline').write_bytes(args)
            def mapped_path(*parts):
                path = Path(*parts)
                return proc / path.relative_to('/proc') if path.is_relative_to('/proc') else path
            result = subprocess.CompletedProcess([], 0, 'ActiveState=active\nMainPID=43\n', '')
            with patch.object(probe, 'Path', side_effect=mapped_path) as paths, \
                 patch.object(probe.subprocess, 'run', return_value=result), \
                 patch.object(probe.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"status":"ok"}')):
                paths.home.return_value = root / 'home'
                observed = probe.node_observation()
            self.assertEqual(sorted(row['pid'] for row in observed['workers']), [43, 44, 45, 46, 47])
            self.assertTrue(observed['service_pid_live'])

    def test_updater_exclusion_preserves_servers_and_unknown_workers(self):
        rows = [
            {'pid': 20, 'parent_pid': 1, 'runtime': 'codex', 'mode': 'daemon-pid-updater'},
            {'pid': 21, 'parent_pid': 1, 'runtime': 'codex', 'mode': 'app-server'},
            {'pid': 22, 'parent_pid': 1, 'runtime': 'codex', 'mode': 'resume'},
            {'pid': 23, 'parent_pid': 1, 'runtime': 'pi', 'mode': 'agent-cli'},
            {'pid': 24, 'parent_pid': 1, 'runtime': 'codex', 'mode': 'unknown'},
            {'pid': 25, 'parent_pid': 20, 'runtime': 'codex', 'mode': 'app-server'},
            {'pid': 26, 'parent_pid': 1, 'runtime': 'pi', 'mode': 'daemon-pid-updater'},
        ]
        self.assertEqual([row['pid'] for row in logical_workers(rows)], [21, 22, 23, 24, 25, 26])

    def test_chiwk12_readiness_uses_the_same_migrated_account(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({'host': 'chiwk12'}), '')
        with patch.object(probe.socket, 'gethostname', return_value='chiap08'), \
             patch.object(probe.subprocess, 'run', return_value=result) as run:
            self.assertEqual(probe.probe_host('chiwk12')['host'], 'chiwk12')
        self.assertIn('skuser01@chiwk12', run.call_args.args[0])
        self.assertNotIn('mrarch@chiwk12', run.call_args.args[0])

    def test_host_slots_match_worker_memory_and_cpu_budgets(self):
        memory = {'MemAvailable': 10 * 1024 * 1024}
        self.assertEqual(probe.host_slots('chiap02', memory, 8), 3)
        self.assertEqual(probe.host_slots('chiwk12', memory, 8), 2)
        self.assertEqual(probe.host_slots('chiap02', memory, 2), 1)
        self.assertEqual(probe.host_slots('chiap01', memory, 64), 0)
        self.assertEqual(probe.host_slots('chiap08', memory, 64), 0)
        self.assertEqual(probe.host_slots('chiap02', {'MemAvailable': 3 * 1024 * 1024}, 8), 0)

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
