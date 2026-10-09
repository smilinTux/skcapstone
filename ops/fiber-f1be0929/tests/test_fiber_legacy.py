"""Reuse the native per-node report-only gate without stopping sknoded."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from skcapstone.fleet import builder_dispatch, node_controller, store
from skcapstone.fleet.paths import FleetPaths


class LegacyAdmissionTest(unittest.TestCase):
    def test_report_only_toggle_preserves_spec_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = FleetPaths(Path(directory))
            writer = store.Writer(role='operator', node='node-chiap08', identity='capauth:test')
            spec = {'actuate': True, 'role': 'builder-standby', 'cordoned': False,
                    'address': {'hostname': 'ZIOWK01'}, 'taints': []}
            labels = {'builder-capacity': '10', 'host': 'ziowk01'}
            store.write_spec(paths, 'node', 'node-ziowk01', spec, labels=labels, writer=writer)
            held = node_controller.set_actuation(paths, 'node-ziowk01', False, writer=writer)
            self.assertEqual(held['spec'], dict(spec, actuate=False))
            self.assertEqual(held['labels'], labels)
            restored = node_controller.set_actuation(paths, 'node-ziowk01', True, writer=writer)
            self.assertEqual(restored['spec'], spec)
            self.assertEqual(restored['labels'], labels)

    def test_report_only_consumer_does_not_touch_jobs_or_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = FleetPaths(Path(directory) / 'fleet')
            launcher, materializer = Mock(), Mock()
            with patch.object(builder_dispatch.store, 'read_spec', return_value={
                    'spec': {'role': 'builder-standby', 'actuate': False}}), \
                 patch.object(builder_dispatch.store, 'actuation_allowed') as actuation:
                self.assertIsNone(builder_dispatch.consume_one(paths, Path(directory), 'node-ziowk01',
                                                               launcher=launcher, materializer=materializer))
                launcher.assert_not_called()
                materializer.assert_not_called()
                actuation.assert_not_called()


if __name__ == '__main__':
    unittest.main()
