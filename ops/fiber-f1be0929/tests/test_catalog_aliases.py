"""Exercise the exact jq/shell migration with synthetic credentials only."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CatalogTest(unittest.TestCase):
    def test_additive_idempotent_conflict_and_byte_exact_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, descriptors = root / 'models.json', root / 'aliases.json'
            gateway = {'baseUrl': 'http://chiap01:18790/v1', 'api': 'openai-completions',
                       'apiKey': 'synthetic-test-value', 'models': [{'id': 'sk-l'}]}
            original = json.dumps({'providers': {'skgateway': gateway}, 'keep': {'nested': True}}).encode()
            catalog.write_bytes(original)
            alias = {key: value for key, value in gateway.items() if key != 'apiKey'}
            alias['models'] = [{'id': 'deepseek-flash'}]
            descriptors.write_text(json.dumps({'providers': {'skgw-deepseek': alias}}))
            command = ['bash', str(ROOT / 'install-catalog-aliases.sh'), str(catalog),
                       str(descriptors), str(ROOT / 'catalog-aliases.jq')]
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertNotIn('synthetic-test-value', first.stdout + first.stderr)
            merged = json.loads(catalog.read_text())
            self.assertEqual(merged['providers']['skgateway'], gateway)
            self.assertEqual(merged['keep'], {'nested': True})
            self.assertEqual(merged['providers']['skgw-deepseek']['apiKey'], gateway['apiKey'])
            self.assertEqual(catalog.stat().st_mode & 0o777, 0o600)
            installed = catalog.read_bytes()
            again = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(again.returncode, 0)
            self.assertIn('unchanged', again.stdout)
            self.assertEqual(catalog.read_bytes(), installed)
            alias['models'] = [{'id': 'conflicting'}]
            descriptors.write_text(json.dumps({'providers': {'skgw-deepseek': alias}}))
            refused = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual(catalog.read_bytes(), installed)
            backups = list(root.glob('models.json.fiber-backup.*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(backups[0].read_bytes(), original)
            subprocess.run(['cp', '--preserve=mode', str(backups[0]), str(catalog)], check=True)
            self.assertEqual(catalog.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
