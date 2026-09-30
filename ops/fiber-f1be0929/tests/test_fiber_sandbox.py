"""Security boundary tests; live namespace tests require a qualified host."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fiber_sandbox as sandbox
import fiber_gateway_bridge as bridge


class SandboxTest(unittest.TestCase):
    def test_root_has_no_host_network_home_or_runtime_socket(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            argv = sandbox.command(root / 'work', root / 'control', root / 'pi', ['/usr/bin/true'])
            self.assertIn('--unshare-all', argv)
            self.assertIn('--disable-userns', argv)
            self.assertNotIn('--share-net', argv)
            self.assertNotIn(str(Path.home()), argv)
            self.assertNotIn('/run/user', argv)
            binds = [argv[i + 1:i + 3] for i, arg in enumerate(argv) if arg == '--bind']
            self.assertEqual(binds, [[str(root / 'work'), '/work']])
            self.assertIn('--clearenv', argv)

    def test_catalog_contains_only_exact_route_and_no_auth(self):
        source = {'providers': {'skgw-zai': {'baseUrl': 'http://chiap01:18790/v1',
                  'apiKey': 'SECRET', 'headers': {'secret': 'SECRET'},
                  'models': [{'id': 'sk-zai-m', 'name': 'GLM', 'api': 'openai-completions',
                              'compat': {'thinkingLevelMap': {'off': 'low'}}}, {'id': 'unapproved'}]}}}
        result = sandbox.public_catalog(source, 'skgw-zai', 'sk-zai-m')
        self.assertNotIn('SECRET', json.dumps(result))
        provider = result['providers']['skgw-zai']
        self.assertEqual(provider['baseUrl'], 'http://127.0.0.1:18790/v1')
        self.assertEqual([x['id'] for x in provider['models']], ['sk-zai-m'])
        self.assertEqual(provider['models'][0]['compat']['thinkingLevelMap']['off'], 'low')

    def test_catalog_preserves_real_provider_api_and_thinking_map(self):
        source = json.loads((Path(__file__).resolve().parents[1] / 'approved-aliases.json').read_text())
        model = sandbox.public_catalog(source, 'skgw-zai', 'sk-zai-m')['providers']['skgw-zai']['models'][0]
        self.assertEqual(model['api'], 'openai-completions')
        self.assertEqual(model['thinkingLevelMap']['off'], 'low')
        self.assertFalse(model['compat']['supportsDeveloperRole'])

    def test_gateway_refuses_wrong_route_oversize_and_token_bypass(self):
        good = {'model': 'sk-zai-m', 'messages': [{'role': 'user', 'content': 'synthetic'}],
                'max_tokens': 256}
        self.assertEqual(bridge.validate_payload('/v1/chat/completions', json.dumps(good).encode(), 'sk-zai-m')['model'], 'sk-zai-m')
        for path, obj in [('/admin', good), ('http://other/v1/chat/completions', good),
                          ('/v1/chat/completions', dict(good, model='other')),
                          ('/v1/chat/completions', dict(good, max_tokens=999999)),
                          ('/v1/chat/completions', dict(good, max_tokens=True))]:
            with self.subTest(path=path, obj=obj), self.assertRaises(ValueError):
                bridge.validate_payload(path, json.dumps(obj).encode(), 'sk-zai-m')
        with self.assertRaises(ValueError):
            bridge.validate_payload('/v1/chat/completions', b'x' * (bridge.MAX_BODY + 1), 'sk-zai-m')

    def test_private_clone_has_no_shared_git_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            source, target = Path(td) / 'source', Path(td) / 'target'
            env = dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
                       GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@example.invalid',
                       GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@example.invalid')
            def git(*args):
                return subprocess.run(['git', *args], env=env, check=True, capture_output=True, text=True).stdout.strip()
            git('init', '-q', str(source))
            git('-C', str(source), 'commit', '--allow-empty', '-qm', 'initial')
            head = git('-C', str(source), 'rev-parse', 'HEAD')
            sandbox.private_clone(source, target, 'work/test', head, git)
            self.assertTrue((target / '.git').is_dir())
            self.assertFalse((target / '.git/objects/info/alternates').exists())
            self.assertEqual(git('-C', str(target), 'rev-parse', 'HEAD'), head)
            self.assertEqual(git('-C', str(target), 'remote'), '')
            with self.assertRaises(ValueError):
                sandbox.private_clone(source, target, 'work/again', head, git)


if __name__ == '__main__':
    unittest.main()
