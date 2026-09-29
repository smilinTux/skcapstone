"""Read-only live worker and node observations for central fleet admission.

Never logs command lines, prompts, auth files, or unfiltered environments.
Uses existing SKCapstone headroom policy; inventory labels alone are not readiness.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import socket
import subprocess
import time
import urllib.request

HOSTS = ('chiap01', 'chiap02', 'chiap03', 'chiap04', 'chiap08', 'chiwk12', 'chiwk13', 'ziowk01')
SSH_ACCOUNT = {'chiwk12': 'mrarch@chiwk12'}
GATEWAY = 'http://chiap01:18790'
MODEL_FAMILY = {'deepseek-flash': 'deepseek', 'deepseek-v4-pro': 'deepseek',
                'gpt-5.6-sol': 'codex', 'gpt-5.6-terra': 'codex', 'sk-zai-m': 'zai'}


def process_provider(name, identity):
    if name == 'codex':
        return 'codex'
    family = MODEL_FAMILY.get(identity.get('SKFLEET_MODEL'))
    return family if family and identity.get('SKFLEET_PROVIDER') == family else None


def footer_provider(text):
    """Read only the final Pi status line; names and old output are not routes."""
    lines = text.rstrip().splitlines()
    if lines and re.fullmatch(r'🔌 MCP: \d+ servers enabled', lines[-1].strip()):
        lines.pop()
    match = re.search(r'\((skgateway|skgw-deepseek|skgw-codex|skgw-zai)\) ([a-z0-9.-]+) • [a-z]+\s*$', lines[-1]) if lines else None
    if not match:
        return None
    provider, model = match.groups()
    family = MODEL_FAMILY.get(model)
    return family if family and provider in {'skgateway', 'skgw-' + family} else None


def logical_workers(workers):
    """Attached native app servers are components, not extra agent sessions."""
    codex_pids = {row['pid'] for row in workers if row['runtime'] == 'codex'}
    return [row for row in workers if not (row['runtime'] == 'codex'
            and row.get('mode') == 'app-server' and row.get('parent_pid') in codex_pids)]


def node_observation():
    host = socket.gethostname().split('.')[0].lower()
    command = subprocess.run(['systemctl', '--user', 'show', 'sknoded.service',
                              '-p', 'ActiveState', '-p', 'MainPID'], capture_output=True, text=True, timeout=8)
    service = dict(line.split('=', 1) for line in command.stdout.splitlines() if '=' in line)
    pid = service.get('MainPID', '0')
    process_ok = pid.isdecimal() and pid != '0' and Path('/proc', pid).exists()
    beat_path = Path.home() / '.skcapstone/fleet/status' / ('node-' + host) / 'heartbeat.json'
    try:
        beat = json.loads(beat_path.read_text())
        beat_age = time.time() - datetime.strptime(beat['ts'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
    except (OSError, ValueError, KeyError, TypeError):
        beat_age = None
    gateway_ok, gateway_error = False, None
    try:
        with urllib.request.urlopen(GATEWAY + '/health', timeout=12) as response:
            payload = response.read(65537)
        gateway_ok = len(payload) <= 65536 and json.loads(payload).get('status') == 'ok'
    except (OSError, ValueError, AttributeError) as exc:
        gateway_error = type(exc).__name__
    workers = []
    for directory in Path('/proc').iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            name = (directory / 'comm').read_text().strip()
            if name not in {'pi', 'codex', 'claude', 'cursor-agent', 'opencode', 'gemini'}:
                continue
            # Only explicitly allowlisted identity keys leave this process.
            identity = {}
            try:
                for item in (directory / 'environ').read_bytes().split(b'\0'):
                    key, _, value = item.partition(b'=')
                    if key in {b'HERDR_PANE_ID', b'SKFLEET_CARD_ID', b'SKFLEET_PROVIDER', b'SKFLEET_MODEL'}:
                        identity[key.decode()] = value.decode(errors='replace')
            except PermissionError:
                pass
            card = identity.get('SKFLEET_CARD_ID', '')
            parent = next(int(line.split()[1]) for line in (directory / 'status').read_text().splitlines() if line.startswith('PPid:'))
            mode = 'agent-cli'
            if name == 'codex':
                # Keep only a recognized command kind, never argv or prompts.
                for arg in (directory / 'cmdline').read_bytes().split(b'\0')[:5]:
                    if arg in {b'app-server', b'mcp-server', b'exec', b'resume'}:
                        mode = arg.decode()
            key = 'card:' + card if re.fullmatch('[0-9a-f]{8}', card) else f'process:{host}:{directory.name}'
            workers.append({'key': key, 'host': host, 'provider': process_provider(name, identity),
                            'runtime': name, 'mode': mode, 'parent_pid': parent,
                            'pid': int(directory.name), 'pane': identity.get('HERDR_PANE_ID')})
        except (FileNotFoundError, ProcessLookupError):
            continue
    return {'host': host, 'observed_at': time.time(), 'service_active': command.returncode == 0 and service.get('ActiveState') == 'active',
            'service_pid_live': process_ok, 'heartbeat_age': beat_age, 'gateway_ok': gateway_ok, 'gateway_error': gateway_error,
            'meminfo': Path('/proc/meminfo').read_text(), 'workers': logical_workers(workers)}


def probe_host(host):
    if socket.gethostname().split('.')[0].lower() == host:
        value = node_observation()
        value['received_at'] = time.time()
        return value
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', SSH_ACCOUNT.get(host, host), 'python3', '-', '--node']
    result = subprocess.run(command, input=Path(__file__).read_text(), capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise ValueError('host-probe-failed:' + host)
    value = json.loads(result.stdout)
    if value.get('host') != host:
        raise ValueError('host-identity-mismatch:' + host)
    # Timestamp receipt on the authority host. Cross-host clock skew is not
    # elapsed transport time; SSH already bounds this fresh probe to 20s.
    value['received_at'] = time.time()
    return value


def collect():
    from skcapstone.fleet.capacity import admit_headroom
    errors, hosts, workers, received = [], {}, [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = {host: pool.submit(probe_host, host) for host in HOSTS}
        for host, future in results.items():
            try:
                observed = future.result()
                if not 0 <= time.time() - observed['received_at'] <= 30:
                    raise ValueError('stale-host')
                received.append(observed['received_at'])
                headroom, reason, memory = admit_headroom(observed['meminfo'])
                beat = observed['heartbeat_age']
                ready = observed['service_active'] and observed['service_pid_live'] and observed['gateway_ok'] and beat is not None and 0 <= beat <= 180 and headroom
                configured = {'chiap02': 2, 'chiap03': 1, 'chiap04': 1}.get(host, 0)
                memory_slots = max(0, (memory['MemAvailable'] - 1048576) // 2097152) if memory else 0
                hosts[host] = {'ready': bool(ready), 'slots': min(configured, memory_slots),
                               'service_active': observed['service_active'], 'service_pid_live': observed['service_pid_live'],
                               'gateway_ok': observed['gateway_ok'], 'gateway_error': observed['gateway_error'],
                               'heartbeat_age': beat, 'headroom': reason, 'memory': memory}
                workers.extend(observed['workers'])
            except Exception as exc:
                errors.append(host + ':' + type(exc).__name__)
                hosts[host] = {'ready': False, 'slots': 0}
    # Herdr detection is corroborated with a live process, never used alone.
    result = subprocess.run(['herdr', 'agent', 'list'], capture_output=True, text=True, timeout=10)
    if result.returncode:
        errors.append('herdr-unavailable')
    else:
        agents = json.loads(result.stdout)['result']['agents']
        by_pane = {agent['pane_id']: agent for agent in agents}
        # Read-only current status evidence. Never retain or log pane contents.
        # Failed reads leave unknown occupancy consuming every provider budget.
        routes = {}
        for worker in workers:
            pane = worker['pane']
            agent = by_pane.get(pane) if worker['host'] == 'chiap08' else None
            if not agent or agent.get('agent_status') in {'idle', 'done'} or worker.get('runtime') != 'pi' or pane in routes:
                continue
            try:
                result = subprocess.run(['herdr', 'agent', 'read', pane, '--source', 'detection', '--lines', '2'],
                                        capture_output=True, text=True, timeout=3)
                routes[pane] = footer_provider(result.stdout) if result.returncode == 0 else None
            except subprocess.TimeoutExpired:
                routes[pane] = None
        filtered = []
        for worker in workers:
            agent = by_pane.get(worker['pane']) if worker['host'] == 'chiap08' else None
            if agent and agent.get('agent_status') in {'idle', 'done'}:
                continue
            if agent:
                worker['name'] = agent.get('name')
                if worker.get('runtime') == 'pi':
                    worker['provider'] = routes.get(worker['pane'])
            filtered.append(worker)
        workers = filtered
        live_panes = {worker['pane'] for worker in workers if worker['host'] == 'chiap08'}
        if any(agent.get('agent_status') == 'working' and agent['pane_id'] not in live_panes for agent in agents):
            errors.append('working-pane-without-process')
    return {'observed_at': min(received) if received else time.time(), 'complete': not errors, 'hosts': hosts,
            'workers': workers, 'providers': {}, 'errors': errors}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--node', action='store_true')
    args = parser.parse_args()
    print(json.dumps(node_observation() if args.node else collect(), sort_keys=True))
