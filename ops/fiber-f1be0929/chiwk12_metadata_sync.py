"""One-time, operator-approved local metadata replica; never copies credentials."""
import json
import fcntl
import os
from pathlib import Path
import socket
import sys
import urllib.request
import xml.etree.ElementTree as ET

OLD_CONFIG = Path('/home/mrarch/.local/state/syncthing/config.xml')
NEW_CONFIG = Path('/home/skuser01/.local/state/syncthing-fleet-fb673cd3/config.xml')
ROOT = '/home/skuser01/.skcapstone'
IGNORES = ['!/cards', '!/coordination/tasks', '!/coordination/agents',
           '!/coordination/archive', '!/coordination/card_events', '!/coordination/recovery',
           '!/coordination/seat-placement.json', '!/coordination/seat-control-plane.json',
           '!/fleet/objects', '!/fleet/placements', '!/fleet/status', '*']
OPTIONS = {'listenAddresses': ['tcp://127.0.0.1:22001'], 'globalAnnounceEnabled': False,
           'localAnnounceEnabled': False, 'relaysEnabled': False, 'natEnabled': False,
           'startBrowser': False, 'autoUpgradeIntervalH': 0, 'urAccepted': -1}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        """Never forward a local GUI credential to a redirected address."""
        return None


class Peer:
    def __init__(self, config, port):
        """Keep API keys only in memory, at their existing native boundary."""
        self.key = ET.parse(config).findtext('gui/apikey')
        if not self.key or port not in (8384, 8385):
            raise ValueError('unqualified-api')
        self.endpoint = 'http://127.0.0.1:' + str(port) + '/rest'
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, path, method='GET', body=None):
        """Use only native local Syncthing configuration and read endpoints."""
        request = urllib.request.Request(self.endpoint + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={'X-API-Key': self.key, 'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=20) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError('oversized-config-response')
        return json.loads(raw) if raw.strip() else None


def configure(old, new):
    """Filter a fresh replica before adding one local-only peer to the old instance."""
    old_id, new_id = old.call('/system/status')['myID'], new.call('/system/status')['myID']
    previous = old.call('/config/folders/skcapstone')
    if (old_id == new_id or previous['path'] != '/home/mrarch/.skcapstone'
            or previous['type'] != 'sendreceive'):
        raise ValueError('old-replica-mismatch')
    folders, devices = new.call('/config/folders'), new.call('/config/devices')
    if folders or any(row['deviceID'] != new_id for row in devices):
        raise ValueError('new-replica-not-empty')
    if any(row['deviceID'] == new_id for row in old.call('/config/devices')):
        raise ValueError('new-device-already-shared')
    new.call('/config/options', 'PATCH', OPTIONS)
    options = new.call('/config/options')
    if any(options.get(key) != value for key, value in OPTIONS.items()):
        raise ValueError('loopback-settings-readback-failed')
    folder = new.call('/config/defaults/folder')
    folder.update(id='skcapstone', label='SKFleet metadata only fb673cd3', path=ROOT,
                  type='sendreceive', paused=True, devices=[{'deviceID': new_id}])
    new.call('/config/folders', 'POST', folder)
    new.call('/db/ignores?folder=skcapstone', 'POST', {'ignore': IGNORES})
    if new.call('/db/ignores?folder=skcapstone').get('ignore') != IGNORES:
        raise ValueError('metadata-filter-readback-failed')
    # Native Syncthing silently drops unknown devices from folder definitions.
    # Register the peer first, then share the still-paused, filtered folder.
    entry = new.call('/config/defaults/device')
    entry.update(deviceID=old_id, name='chiwk12-mrarch-existing',
                 addresses=['tcp://127.0.0.1:22000'], introducer=False,
                 autoAcceptFolders=False, paused=False)
    new.call('/config/devices', 'POST', entry)
    new.call('/config/folders/skcapstone', 'PATCH',
             {'devices': [{'deviceID': new_id}, {'deviceID': old_id}]})
    shared = new.call('/config/folders/skcapstone')
    if (not shared['paused'] or len(shared['devices']) != 2
            or {row['deviceID'] for row in shared['devices']} != {new_id, old_id}):
        raise ValueError('new-sharing-readback-failed')
    entry = old.call('/config/defaults/device')
    entry.update(deviceID=new_id, name='chiwk12-skuser01-fleet-fb673cd3',
                 addresses=['tcp://127.0.0.1:22001'], introducer=False,
                 autoAcceptFolders=False, paused=False)
    old.call('/config/devices', 'POST', entry)
    current = old.call('/config/folders/skcapstone')
    # Native API has no conditional/add-item endpoint. This one-time operator
    # window is serialized locally; unrelated GUI/config writers must be idle.
    if current['devices'] != previous['devices']:
        raise ValueError('old-sharing-changed-during-setup')
    old.call('/config/folders/skcapstone', 'PATCH',
             {'devices': current['devices'] + [{'deviceID': new_id}]})
    after = old.call('/config/folders/skcapstone')['devices']
    by_id = {row['deviceID']: row for row in after}
    if (len(by_id) != len(after) or set(by_id) != {new_id, *[row['deviceID'] for row in current['devices']]}
            or any(by_id[row['deviceID']] != row for row in current['devices'])):
        raise ValueError('old-sharing-readback-failed')
    # Sharing is enabled last, after the native ignore parser accepted our filter.
    new.call('/config/folders/skcapstone', 'PATCH', {'paused': False})
    return {'old_device': old_id, 'new_device': new_id, 'root': ROOT,
            'prior_old_peer_ids': [row['deviceID'] for row in previous['devices']],
            'ignored_by_default': True, 'allowlist': IGNORES[:-1], 'credentials_copied': False}


if __name__ == '__main__':
    if os.getuid() != 0 or socket.gethostname().split('.')[0].lower() != 'chiwk12':
        sys.exit('This bounded migration runs only as the chiwk12 administrator.')
    try:
        descriptor = os.open(NEW_CONFIG.parent / 'operator-migration.lock',
                             os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print(json.dumps(configure(Peer(OLD_CONFIG, 8384), Peer(NEW_CONFIG, 8385)), sort_keys=True))
    except Exception as exc:
        # Native config remains for inspection/rollback; do not leak GUI keys or payloads.
        print(json.dumps({'state': 'blocked', 'error_type': type(exc).__name__,
                          'http_status': getattr(exc, 'code', None)}), file=sys.stderr)
        sys.exit(70)
