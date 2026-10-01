"""Gateway owns request concurrency; local records prevent duplicate cards."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fiber_admission import Refused, reserve, select_host
import fiber_lifecycle as lifecycle


class GatewayCapacityTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / 'admission.sqlite3'
        self.truth = {'complete': True, 'observed_at': 1000,
                      'workers': [{'key': str(i), 'host': 'chiap02', 'provider': None}
                                  for i in range(100)],
                      'hosts': {'chiap02': {'ready': True, 'slots': 0}},
                      'providers': {'deepseek': {'ready': True}}}
        self.job = {'card': '1234abcd', 'stage': 'implementation',
                    'host': 'chiap02', 'provider': 'deepseek', 'claimable': True}

    def test_no_estate_node_provider_or_spacing_ceiling(self):
        for i in range(5):
            result = reserve(self.database, dict(self.job, card=f'{i:08x}'),
                             self.truth, now=1000)
            self.assertEqual(result['card'], f'{i:08x}')

    def test_duplicate_card_still_refused(self):
        reserve(self.database, self.job, self.truth, now=1000)
        with self.assertRaisesRegex(Refused, 'duplicate-card'):
            reserve(self.database, self.job, self.truth, now=1000)

    def test_automatic_placement_uses_every_installed_worker_host(self):
        for host in ('chiap02', 'chiap03', 'chiap04', 'chiwk12', 'chiwk13', 'ziowk01'):
            self.truth['hosts'] = {host: {'ready': True, 'slots': 0}}
            with self.subTest(host=host):
                self.assertEqual(select_host(self.database, dict(self.job, host='auto'),
                                             self.truth, now=1000), host)

    def test_gateway_and_authority_roles_are_not_worker_installations(self):
        self.truth['hosts'] = {'chiap01': {'ready': True}, 'chiap08': {'ready': True}}
        with self.assertRaisesRegex(Refused, 'no-eligible-host'):
            select_host(self.database, dict(self.job, host='auto'), self.truth, now=1000)

    def test_unready_provider_and_same_family_review_still_refused(self):
        self.truth['providers']['deepseek']['ready'] = False
        with self.assertRaisesRegex(Refused, 'provider-not-qualified'):
            reserve(self.database, self.job, self.truth, now=1000)
        self.truth['providers']['deepseek']['ready'] = True
        with self.assertRaisesRegex(Refused, 'review-independence'):
            reserve(self.database, dict(self.job, stage='review', producer_provider='deepseek'),
                    self.truth, now=1000)

    def test_invalid_lifecycle_card_does_not_stop_other_scoped_cards(self):
        root = Path(self.directory.name)
        state = root / 'state'; state.mkdir(mode=0o700)
        qualified = root / 'qualified'; qualified.mkdir(mode=0o700)
        policy = root / 'policy.json'; policy.touch(mode=0o600)
        forbidden = lambda *a, **kw: self.fail('invalid source must not mutate native state')
        with patch.object(lifecycle, '_policy', return_value={'cards': {'1234abcd': 'a', '1234abce': 'b'}}), \
             patch.object(lifecycle, '_run', side_effect=[{'state': 'terminal', 'token': 'bad'}, None]) as read:
            result = lifecycle.connect_reviews(state, qualified, policy, root / 'HOLD',
                home=root, data_root=root, read_cards=forbidden, collect_candidate=forbidden,
                snapshot_revision=forbidden, record_verdict=forbidden, open_review=forbidden,
                describe=forbidden, eligible=forbidden, verify=forbidden, now=1000,
                isolate_failures=True)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(result['reviews'], [])
        self.assertEqual(result['waiting'][0]['reason'], 'lifecycle-run-identity')
        self.assertEqual(result['waiting'][1]['card'], '1234abce')


if __name__ == '__main__':
    unittest.main()
