"""Metadata replication must be filtered before the old peer shares anything."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chiwk12_metadata_sync import IGNORES, OPTIONS, configure


class Peer:
    def __init__(self, name, broken_ignores=False):
        self.name, self.broken_ignores = name, broken_ignores
        self.writes = []
        self.folders = {} if name == 'new' else {'skcapstone': {
            'id': 'skcapstone', 'path': '/home/mrarch/.skcapstone',
            'type': 'sendreceive', 'devices': [{'deviceID': 'old'}]}}
        self.options = {}
        self.devices = [{'deviceID': name}]

    def call(self, path, method='GET', body=None):
        if method != 'GET':
            self.writes.append((path, method, body))
        if path == '/system/status':
            return {'myID': self.name}
        if path == '/config/folders':
            if method == 'POST':
                self.folders[body['id']] = body
            return list(self.folders.values())
        if path == '/config/devices':
            if method == 'POST':
                self.devices.append(body)
            return self.devices
        if path.startswith('/config/defaults/'):
            return {}
        if path == '/config/options':
            if body:
                self.options.update(body)
            return self.options
        if path == '/db/ignores?folder=skcapstone':
            return {'ignore': [] if self.broken_ignores else IGNORES}
        if path == '/config/folders/skcapstone':
            if method == 'PATCH':
                self.folders['skcapstone'].update(body)
            return copy.deepcopy(self.folders['skcapstone'])
        raise AssertionError(path)


class MetadataSyncTest(unittest.TestCase):
    def test_only_loopback_and_allowlisted_metadata_is_shared(self):
        old, new = Peer('old'), Peer('new')
        result = configure(old, new)
        self.assertEqual(result['old_device'], 'old')
        self.assertEqual(result['prior_old_peer_ids'], ['old'])
        self.assertEqual(new.options, OPTIONS)
        self.assertEqual(new.options['listenAddresses'], ['tcp://127.0.0.1:22001'])
        self.assertFalse(new.folders['skcapstone']['paused'])
        self.assertEqual(IGNORES[-1], '*')
        for unsafe in ('!/agents', '!/identity', '!/config', '!/coordination', '!/fleet', '!/evidence'):
            self.assertNotIn(unsafe, IGNORES)
        self.assertEqual([op[0] for op in old.writes], ['/config/devices', '/config/folders/skcapstone'])
        self.assertEqual(old.folders['skcapstone']['devices'], [{'deviceID': 'old'}, {'deviceID': 'new'}])

    def test_unverified_filter_never_modifies_existing_instance(self):
        old, new = Peer('old'), Peer('new', broken_ignores=True)
        with self.assertRaises(ValueError):
            configure(old, new)
        self.assertEqual(old.writes, [])
        self.assertTrue(new.folders['skcapstone']['paused'])

    def test_unknown_existing_replica_is_not_overwritten(self):
        old, new = Peer('old'), Peer('new')
        new.folders['unexpected'] = {'id': 'unexpected', 'path': '/another/path'}
        with self.assertRaises(ValueError):
            configure(old, new)
        self.assertEqual(old.writes, [])
        self.assertEqual(new.writes, [])

    def test_failed_old_peer_readback_keeps_new_folder_paused(self):
        old, new = Peer('old'), Peer('new')
        original = old.call
        def damaged(path, method='GET', body=None):
            result = original(path, method, body)
            if path == '/config/folders/skcapstone' and method == 'PATCH':
                old.folders['skcapstone']['devices'] = [{'deviceID': 'new'}]
            return result
        old.call = damaged
        with self.assertRaisesRegex(ValueError, 'old-sharing-readback-failed'):
            configure(old, new)
        self.assertTrue(new.folders['skcapstone']['paused'])

    def test_detected_intervening_peer_change_is_not_overwritten(self):
        old, new = Peer('old'), Peer('new')
        original, reads = old.call, []
        def changed(path, method='GET', body=None):
            if path == '/config/folders/skcapstone' and method == 'GET':
                reads.append(1)
                if len(reads) == 2:
                    old.folders['skcapstone']['devices'].append({'deviceID': 'other'})
            return original(path, method, body)
        old.call = changed
        with self.assertRaisesRegex(ValueError, 'old-sharing-changed-during-setup'):
            configure(old, new)
        self.assertTrue(new.folders['skcapstone']['paused'])
        self.assertNotIn('/config/folders/skcapstone', [row[0] for row in old.writes])
