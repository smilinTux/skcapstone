"""Remote adapter refusal and exact service-manager receipt tests."""
from pathlib import Path
import subprocess
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fiber_worker as worker
from fiber_admission import Refused


def request():
    token = 'a' * 32
    job = {'card': '1234abcd', 'owner': 'fiber-test', 'host': 'chiap02',
           'provider': 'deepseek', 'pi_provider': 'skgw-deepseek', 'model': 'deepseek-flash',
           'repository': 'https://example.invalid/approved/repo.git',
           'base_ref': 'refs/heads/main', 'base_revision': 'b' * 40, 'stage': 'implementation'}
    receipt = {'card': job['card'], 'host': job['host'], 'provider': job['provider'],
               'token': token, 'unit': 'skfleet-fiber-1234abcd-' + token + '.service'}
    return {'job': job, 'receipt': receipt}


class WorkerTest(unittest.TestCase):
    def test_replica_read_failure_waits_but_authority_failure_does_not(self):
        item = request()
        item['claim'] = {'authority': 'chiap08', 'owner': 'fiber-test', 'revision': 'a' * 32}
        row = {'owner': 'fiber-test'}
        with patch.object(worker, 'read_claim', side_effect=[row, Refused('claim-readback-failed'), row, row]) as read, \
             patch.object(worker, 'verify_source'), patch.object(worker.time, 'sleep') as sleep:
            self.assertEqual(worker.verify_claim(item, wait_seconds=5), row)
            self.assertEqual(read.call_count, 4)
            sleep.assert_called_once()
        for responses in ([Refused('claim-readback-failed')],
                          [row, Refused('claim-readback-failed'), Refused('claim-not-active')]):
            with patch.object(worker, 'read_claim', side_effect=responses) as read, \
                 patch.object(worker, 'verify_source'), patch.object(worker.time, 'sleep'):
                with self.assertRaises(Refused):
                    worker.verify_claim(item, wait_seconds=5)
                self.assertEqual(read.call_count, len(responses))

    def test_replica_read_failure_has_deadline_and_no_wait_default(self):
        item = request()
        item['claim'] = {'authority': 'chiap08', 'owner': 'fiber-test', 'revision': 'a' * 32}
        for seconds in (0, 5):
            with patch.object(worker, 'read_claim', side_effect=[{}, Refused('claim-readback-failed')]), \
                 patch.object(worker, 'verify_source'), patch.object(worker.time, 'monotonic', side_effect=[0, 6]), \
                 patch.object(worker.time, 'sleep') as sleep:
                with self.assertRaisesRegex(Refused, 'claim-readback-failed'):
                    worker.verify_claim(item, wait_seconds=seconds)
                sleep.assert_not_called()

    def test_source_origin_allows_only_exact_forgejo_transport(self):
        repository = 'https://skgit.skstack01.douno.it/smilinTux/sklegal.git'
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / 'cache.git'
            env = dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1')
            def git(*args):
                return subprocess.run(['git', *args], env=env, check=True,
                                      capture_output=True, text=True).stdout.strip()
            git('init', '--bare', '--quiet', str(cache))
            git('-C', str(cache), 'remote', 'add', 'origin', repository)
            worker.verify_cache_origin(cache, repository, git)
            for target in ('ssh://git@skgit.skstack01.douno.it:222/smilinTux/sklegal.git',
                           'ssh://git@evil.invalid:222/smilinTux/sklegal.git',
                           'ssh://git@skgit.skstack01.douno.it:222/other/repo.git',
                           'ssh://git@skgit.skstack01.douno.it:22/smilinTux/sklegal.git',
                           'ssh://root@skgit.skstack01.douno.it:222/smilinTux/sklegal.git'):
                key = 'url.' + target + '.insteadOf'
                git('-C', str(cache), 'config', key, repository)
                with self.subTest(target=target):
                    if target == 'ssh://git@skgit.skstack01.douno.it:222/smilinTux/sklegal.git':
                        worker.verify_cache_origin(cache, repository, git)
                    else:
                        with self.assertRaisesRegex(Refused, 'source-cache-mismatch'):
                            worker.verify_cache_origin(cache, repository, git)
                git('-C', str(cache), 'config', '--unset', key)
            git('-C', str(cache), 'remote', 'set-url', 'origin', 'https://evil.invalid/repo.git')
            with self.assertRaisesRegex(Refused, 'source-cache-mismatch'):
                worker.verify_cache_origin(cache, repository, git)
            link = Path(td) / 'link.git'
            link.symlink_to(cache, target_is_directory=True)
            with self.assertRaisesRegex(Refused, 'source-cache-mismatch'):
                worker.verify_cache_origin(link, repository, git)

    def test_replica_wait_rechecks_authority_before_acceptance(self):
        item = request()
        item['claim'] = {'authority': 'chiap08', 'owner': 'fiber-test', 'revision': 'a' * 32}
        row = {'owner': 'fiber-test'}
        with patch.object(worker, 'read_claim', side_effect=[row, Refused('claim-owner-or-revision-mismatch'), row, row]) as read, \
             patch.object(worker, 'verify_source'), patch.object(worker.time, 'sleep') as sleep:
            self.assertEqual(worker.verify_claim(item, wait_seconds=5), row)
            self.assertEqual(read.call_count, 4)
            sleep.assert_called_once()

    def test_replica_wait_never_retries_authority_revocation(self):
        item = request()
        item['claim'] = {'authority': 'chiap08', 'owner': 'fiber-test', 'revision': 'a' * 32}
        with patch.object(worker, 'read_claim', side_effect=[{}, Refused('claim-owner-or-revision-mismatch'), Refused('claim-not-active')]) as read, \
             patch.object(worker, 'verify_source'), patch.object(worker.time, 'sleep'):
            with self.assertRaisesRegex(Refused, 'claim-not-active'):
                worker.verify_claim(item, wait_seconds=5)
            self.assertEqual(read.call_count, 3)

    def test_replica_wait_is_bounded_and_checks_no_other_failures(self):
        item = request()
        item['claim'] = {'authority': 'chiap08', 'owner': 'fiber-test', 'revision': 'a' * 32}
        with patch.object(worker, 'read_claim', side_effect=[{}, Refused('claim-owner-or-revision-mismatch')]), \
             patch.object(worker, 'verify_source'), patch.object(worker.time, 'monotonic', side_effect=[0, 6]), \
             patch.object(worker.time, 'sleep') as sleep:
            with self.assertRaisesRegex(Refused, 'claim-owner-or-revision-mismatch'):
                worker.verify_claim(item, wait_seconds=5)
            sleep.assert_not_called()
        with patch.object(worker, 'read_claim', side_effect=[{}, Refused('claim-readback-invalid')]), \
             patch.object(worker, 'verify_source'), patch.object(worker.time, 'sleep') as sleep:
            with self.assertRaisesRegex(Refused, 'claim-readback-invalid'):
                worker.verify_claim(item, wait_seconds=5)
            sleep.assert_not_called()

    def test_start_defers_replica_wait_to_tracked_service(self):
        item = request()
        with tempfile.TemporaryDirectory() as td, patch.object(worker, 'STATE', Path(td)), \
             patch.object(worker.socket, 'gethostname', return_value='chiap02'), \
             patch.object(worker, 'check_catalog'), patch.object(worker, 'verify_claim') as claim, \
             patch.object(worker, 'read_unit', return_value={'state': 'running'}), \
             patch.object(worker.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            worker.start(item)
            claim.assert_called_once_with(item, authority_only=True)

    def test_execute_waits_before_resource_or_workspace_actions(self):
        item = request()
        with patch.object(worker.socket, 'gethostname', return_value='chiap02'), \
             patch.object(worker, 'check_catalog'), \
             patch.object(worker, 'verify_claim', side_effect=Refused('claim-owner-or-revision-mismatch')) as claim, \
             patch.object(worker, 'verify_resource_limits') as resources, \
             patch.object(worker, 'prepare_worktree') as materialize:
            with self.assertRaisesRegex(Refused, 'claim-owner-or-revision-mismatch'):
                worker.execute(item)
            claim.assert_called_once_with(item, wait_seconds=300)
            resources.assert_not_called()
            materialize.assert_not_called()

    def test_authority_only_does_not_consult_replica(self):
        item = request()
        item['claim'] = {'authority': 'chiap08', 'owner': 'fiber-test', 'revision': 'a' * 32}
        with patch.object(worker, 'read_claim', return_value={}) as read, patch.object(worker, 'verify_source'):
            worker.verify_claim(item, authority_only=True)
            self.assertEqual(read.call_count, 1)
            self.assertIn('runner', read.call_args.kwargs)

    def test_actual_worker_resource_budget_is_required(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name, value in {'memory.max': '3221225472', 'memory.swap.max': '536870912',
                                'pids.max': '256', 'cpu.max': '200000 100000'}.items():
                (root / name).write_text(value)
            worker.verify_resource_limits(root)
            (root / 'memory.max').write_text('max')
            with self.assertRaisesRegex(Refused, 'worker-resource-limits'):
                worker.verify_resource_limits(root)

    def test_small_host_rejects_forged_work_class(self):
        item = request()['job']
        item.update(host='chiwk13', stage='tests', work_class='S')
        row = {'title': '[M] substantial test work', 'labels': ['source-only'],
               'meta': {key: item[key] for key in ['repository', 'base_ref', 'base_revision']}}
        item['card_fingerprint'] = worker.card_fingerprint(row)
        with self.assertRaisesRegex(Refused, 'work-class-mismatch'):
            worker.verify_source(row, item)

    def test_authority_and_local_claim_must_both_match(self):
        item = request()
        item['claim'] = {'authority': 'chiap08', 'owner': item['job']['owner'], 'revision': 'a' * 32}
        row = {'owner': item['job']['owner']}
        with patch.object(worker, 'read_claim', return_value=row) as read, patch.object(worker, 'verify_source'):
            self.assertEqual(worker.verify_claim(item), row)
            self.assertEqual(read.call_count, 2)
            for call in read.call_args_list:
                self.assertEqual(call.kwargs['revision'], 'a' * 32)
            read.reset_mock()
            read.side_effect = [row, Refused('claim-owner-or-revision-mismatch')]
            with self.assertRaisesRegex(Refused, 'claim-owner-or-revision-mismatch'):
                worker.verify_claim(item)
            self.assertEqual(read.call_count, 2)
            read.reset_mock()
            read.side_effect = Refused('claim-readback-failed')
            with self.assertRaisesRegex(Refused, 'claim-readback-failed'):
                worker.verify_claim(item)
            self.assertEqual(read.call_count, 1)

    def test_authority_read_uses_only_mediated_cli_with_worker_identity(self):
        item = request()
        item['claim'] = {'authority': 'chiap08', 'owner': 'fiber-test', 'revision': 'a' * 32}
        def read(card, owner, **kwargs):
            if 'runner' in kwargs:
                kwargs['runner'](['skcapstone', 'coord', 'show', card, '--json'], capture_output=True, text=True, timeout=60)
            return {}
        with patch.object(worker, 'read_claim', side_effect=read), patch.object(worker, 'verify_source'), \
             patch.object(worker.subprocess, 'run') as run:
            worker.verify_claim(item)
            argv = run.call_args.args[0]
            self.assertEqual(argv[:6], ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', 'chiap08'])
            self.assertIn('SKAGENT=fiber-test', argv[-1])
            self.assertIn('coord show 1234abcd --json', argv[-1])
            self.assertNotIn('coord claim', argv[-1])

    def test_missing_claim_refuses_before_request_or_materialization(self):
        with tempfile.TemporaryDirectory() as td, patch.object(worker, 'STATE', Path(td)), \
             patch.object(worker.socket, 'gethostname', return_value='chiap02'), \
             patch.object(worker, 'check_catalog'), patch.object(worker, 'read_claim') as read:
            with self.assertRaisesRegex(Refused, 'authority-claim-required'):
                worker.start(request())
            self.assertEqual(list(Path(td).iterdir()), [])
            read.assert_not_called()

    def test_source_metadata_and_links_must_agree(self):
        item = request()['job']
        binding = {key: item[key] for key in ['repository', 'base_ref', 'base_revision']}
        self.assertEqual(worker.source_binding({'links': binding}), tuple(binding.values()))
        self.assertEqual(worker.source_binding({'meta': binding}), tuple(binding.values()))
        self.assertEqual(worker.source_binding({'meta': binding, 'links': binding}), tuple(binding.values()))
        conflict = dict(binding, base_revision='c' * 40)
        with self.assertRaisesRegex(Refused, 'conflicting-source-binding'):
            worker.source_binding({'meta': conflict, 'links': binding})

    def review_row(self):
        item = request()['job']
        return {'title': '[S][REVIEW] exact candidate', 'labels': ['source-only'],
                'meta': {**{key: item[key] for key in ['repository', 'base_ref', 'base_revision']},
                         'link_head_revision': 'c' * 40, 'link_source_card': 'abcdef12',
                         'producer_identity': 'pi-producer', 'candidate_evidence_sha256': 'd' * 64},
                'links': {}}

    def test_review_uses_candidate_without_mutating_historical_base(self):
        import copy
        row = self.review_row()
        before = copy.deepcopy(row)
        self.assertEqual(worker.source_binding(row, review=True)[2], 'c' * 40)
        self.assertEqual(worker.source_binding(row)[2], 'b' * 40)
        self.assertEqual(row, before)
        row['links'] = row.pop('meta')
        self.assertEqual(worker.source_binding(row, review=True)[2], 'c' * 40)

    def test_review_refuses_missing_malformed_and_conflicting_typed_fields(self):
        for field in ('link_head_revision', 'link_source_card', 'producer_identity', 'candidate_evidence_sha256'):
            for bad in (None, '', 123, [], 'not a valid pin!'):
                row = self.review_row()
                row['meta'][field] = bad
                with self.subTest(field=field, bad=bad), self.assertRaises(Refused):
                    worker.source_binding(row, review=True)
            row = self.review_row()
            row['links'][field] = {'link_head_revision': 'e' * 40, 'link_source_card': 'abcdef13',
                                    'producer_identity': 'pi-other', 'candidate_evidence_sha256': 'e' * 64}[field]
            with self.subTest(field=field), self.assertRaisesRegex(Refused, 'conflicting-review-binding'):
                worker.source_binding(row, review=True)
        row = self.review_row()
        row['links']['base_revision'] = 'a' * 40
        with self.assertRaisesRegex(Refused, 'conflicting-source-binding'):
            worker.source_binding(row, review=True)

    def test_review_verification_refuses_old_base_and_preserves_source_lane(self):
        row = self.review_row()
        item = dict(request()['job'], stage='review', card_fingerprint=worker.card_fingerprint(row))
        with self.assertRaisesRegex(Refused, 'source-binding-changed'):
            worker.verify_source(row, item)
        item['base_revision'] = 'c' * 40
        worker.verify_source(row, item)
        for labels in ([], ['source-only', 'local-only']):
            row['labels'] = labels
            item['card_fingerprint'] = worker.card_fingerprint(row)
            with self.assertRaisesRegex(Refused, 'source-lane-policy'):
                worker.verify_source(row, item)

    def test_review_cannot_be_admitted_as_implementation_or_without_stage(self):
        row = self.review_row()
        for stage in (None, 'implementation', 'tests', 'invalid'):
            item = dict(request()['job'], stage=stage, card_fingerprint=worker.card_fingerprint(row))
            with self.subTest(stage=stage), self.assertRaises(Refused):
                worker.verify_source(row, item)

    def test_binding_and_route_validation(self):
        self.assertEqual(worker.validate(request())[0]['card'], '1234abcd')
        for section, field, value in [('receipt', 'token', '../bad'),
                                      ('receipt', 'host', 'chiap08'),
                                      ('job', 'model', 'kimi-for-coding'),
                                      ('job', 'repository', 'https://secret@example.invalid/repo'),
                                      ('job', 'base_revision', 'main')]:
            item = request()
            item[section][field] = value
            with self.subTest(field=field), self.assertRaises(Refused):
                worker.validate(item)

    def test_start_refuses_missing_catalog_before_request_write(self):
        with tempfile.TemporaryDirectory() as td, patch.object(worker, 'STATE', Path(td)), \
             patch.object(worker.socket, 'gethostname', return_value='chiap02'), \
             patch.object(worker, 'check_catalog', side_effect=Refused('model-not-in-agent-catalog')):
            with self.assertRaisesRegex(Refused, 'model-not-in-agent-catalog'):
                worker.start(request())
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_start_uses_resource_limits_and_refuses_duplicate_request(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, '', '')
        with tempfile.TemporaryDirectory() as td, patch.object(worker, 'STATE', Path(td)), \
             patch.object(worker.socket, 'gethostname', return_value='chiap02'), \
             patch.object(worker, 'check_catalog'), patch.object(worker, 'verify_claim'), \
             patch.object(worker, 'read_unit', return_value={'state': 'running'}), \
             patch.object(worker.subprocess, 'run', side_effect=run):
            worker.start(request())
            with self.assertRaises(FileExistsError):
                worker.start(request())
        self.assertEqual(len(calls), 1)
        for flag in ['--property=CPUQuota=200%', '--property=MemoryMax=3G',
                     '--property=MemorySwapMax=512M', '--property=TasksMax=256',
                     '--property=RuntimeMaxSec=3600', '--property=RemainAfterExit=yes']:
            self.assertIn(flag, calls[0])
        self.assertNotIn('--collect', calls[0])
        self.assertIn('--setenv=PATH=' + str(Path.home() / '.skenv/bin') + ':'
                      + str(Path.home() / '.npm-global/bin')
                      + ':' + str(Path.home() / '.local/bin')
                      + ':/usr/local/bin:/usr/bin:/bin', calls[0])

    def test_service_receipt_requires_exact_description_and_exit(self):
        receipt = request()['receipt']
        values = {'Id': receipt['unit'], 'Description': 'SKFleet fiber 1234abcd ' + receipt['token'],
                  'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'exited',
                  'MainPID': '0', 'InvocationID': 'b' * 32, 'ExecMainCode': '1', 'ExecMainStatus': '0'}
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, '\n'.join(k + '=' + v for k, v in values.items()), '')
        with patch.object(worker.subprocess, 'run', side_effect=run):
            self.assertEqual(worker.read_unit(receipt)['state'], 'terminal')
            values['ExecMainCode'] = '0'
            self.assertEqual(worker.read_unit(receipt)['state'], 'unknown')
            values['Description'] = 'unrelated service'
            self.assertEqual(worker.read_unit(receipt)['state'], 'unknown')

    def test_source_and_policy_refusal_precede_materialization(self):
        item = request()
        row = {'labels': ['source-only'], 'links': {'repository': item['job']['repository'],
               'base_ref': item['job']['base_ref'], 'base_revision': 'c' * 40}, 'meta': {}}
        item['job']['card_fingerprint'] = worker.card_fingerprint(row)
        with patch.object(worker.socket, 'gethostname', return_value='chiap02'), \
             patch.object(worker, 'check_catalog'), patch.object(worker, 'verify_claim', side_effect=Refused('source-binding-changed')), \
             patch.object(worker.subprocess, 'run') as run:
            with self.assertRaisesRegex(Refused, 'source-binding-changed'):
                worker.execute(item)
            run.assert_not_called()
        row['labels'].append('no-egress')
        item['job']['card_fingerprint'] = worker.card_fingerprint(row)
        with self.assertRaisesRegex(Refused, 'source-lane-policy'):
            worker.verify_source(row, item['job'])

    def test_job_without_environment_keeps_stdlib_only_behavior(self):
        item = request()
        job, _ = worker.validate(item)
        self.assertNotIn('test_environment', job)
        self.assertIsNone(worker.environment_plan(job))

    def test_environment_selector_supports_every_existing_stage(self):
        for stage in ('implementation', 'review', 'tests'):
            item = request()
            item['job'].update(stage=stage, host='auto',
                               test_environment={'lock_sha256': 'a' * 64, 'python': '3.12'})
            item['receipt']['host'] = 'auto'
            with self.subTest(stage=stage):
                job, _ = worker.validate(item)
                self.assertEqual(job['test_environment'], {'lock_sha256': 'a' * 64, 'python': '3.12'})
        for selector in (None, 'not-a-dict', {'lock_sha256': 'a' * 64, 'python': '3.12', 'venv': '/tmp/x'}):
            item = request()
            item['job'].update(stage='tests', test_environment=selector)
            with self.subTest(selector=selector), self.assertRaises(Refused):
                worker.validate(item)

    def test_missing_environment_refuses_before_claim(self):
        item = request()
        item['job'].update(stage='tests', test_environment={'lock_sha256': 'a' * 64, 'python': '3.12'})
        with patch.object(worker.socket, 'gethostname', return_value='chiap02'), \
             patch.object(worker, 'check_catalog'), \
             patch.object(worker, 'environment_plan', side_effect=Refused('test-environment-unavailable')) as plan, \
             patch.object(worker, 'verify_claim') as claim:
            with self.assertRaisesRegex(Refused, 'test-environment-unavailable'):
                worker.execute(item)
            plan.assert_called_once()
            claim.assert_not_called()

    def test_environment_plan_uses_exact_repository(self):
        item = request()
        selected = {'lock_sha256': 'a' * 64, 'python': '3.12'}
        item['job'].update(stage='tests', test_environment=selected)
        job, _ = worker.validate(item)
        with patch.object(worker.fiber_test_environment, 'qualify', return_value={'venv': '/x'}) as qualify:
            self.assertEqual(worker.environment_plan(job), {'venv': '/x'})
            qualify.assert_called_once_with(selected, job['repository'])

    def test_mismatched_workspace_lock_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(Refused, 'test-environment-lock-missing'):
                worker.verify_environment_lock(td, {'lock_sha256': 'a' * 64})
            self.assertIsNone(worker.verify_environment_lock(td, None))

    def test_environment_mount_is_passed_to_sandbox_command(self):
        item = request()
        item['job'].update(stage='tests', test_environment={'lock_sha256': 'a' * 64, 'python': '3.12'})
        with patch.object(worker.socket, 'gethostname', return_value='chiap02'), \
             patch.object(worker, 'check_catalog'), \
             patch.object(worker, 'environment_plan', return_value={'venv': '/opt/qualified', 'source_dirs': ['src']}), \
             patch.object(worker, 'verify_claim', side_effect=Refused('stop-before-workspace')), \
             patch.object(worker, 'verify_resource_limits'):
            with self.assertRaisesRegex(Refused, 'stop-before-workspace'):
                worker.execute(item)


if __name__ == '__main__':
    unittest.main()
