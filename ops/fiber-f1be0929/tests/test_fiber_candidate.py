"""Terminal evidence collection must not turn worker claims into acceptance."""
import base64
from contextlib import ExitStack
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
import fiber_candidate as candidate
from fiber_admission import Refused
from test_fiber_worker import request


class CandidateTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.state, self.data = root / 'state', root / 'data'
        self.item = request()
        self.item['receipt']['invocation'] = 'c' * 32
        self.saved = json.loads(json.dumps(self.item))
        del self.saved['receipt']['invocation']
        self.saved['receipt']['reserved_at'] = 1000.0
        self.saved['job']['claimable'] = True
        self.saved['claim'] = {'authority': 'chiap08', 'owner': 'fiber-test', 'revision': 'd' * 32}
        self.token = self.item['receipt']['token']
        self.work = self.data / 'workspaces' / ('1234abcd-' + self.token)
        self.work.mkdir(parents=True)
        controls = self.state / 'controls' / self.token
        controls.mkdir(parents=True)
        requests = self.state / 'requests'
        requests.mkdir()
        (requests / (self.token + '.json')).write_text(json.dumps(self.saved))
        self.execution = {'card': '1234abcd', 'claim_revision': 'd' * 32,
            'token': self.token, 'host': 'chiap02', 'workspace': str(self.work),
            'base_revision': 'b' * 40, 'exit_code': 0, 'gateway_requests': 3,
            'gateway_failures': [], 'accepted': False, 'state': 'awaiting-external-verification'}
        self.execution_path = controls / 'execution.json'
        self.execution_path.write_text(json.dumps(self.execution))
        for directory in (self.state, requests, self.state / 'controls', controls,
                          self.data, self.data / 'workspaces', self.work):
            directory.chmod(0o700)
        (requests / (self.token + '.json')).chmod(0o600)
        self.execution_path.chmod(0o600)
        self.observed = dict(self.item['receipt'], state='terminal', main_pid=0, exit_code=0)
        self.packet = {'commit': 'e' * 40, 'tree': 'f' * 40,
            'ref': 'refs/heads/work/fiber-1234abcd-' + self.token[:8],
            'evidence_sha256': hashlib.sha256(b'evidence').hexdigest(),
            'evidence_b64': base64.b64encode(b'evidence').decode()}
        self.stack.enter_context(patch.object(candidate.worker, 'STATE', self.state))
        self.stack.enter_context(patch.object(candidate.worker, 'DATA', self.data))
        self.stack.enter_context(patch.object(candidate.socket, 'gethostname', return_value='chiap02'))
        self.claim = self.stack.enter_context(patch.object(candidate.worker, 'verify_claim', return_value={}))
        self.observe = self.stack.enter_context(patch.object(candidate.worker, 'read_unit', side_effect=lambda _: self.observed))
        self.real_inspect = candidate.inspect_workspace
        self.capture = self.stack.enter_context(patch.object(candidate, 'inspect_workspace', return_value=self.packet))

    def test_exact_terminal_run_returns_bound_evidence_not_acceptance(self):
        result = candidate.collect_candidate(self.item)
        self.assertFalse(result['accepted'])
        self.assertEqual(result['candidate'], self.packet)
        self.assertEqual(result['claim_revision'], 'd' * 32)
        self.assertEqual(result['invocation'], 'c' * 32)
        self.assertEqual(result['execution_sha256'], hashlib.sha256(self.execution_path.read_bytes()).hexdigest())
        self.assertEqual(self.observe.call_count, 2)
        self.assertEqual(self.claim.call_count, 2)

    def test_missing_wrong_running_failed_or_boolean_terminal_receipt_refuses(self):
        for changes in ({'invocation': 'e' * 32}, {'state': 'unknown'},
                        {'state': 'running', 'main_pid': 2}, {'main_pid': 1},
                        {'exit_code': 1}, {'exit_code': False}):
            with self.subTest(changes=changes):
                self.observed = dict(self.item['receipt'], state='terminal', main_pid=0, exit_code=0) | changes
                with self.assertRaises(Refused):
                    candidate.collect_candidate(self.item)
        self.capture.assert_not_called()

    def test_invocation_is_mandatory_and_saved_job_must_match(self):
        del self.item['receipt']['invocation']
        with self.assertRaises(Refused):
            candidate.collect_candidate(self.item)
        self.item['receipt']['invocation'] = 'c' * 32
        self.item['job']['base_revision'] = 'e' * 40
        with self.assertRaisesRegex(Refused, 'request-changed'):
            candidate.collect_candidate(self.item)
        self.capture.assert_not_called()

    def test_launch_only_metadata_must_be_valid_not_arbitrarily_ignored(self):
        path = self.state / 'requests' / (self.token + '.json')
        for section, key, value in (('job', 'claimable', False),
                                    ('receipt', 'reserved_at', True),
                                    ('receipt', 'reserved_at', float('nan')),
                                    ('receipt', 'reserved_at', -1)):
            changed = json.loads(json.dumps(self.saved))
            changed[section][key] = value
            path.write_text(json.dumps(changed))
            with self.subTest(key=key, value=value), self.assertRaisesRegex(Refused, 'request-changed'):
                candidate.collect_candidate(self.item)
        self.capture.assert_not_called()

    def test_revoked_claim_never_reads_candidate(self):
        self.claim.side_effect = Refused('claim-owner-or-revision-mismatch')
        with self.assertRaises(Refused):
            candidate.collect_candidate(self.item)
        self.capture.assert_not_called()

    def test_execution_wrong_claim_gateway_errors_or_accepted_flag_refuses(self):
        for changes in ({'claim_revision': 'e' * 32}, {'accepted': True},
                        {'gateway_failures': ['502']}, {'exit_code': False},
                        {'workspace': '/other'}, {'base_revision': 'f' * 40}):
            with self.subTest(changes=changes):
                self.execution_path.write_text(json.dumps(self.execution | changes))
                with self.assertRaises(Refused):
                    candidate.collect_candidate(self.item)
        self.capture.assert_not_called()

    def test_second_terminal_check_and_claim_read_are_required(self):
        self.observe.side_effect = [self.observed, dict(self.observed, state='running', main_pid=12)]
        with self.assertRaises(Refused):
            candidate.collect_candidate(self.item)
        self.observe.side_effect = lambda _: self.observed
        self.claim.side_effect = [{}, Refused('claim-revoked')]
        with self.assertRaises(Refused):
            candidate.collect_candidate(self.item)

    def test_execution_changed_during_capture_refuses(self):
        def capture(*args, **kwargs):
            self.execution_path.write_text(json.dumps(self.execution | {'gateway_requests': 4}))
            return self.packet
        self.capture.side_effect = capture
        with self.assertRaisesRegex(Refused, 'execution-changed'):
            candidate.collect_candidate(self.item)

    def test_symlink_execution_or_workspace_refuses(self):
        original = self.execution_path.with_suffix('.original')
        self.execution_path.rename(original)
        self.execution_path.symlink_to(original)
        with self.assertRaises((Refused, OSError)):
            candidate.collect_candidate(self.item)
        self.execution_path.unlink()
        original.rename(self.execution_path)
        self.work.rmdir()
        self.work.symlink_to(self.data, target_is_directory=True)
        with self.assertRaises(Refused):
            candidate.collect_candidate(self.item)

    def test_packet_digest_and_branch_validation(self):
        for changes in ({'commit': 'bad'}, {'evidence_sha256': 'a' * 64},
                        {'evidence_b64': 'not base64!'}, {'ref': 'refs/heads/other'},
                        {'tree': 'f' * 39}, {'extra': 'untrusted'}):
            with self.subTest(changes=changes), self.assertRaises(Refused):
                candidate.validate_packet(self.packet | changes, self.saved)

    def test_optional_bundle_is_bounded_and_hash_bound(self):
        raw = b'synthetic bundle bytes'
        packet = self.packet | {'bundle_b64': base64.b64encode(raw).decode(),
                              'bundle_sha256': hashlib.sha256(raw).hexdigest()}
        self.assertEqual(candidate.validate_packet(packet, self.saved, include_bundle=True), packet)
        for changes in ({'bundle_b64': ''}, {'bundle_sha256': 'a' * 64},
                        {'bundle_b64': 'not base64'}, {'bundle_b64': True},
                        {'bundle_b64': 'x' * ((candidate.MAX_BUNDLE + 2) // 3 * 4 + 1)}):
            with self.subTest(keys=list(changes)), self.assertRaisesRegex(Refused, 'bundle-invalid'):
                candidate.validate_packet(packet | changes, self.saved, include_bundle=True)
        with self.assertRaises(Refused):
            candidate.validate_packet(packet, self.saved)

    def test_review_proposal_is_typed_exact_and_not_inferred_from_markdown(self):
        item = json.loads(json.dumps(self.saved))
        item['job']['stage'] = 'review'
        decision = {'schema': 'skfleet.review-proposal/v1', 'card': item['job']['card'],
                    'source_head': item['job']['base_revision'], 'verdict': 'PASS',
                    'evidence_sha256': self.packet['evidence_sha256']}
        def packet(raw):
            return self.packet | {'review_decision_b64': base64.b64encode(raw).decode(),
                                  'review_decision_sha256': hashlib.sha256(raw).hexdigest()}
        for verdict in ('PASS', 'FAIL', 'BLOCKED'):
            value = packet(json.dumps(decision | {'verdict': verdict}).encode())
            candidate.validate_packet(value, item)
            self.assertEqual(candidate.review_decision(value, item['job'])['verdict'], verdict)
        for change in ({'source_head': '0' * 40}, {'card': 'deadbeef'}, {'schema': 'other'},
                       {'evidence_sha256': '0' * 64}, {'verdict': 'PASS_FOR_REVIEW'},
                       {'verdict': True}, {'extra': 'not accepted'}):
            with self.subTest(change=change), self.assertRaisesRegex(Refused, 'review-proposal-invalid'):
                candidate.validate_packet(packet(json.dumps(decision | change).encode()), item)
        duplicate = json.dumps(decision)[:-1] + ', "verdict": "PASS"}'
        for raw in (duplicate.encode(), b'PASS', b'[]', b'{}', b'x' * 16385):
            with self.subTest(raw=raw[:30]), self.assertRaisesRegex(Refused, 'review-proposal-invalid'):
                candidate.validate_packet(packet(raw), item)
        with self.assertRaisesRegex(Refused, 'packet-invalid'):
            candidate.validate_packet(self.packet, item)

    def test_bundle_collection_keeps_original_terminal_claim_checks(self):
        raw = b'synthetic bundle bytes'
        self.capture.return_value = self.packet | {'bundle_b64': base64.b64encode(raw).decode(),
                                                 'bundle_sha256': hashlib.sha256(raw).hexdigest()}
        result = candidate.collect_candidate(self.item, include_bundle=True)
        self.assertFalse(result['accepted'])
        self.assertTrue(self.capture.call_args.kwargs['include_bundle'])
        self.assertEqual(self.claim.call_count, 2)
        self.assertEqual(self.observe.call_count, 2)

    def test_inspection_reuses_sandbox_with_read_only_workspace(self):
        wrapped = ['/usr/bin/bwrap', '--unshare-all', '--bind', str(self.work), '/work', '--', 'old']
        with patch.object(candidate.worker.fiber_sandbox, 'command', return_value=wrapped) as build, \
             patch.object(candidate.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(self.packet), '')) as run:
            self.real_inspect(self.saved, self.work, self.state)
        argv = run.call_args.args[0]
        self.assertIn('--unshare-all', argv)
        self.assertEqual(argv[argv.index('--') - 2:argv.index('--')], ['--remount-ro', '/work'])
        program = build.call_args.args[3]
        self.assertEqual(program[:3], ['/usr/bin/python3', '-I', '-c'])
        self.assertEqual(run.call_args.kwargs['timeout'], 30)
        self.assertNotIn('HOME', run.call_args.kwargs['env'])

    def test_group_writable_parent_is_not_qualified_by_private_child(self):
        self.state.chmod(0o775)
        with self.assertRaisesRegex(Refused, 'unsafe-directory'):
            candidate.collect_candidate(self.item)
        self.capture.assert_not_called()


@unittest.skipUnless(os.environ.get('SKFLEET_LIVE_SANDBOX_TESTS') == '1', 'requires qualified local sandbox')
class CandidateSandboxTest(unittest.TestCase):
    def test_real_review_changes_only_its_own_evidence(self):
        with tempfile.TemporaryDirectory(prefix='fiber-review-test-') as temporary:
            root = Path(temporary)
            work, control = root / 'work', root / 'control'
            work.mkdir(mode=0o700)
            control.mkdir(mode=0o700)
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                GIT_AUTHOR_NAME='synthetic', GIT_AUTHOR_EMAIL='test@example.invalid',
                GIT_COMMITTER_NAME='synthetic', GIT_COMMITTER_EMAIL='test@example.invalid')
            def git(*args):
                return subprocess.run(['git', '-C', str(work), *args], env=env,
                    check=True, capture_output=True, text=True).stdout.strip()
            item = request()
            item['job']['stage'] = 'review'
            item['job']['producer_provider'] = 'zai'
            branch = 'work/fiber-1234abcd-' + item['receipt']['token'][:8]
            git('init', '-q', '-b', branch)
            (work / 'source.txt').write_text('Producer source stays unchanged.\n')
            git('add', 'source.txt')
            git('commit', '-qm', 'synthetic producer')
            item['job']['base_revision'] = git('rev-parse', 'HEAD')
            directory = work / 'docs/evidence/agents/1234abcd'
            directory.mkdir(parents=True)
            evidence = b'Synthetic independent review, no live acceptance.\n'
            (directory / 'COMPLETION-EVIDENCE.md').write_bytes(evidence)
            decision = {'schema': 'skfleet.review-proposal/v1', 'card': '1234abcd',
                        'source_head': item['job']['base_revision'], 'verdict': 'PASS',
                        'evidence_sha256': hashlib.sha256(evidence).hexdigest()}
            (directory / 'REVIEW-DECISION.json').write_text(json.dumps(decision))
            git('add', 'docs')
            git('commit', '-qm', 'synthetic independent evidence')
            packet = candidate.inspect_workspace(item, work, control)
            self.assertEqual(candidate.review_decision(packet, item['job']), decision)
            (work / 'source.txt').write_text('Reviewer secretly amended producer source.\n')
            git('add', 'source.txt')
            git('commit', '-qm', 'synthetic wrong source change')
            with self.assertRaisesRegex(Refused, 'inspection-failed'):
                candidate.inspect_workspace(item, work, control)

    def test_real_git_candidate_and_refusals_in_read_only_sandbox(self):
        with tempfile.TemporaryDirectory(prefix='fiber-candidate-test-') as temporary:
            root = Path(temporary)
            work, control = root / 'work', root / 'control'
            work.mkdir(mode=0o700)
            control.mkdir(mode=0o700)
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                GIT_AUTHOR_NAME='synthetic', GIT_AUTHOR_EMAIL='test@example.invalid',
                GIT_COMMITTER_NAME='synthetic', GIT_COMMITTER_EMAIL='test@example.invalid')
            def git(*args):
                return subprocess.run(['git', '-C', str(work), *args], env=env,
                    check=True, capture_output=True, text=True).stdout.strip()
            item = request()
            branch = 'work/fiber-1234abcd-' + item['receipt']['token'][:8]
            git('init', '-q', '-b', branch)
            git('commit', '--allow-empty', '-qm', 'synthetic baseline')
            item['job']['base_revision'] = git('rev-parse', 'HEAD')
            relative = 'docs/evidence/agents/1234abcd/COMPLETION-EVIDENCE.md'
            evidence = work / relative
            evidence.parent.mkdir(parents=True)
            evidence.write_text('Synthetic evidence only. Not a live verdict.\n')
            git('add', relative)
            git('commit', '-qm', 'synthetic candidate')
            expected = git('rev-parse', 'HEAD')
            result = candidate.inspect_workspace(item, work, control)
            self.assertEqual(result['commit'], expected)
            self.assertEqual(base64.b64decode(result['evidence_b64']), evidence.read_bytes())
            self.assertEqual(git('status', '--porcelain'), '')
            exported = candidate.inspect_workspace(item, work, control, include_bundle=True)
            bundle_bytes = base64.b64decode(exported['bundle_b64'], validate=True)
            self.assertEqual(hashlib.sha256(bundle_bytes).hexdigest(), exported['bundle_sha256'])
            bundle = root / 'candidate.bundle'
            bundle.write_bytes(bundle_bytes)
            receiver = root / 'receiver.git'
            subprocess.run(['git', 'init', '--bare', '-q', str(receiver)], check=True)
            def receive(*args):
                return subprocess.run(['git', '-C', str(receiver), *args], env=env,
                    check=True, capture_output=True, text=True).stdout.strip()
            receive('fetch', '-q', str(work), item['job']['base_revision'])
            receive('bundle', 'verify', str(bundle))
            receive('fetch', '-q', str(bundle), exported['ref'])
            self.assertEqual(receive('rev-parse', 'FETCH_HEAD'), expected)
            self.assertEqual(receive('rev-parse', 'FETCH_HEAD^{tree}'), result['tree'])
            self.assertEqual(git('status', '--porcelain'), '')
            evidence.write_text('changed after commit\n')
            with self.assertRaisesRegex(Refused, 'inspection-failed'):
                candidate.inspect_workspace(item, work, control)
            evidence.write_text('Synthetic evidence only. Not a live verdict.\n')
            item['job']['base_revision'] = '0' * 40
            with self.assertRaisesRegex(Refused, 'inspection-failed'):
                candidate.inspect_workspace(item, work, control)
            item['job']['base_revision'] = git('rev-parse', 'HEAD^')
            git('branch', '-m', 'different')
            with self.assertRaisesRegex(Refused, 'inspection-failed'):
                candidate.inspect_workspace(item, work, control)
            self.assertEqual(git('rev-parse', 'HEAD'), expected)


if __name__ == '__main__':
    unittest.main()
