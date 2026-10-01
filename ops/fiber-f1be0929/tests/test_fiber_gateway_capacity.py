"""Gateway owns request concurrency; local records prevent duplicate cards."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
import subprocess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fiber_admission import Refused, reserve, select_host
import fiber_control as control
import fiber_lifecycle as lifecycle


class _FakeNativeGate:
    """Scoped stand-in for the installed-runtime native gate boundary.

    Only the unavailable ``skcapstone.coord_gate_diagnostic`` import is faked;
    ``fiber_control.eligible`` itself runs for real. The fake records whether
    ``diagnose`` was reached so a test can prove the local dependency-verdict
    refusal happens before native admission spends any work.
    """

    def __init__(self, decision=True):
        self.decision = decision
        self.calls = []

    def __enter__(self):
        self.module = types.ModuleType('skcapstone.coord_gate_diagnostic')
        self.module.diagnose = self.diagnose
        self._saved = sys.modules.get('skcapstone.coord_gate_diagnostic')
        sys.modules['skcapstone.coord_gate_diagnostic'] = self.module
        return self

    def diagnose(self, home, card_id):
        self.calls.append((str(home), card_id))
        return {'eligible': self.decision, 'reasons': [] if self.decision else ['synthetic']}

    def __exit__(self, *exc):
        if self._saved is None:
            sys.modules.pop('skcapstone.coord_gate_diagnostic', None)
        else:
            sys.modules['skcapstone.coord_gate_diagnostic'] = self._saved
        return False


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

    def test_transport_failures_do_not_starve_unrelated_lifecycle_cards(self):
        root = Path(self.directory.name)
        state = root / 'state'; state.mkdir(mode=0o700)
        qualified = root / 'qualified'; qualified.mkdir(mode=0o700)
        policy = root / 'policy.json'; policy.touch(mode=0o600)
        forbidden = lambda *a, **kw: self.fail('failed transport must not mutate native state')
        for failure in (subprocess.TimeoutExpired(['ssh'], 40),
                        subprocess.CalledProcessError(255, ['ssh'])):
            with self.subTest(failure=type(failure).__name__), \
                 patch.object(lifecycle, '_policy', return_value={'cards': {'1234abcd': 'a', '1234abce': 'b'}}), \
                 patch.object(lifecycle, '_run', side_effect=[{'state': 'terminal', 'token': 'a' * 32}, None]) as read, \
                 patch.object(lifecycle, '_producer', return_value=({}, {})), \
                 patch.object(lifecycle, 'collect_review', side_effect=forbidden):
                def collect(run):
                    raise failure
                result = lifecycle.connect_reviews(state, qualified, policy, root / 'HOLD',
                    home=root, data_root=root, read_cards=lambda ids: {}, collect_candidate=collect,
                    snapshot_revision=forbidden, record_verdict=forbidden, open_review=forbidden,
                    describe=forbidden, eligible=forbidden, verify=lambda *a: None, now=1000,
                    isolate_failures=True)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(result['reviews'], [])
            self.assertEqual(result['waiting'][0]['reason'], type(failure).__name__)
            self.assertEqual(result['waiting'][1]['card'], '1234abce')


class DependencyAdmissionTest(unittest.TestCase):
    """Every direct dependency must be DONE with an exact PASS verdict."""

    def row(self, dependencies=None, **overrides):
        row = {'id': '9e26a7d1', 'status': 'ready',
               'dependencies': list(dependencies or []), 'links': {}}
        row.update(overrides)
        return row

    def test_missing_verdict_is_rejected_before_native_gate(self):
        # Observed trial83b5ae19 mismatch: a DONE dependency whose links lack
        # verdict=PASS. The source must be refused before native admission.
        board = {'41a6039b': {'status': 'done', 'links': {}}}
        with _FakeNativeGate() as gate:
            self.assertFalse(control.eligible(self.row(['41a6039b']), board))
            self.assertEqual(gate.calls, [])

    def test_non_pass_verdicts_are_rejected_before_native_gate(self):
        for verdict in ('FAIL', 'BLOCKED', 'pass', 'PASS_FOR_REVIEW', ''):
            with self.subTest(verdict=verdict), _FakeNativeGate() as gate:
                board = {'41a6039b': {'status': 'done', 'links': {'verdict': verdict}}}
                self.assertFalse(control.eligible(self.row(['41a6039b']), board))
                self.assertEqual(gate.calls, [])

    def test_missing_or_non_done_dependency_rejected_even_with_pass(self):
        missing = {}
        with _FakeNativeGate() as gate:
            self.assertFalse(control.eligible(self.row(['41a6039b']), missing))
            self.assertEqual(gate.calls, [])
        for status in ('doing', 'review', 'backlog', 'ready'):
            with self.subTest(status=status), _FakeNativeGate() as gate:
                board = {'41a6039b': {'status': status, 'links': {'verdict': 'PASS'}}}
                self.assertFalse(control.eligible(self.row(['41a6039b']), board))
                self.assertEqual(gate.calls, [])

    def test_all_dependencies_done_pass_reach_native_gate_both_decisions(self):
        board = {'41a6039b': {'status': 'done', 'links': {'verdict': 'PASS'}},
                 '41a6039c': {'status': 'done', 'links': {'verdict': 'PASS'}}}
        row = self.row(['41a6039b', '41a6039c'])
        with _FakeNativeGate(decision=True) as gate:
            self.assertTrue(control.eligible(row, board))
            self.assertEqual(len(gate.calls), 1)
        with _FakeNativeGate(decision=False) as gate:
            self.assertFalse(control.eligible(row, board))
            self.assertEqual(len(gate.calls), 1)

    def test_rejected_source_does_not_block_separate_valid_source(self):
        board = {'41a6039b': {'status': 'done', 'links': {}},
                 '41a6039c': {'status': 'done', 'links': {'verdict': 'PASS'}}}
        invalid = self.row(['41a6039b'], id='9e26a7d1')
        valid = self.row(['41a6039c'], id='9e26a7d2')
        with _FakeNativeGate() as gate:
            self.assertFalse(control.eligible(invalid, board))
            self.assertTrue(control.eligible(valid, board))
            self.assertEqual(gate.calls, [(str(Path.home() / '.skcapstone'), '9e26a7d2')])

    def test_no_dependency_source_still_reaches_native_gate(self):
        with _FakeNativeGate() as gate:
            self.assertTrue(control.eligible(self.row([]), {}))
            self.assertEqual(gate.calls, [(str(Path.home() / '.skcapstone'), '9e26a7d1')])


if __name__ == '__main__':
    unittest.main()
