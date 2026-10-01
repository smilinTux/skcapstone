"""Qualified family identity cannot be replaced by a transport/backend alias."""
import copy
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fiber_admission as admission
import fiber_control as control
import fiber_probe as probe
import fiber_routes as routes


class RoutesTest(unittest.TestCase):
    def test_streamed_node_probe_needs_no_installed_host_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            isolated = Path(directory) / 'fiber_probe.py'
            isolated.write_bytes(Path(probe.__file__).read_bytes())
            result = subprocess.run([sys.executable, '-I', '-c',
                'import runpy,sys; runpy.run_path(sys.argv[1], run_name="remote_node_probe")',
                str(isolated)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def headers(self):
        return {'x-sk-provider': 'local', 'x-sk-backend': 'chiap08-qwen38',
                'x-sk-model-requested': routes.QWEN_MODEL,
                'x-sk-model-served': routes.QWEN_MODEL}

    def test_qwen_requires_qualified_backend_provider_and_model(self):
        self.assertTrue(routes.provider_matches('qwen', routes.QWEN_MODEL, self.headers()))
        for change in [{'x-sk-provider': 'qwen'}, {'x-sk-backend': 'another-local'},
                       {'x-sk-backend': None}, {'x-sk-provider': 'kimi'}]:
            with self.subTest(change=change):
                self.assertFalse(routes.provider_matches('qwen', routes.QWEN_MODEL,
                    dict(self.headers(), **change)))
        self.assertFalse(routes.provider_matches('qwen', 'unqualified-qwen-alias', self.headers()))

    def test_existing_family_rejects_conflicting_backend(self):
        self.assertTrue(routes.provider_matches('deepseek', 'deepseek-flash',
                                               {'x-sk-backend': 'deepseek'}))
        self.assertFalse(routes.provider_matches('deepseek', 'deepseek-flash',
            {'x-sk-provider': 'deepseek', 'x-sk-backend': 'zai'}))

    def test_same_family_and_backend_aliases_never_establish_independence(self):
        self.assertTrue(routes.independent('qwen', 'deepseek'))
        for producer, reviewer in [('qwen', 'qwen'), ('local', 'qwen'),
                                  ('chiap08-qwen38', 'qwen'), ('kimi-for-coding', 'kimi-k3'),
                                  (None, 'deepseek')]:
            with self.subTest(producer=producer, reviewer=reviewer):
                self.assertFalse(routes.independent(producer, reviewer))
        self.assertNotIn('kimi', admission.PROVIDERS)
        self.assertNotIn('local', admission.PROVIDERS)

    def test_completion_preserves_exact_served_model_gate(self):
        job = {'provider': 'qwen', 'model': routes.QWEN_MODEL}
        answer = {'model': routes.QWEN_MODEL, 'choices': [{'message': {'content': 'OK'}}]}
        self.assertEqual(control.validate_completion(job, answer, self.headers()), routes.QWEN_MODEL)
        altered = dict(answer, model='another-qwen-model')
        with self.assertRaisesRegex(admission.Refused, 'completion-model-substitution'):
            control.validate_completion(job, altered, self.headers())
        with self.assertRaisesRegex(admission.Refused, 'completion-attribution-mismatch'):
            control.validate_completion(job, answer, dict(self.headers(), **{'x-sk-backend': 'kimi-k3'}))

    def test_live_footer_and_process_attribute_canonical_qwen_only(self):
        self.assertEqual(probe.footer_provider('(skgw-qwen) ' + routes.QWEN_MODEL + ' • high'), 'qwen')
        self.assertEqual(probe.process_provider('pi',
            {'SKFLEET_PROVIDER': 'qwen', 'SKFLEET_MODEL': routes.QWEN_MODEL}), 'qwen')
        self.assertIsNone(probe.footer_provider('(skgw-local) ' + routes.QWEN_MODEL + ' • high'))
        self.assertEqual(routes.CAPACITY_DOMAINS['qwen'], ('chiap08-qwen38',))

    def test_catalog_preserves_original_and_exact_credential_reference(self):
        original = {'providers': {'skgw-deepseek': {'baseUrl': 'http://chiap01:18790/v1',
            'api': 'openai-completions', 'apiKey': 'EXISTING_ENV_REFERENCE',
            'models': [{'id': 'deepseek-flash'}]}}}
        before = copy.deepcopy(original)
        result = routes.qwen_catalog(original)
        self.assertEqual(original, before)
        self.assertEqual(result['providers']['skgw-deepseek'], original['providers']['skgw-deepseek'])
        self.assertEqual(result['providers']['skgw-qwen']['apiKey'], 'EXISTING_ENV_REFERENCE')
        self.assertEqual(routes.qwen_catalog(result), result)
        result['providers']['skgw-qwen']['apiKey'] = 'DIFFERENT_REFERENCE'
        with self.assertRaisesRegex(ValueError, 'binding-conflict'):
            routes.qwen_catalog(result)
        original['providers']['skgw-deepseek']['baseUrl'] = 'https://unapproved.invalid/v1'
        with self.assertRaisesRegex(ValueError, 'source-unqualified'):
            routes.qwen_catalog(original)

    def test_live_qualification_uses_actual_gateway_domain_and_records_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            agent = home / '.pi/agent'
            agent.mkdir(parents=True)
            (home / 'observations').mkdir()
            catalog = routes.qwen_catalog({'providers': {'skgw-deepseek': {
                'baseUrl': 'http://chiap01:18790/v1', 'api': 'openai-completions',
                'apiKey': 'synthetic-credential', 'models': []}}})
            (agent / 'models.json').write_text(json.dumps(catalog))
            response = MagicMock()
            response.__enter__.return_value = response
            response.headers = self.headers()
            response.read.return_value = json.dumps({'model': routes.QWEN_MODEL,
                'choices': [{'message': {'content': 'OK'}}]}).encode()
            with patch.object(control.Path, 'home', return_value=home), \
                    patch.object(control, 'STATE', home), patch.object(control, '_route_proofs', {}), \
                    patch('skcapstone.fleet_lane_health.acquire_lane_snapshot',
                          return_value={'runtime_revision': 'a' * 40}) as acquire, \
                    patch('skcapstone.fleet_lane_health.lane_health', return_value=(True, 'ready')) as health, \
                    patch.object(control.urllib.request, 'urlopen', return_value=response):
                actual = control.qualify_route({'provider': 'qwen', 'model': routes.QWEN_MODEL,
                                               'pi_provider': 'skgw-qwen'})
            self.assertEqual(acquire.call_args.args[2], {'qwen': ('chiap08-qwen38',)})
            self.assertEqual(health.call_args.kwargs['capacity_domains'], ('chiap08-qwen38',))
            self.assertEqual(actual['proof']['gateway_provider'], 'local')
            self.assertEqual(actual['proof']['gateway_backend'], 'chiap08-qwen38')
            self.assertEqual(actual['proof']['provider'], 'qwen')


if __name__ == '__main__':
    unittest.main()
