"""Test exact migration, credential preservation, refusal and rollback bytes."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ThinkingMigrationTest(unittest.TestCase):
    def catalog(self):
        """Return an old-format synthetic catalog with unrelated fields."""
        result = json.loads((ROOT / 'approved-aliases.json').read_text())
        for provider in result['providers'].values():
            provider['apiKey'] = 'synthetic-test-only'
            for model in provider['models']:
                model.pop('compat', None)
                model.pop('thinkingLevelMap', None)
        result['providers']['skgw-deepseek']['models'].append({'id': 'untouched-pro'})
        result['keep'] = {'unchanged': True}
        return result

    def run_migration(self, catalog):
        """Exercise the existing atomic installer with the new narrow filter."""
        return subprocess.run(['bash', str(ROOT / 'install-catalog-aliases.sh'),
            str(catalog), str(ROOT / 'approved-aliases.json'),
            str(ROOT / 'catalog-thinking.jq')], text=True, capture_output=True)

    def test_exact_changes_idempotency_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'models.json'
            before = self.catalog()
            original = json.dumps(before).encode()
            path.write_bytes(original)
            result = self.run_migration(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('synthetic-test-only', result.stdout + result.stderr)
            expected = copy.deepcopy(before)
            expected['providers']['skgw-deepseek']['models'][0]['compat'] = {'thinkingFormat': 'deepseek'}
            expected['providers']['skgw-zai']['models'][0]['thinkingLevelMap'] = {'off': 'low'}
            self.assertEqual(json.loads(path.read_text()), expected)
            installed = path.read_bytes()
            again = self.run_migration(path)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn('unchanged', again.stdout)
            self.assertEqual(path.read_bytes(), installed)
            backups = list(path.parent.glob('models.json.fiber-backup.*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
            subprocess.run(['cp', '--preserve=mode', str(backups[0]), str(path)], check=True)
            self.assertEqual(path.read_bytes(), original)

    def test_refuses_conflicting_or_missing_route(self):
        variants = []
        value = self.catalog()
        value['providers']['skgw-zai']['baseUrl'] = 'https://unapproved.invalid/v1'
        variants.append(value)
        value = self.catalog()
        value['providers']['skgw-deepseek']['models'][0]['compat'] = {'thinkingFormat': 'zai'}
        variants.append(value)
        value = self.catalog()
        value['providers']['skgw-zai']['models'][0]['thinkingLevelMap'] = {'off': 'high'}
        variants.append(value)
        value = self.catalog()
        value['providers']['skgw-zai']['models'] = []
        variants.append(value)
        value = self.catalog()
        value['providers']['skgw-zai']['models'] *= 2
        variants.append(value)
        for value in variants:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'models.json'
                original = json.dumps(value).encode()
                path.write_bytes(original)
                self.assertNotEqual(self.run_migration(path).returncode, 0)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(path.parent.glob('*.fiber-backup.*')), [])


if __name__ == '__main__':
    unittest.main()
