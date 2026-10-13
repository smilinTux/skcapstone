"""chiap08 authority for explicit, source-bound fleet stage requests."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request

from fiber_admission import ESTATE_CAP, STAGE_HOSTS, HOST_SLOTS, Refused, card_fingerprint, claim_for_worker, select_host
from fiber_dispatch import authority, dispatch_one, reconcile
from fiber_probe import collect
from fiber_worker import GATEWAY, ROUTES, source_binding, validate, verify_source

STATE = Path.home() / '.local/state/skfleet-fiber'
QUEUE = Path.home() / '.config/skfleet-fiber/jobs'
HOLD = Path.home() / '.config/skfleet-fiber/HOLD'
LIBRARY = '.local/lib/skfleet-fiber/fiber_worker.py'
CAPS = {'deepseek': 3, 'codex': 2, 'zai': 5}


def cards(card_ids):
    """Read queued cards and direct dependencies, never the entire board."""
    found = {}
    def read(card):
        if card in found:
            return
        if not re.fullmatch('[0-9a-f]{8}', str(card)):
            raise Refused('invalid-card')
        result = subprocess.run(['skcapstone', 'coord', 'show', card, '--json'], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise Refused('board-read-failed')
        row = json.loads(result.stdout)
        if not isinstance(row, dict) or row.get('id') != card or 'owner' not in row:
            raise Refused('ambiguous-board-row')
        found[card] = row
    for card in dict.fromkeys(card_ids):
        read(card)
        for dependency in found[card].get('dependencies', []):
            read(dependency)
    return found


def eligible(row, board):
    from skcapstone.coord_gate_diagnostic import diagnose
    if row.get('owner') or row.get('archived') or row.get('status') not in {'backlog', 'ready', 'doing', 'review'}:
        return False
    if any(board.get(cid, {}).get('status') != 'done' for cid in row.get('dependencies', [])):
        return False
    return diagnose(Path.home() / '.skcapstone', row['id']).get('eligible') is True


def describe(card, stage, family, *, producer_family=None):
    row = cards([card]).get(card)
    if row is None:
        raise Refused('unknown-card')
    repository, base_ref, revision = source_binding(row)
    pi_provider, model = ROUTES[family]
    sizes = re.findall(r'\[(S|M|L|XL)\]', row.get('title', ''))
    if len(sizes) != 1:
        raise Refused('work-class-required')
    if stage == 'review' and (producer_family not in ROUTES or producer_family == family):
        raise Refused('review-independence')
    task = {'card': card, 'owner': ('pi-seraph-fiber-' if stage == 'review' else 'pi-fiber-' + stage + '-') + card,
            'stage': stage, 'host': 'auto', 'work_class': sizes[0], 'provider': family,
            'pi_provider': pi_provider, 'model': model, 'repository': repository,
            'base_ref': base_ref, 'base_revision': revision,
            'card_fingerprint': card_fingerprint(row)}
    if stage == 'review':
        task['producer_provider'] = producer_family
    return task


def remote(mode, request):
    job = request['job']
    if job['host'] not in HOST_SLOTS:
        raise Refused('host-outside-initial-placement')
    account = 'mrarch' if job['host'] == 'chiwk12' else 'skuser01'
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', account + '@' + job['host'],
               shlex.join(['/home/' + account + '/.skenv/bin/python', '/home/' + account + '/' + LIBRARY, mode])]
    result = subprocess.run(command, input=json.dumps(request), capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise Refused('remote-' + mode + '-failed')
    return json.loads(result.stdout)


def observe(run):
    job = json.loads(run['binding'])
    receipt = {key: run[key] for key in ['card', 'host', 'token', 'unit']}
    receipt['provider'] = job['provider']
    return remote('observe', {'job': job, 'receipt': receipt})


def launch_claimed(job, receipt):
    """Claim only on the authority, then bind the exact generation to the run."""
    if socket.gethostname().split('.')[0].lower() != 'chiap08':
        raise Refused('not-authority-host')
    row = claim_for_worker(job['card'], job['owner'])
    verify_source(row, job)
    claim = {'authority': 'chiap08', 'owner': job['owner'],
             'revision': row['meta']['_claim_revision']}
    return remote('start', {'job': job, 'receipt': receipt, 'claim': claim})


def validate_completion(job, answer, headers):
    """Require exact attribution, including the upstream's returned model.

    Native bucket admission selects the qualified member. A different served
    model is not evidence that member completed, even within the same family.
    """
    family, model = job['provider'], job['model']
    served = answer.get('model')
    content = answer.get('choices', [{}])[0].get('message', {}).get('content')
    if not isinstance(content, str) or not content.strip():
        raise Refused('completion-empty')
    if headers.get('x-sk-provider', headers.get('x-sk-backend')) != family or headers.get('x-sk-model-requested') != model:
        raise Refused('completion-attribution-mismatch')
    expected = headers.get('x-sk-bucket-member') if model.startswith('sk-') else model
    if not expected or served != expected or headers.get('x-sk-model-served') != served:
        raise Refused('completion-model-substitution')
    return served


def qualify_route(job):
    """One tiny completion through the configured SKGateway alias, no Matter data."""
    from skcapstone.fleet_lane_health import acquire_lane_snapshot, lane_health
    family, model = job['provider'], job['model']
    stamp = str(time.time_ns())
    path = STATE / 'observations' / ('lane-' + stamp + '.json')
    endpoint = GATEWAY.removesuffix('/v1')
    snapshot = acquire_lane_snapshot(endpoint, [{'name': family, 'model': model}],
                                     {family: (family,)}, path, stamp)
    ok, reason = lane_health(snapshot, family, model, cycle_id=stamp, endpoint=endpoint,
                             capacity_domains=(family,), active_revision=snapshot['runtime_revision'])
    if not ok:
        raise Refused('lane-health:' + reason)
    catalog = json.loads((Path.home() / '.pi/agent/models.json').read_text())
    provider = catalog.get('providers', {}).get(job['pi_provider'], {})
    if provider.get('baseUrl', '').rstrip('/') != GATEWAY or model not in {row['id'] for row in provider.get('models', [])}:
        raise Refused('authority-catalog-mismatch')
    key = provider.get('apiKey')
    if not isinstance(key, str) or not key or key.startswith('!'):
        raise Refused('credential-reference-unavailable')
    if re.fullmatch('[A-Z_][A-Z0-9_]*', key):
        key = os.environ.get(key)
        if not key:
            raise Refused('credential-environment-unavailable')
    request = urllib.request.Request(GATEWAY + '/chat/completions',
        data=json.dumps({'model': model, 'messages': [{'role': 'user', 'content': 'Reply only OK.'}], 'max_tokens': 256, 'stream': False}).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            payload = response.read(65537)
            headers = {key.lower(): value for key, value in response.headers.items()}
        if len(payload) > 65536:
            raise Refused('completion-response-too-large')
        answer = json.loads(payload)
    except urllib.error.HTTPError as exc:
        raise Refused('completion-http-' + str(exc.code)) from None
    served = validate_completion(job, answer, headers)
    proof = {'observed_at': time.time(), 'requested_model': model, 'served_model': served,
             'provider': family, 'gateway_revision': snapshot['runtime_revision'], 'nonempty': True,
             'resolved_model': headers.get('x-sk-bucket-member', model)}
    evidence = STATE / 'observations' / ('completion-' + stamp + '.json')
    with evidence.open('x') as stream:
        json.dump(proof, stream, sort_keys=True)
    return {'ready': True, 'cap': CAPS[family], 'proof': proof}


def observation(job):
    snapshot = collect()
    if not snapshot['complete']:
        raise Refused('incomplete-observation')
    if len(snapshot['workers']) >= ESTATE_CAP:
        raise Refused('estate-capacity')
    if not snapshot['hosts'].get(job['host'], {}).get('ready'):
        raise Refused('host-not-ready')
    token = '0' * 32
    check = remote('check', {'job': job, 'receipt': {'card': job['card'], 'host': job['host'],
                   'provider': job['provider'], 'token': token,
                   'unit': 'skfleet-fiber-' + job['card'] + '-' + token + '.service'}})
    if check.get('state') != 'ready' or check.get('host') != job['host'] or check.get('card') != job['card'] or check.get('model') != job['model']:
        raise Refused('remote-preflight-mismatch')
    # No completion calls while the estate is already full.
    proof = qualify_route(job)
    # Re-probe after the potentially slow completion, not before it.
    snapshot = collect()
    snapshot['providers'][job['provider']] = proof
    return snapshot


def prepare_job(job):
    """Resolve placement and runtime before spending a provider probe.

    Called under dispatch_one's authority lock. Failed runtime candidates do
    not prevent trying another allowed host. No claim or launch occurs here.
    """
    snapshot = collect()
    rejected = []
    while True:
        try:
            host = select_host(STATE / 'admission.sqlite3', job, snapshot, now=time.time())
        except Refused as exc:
            if str(exc) == 'no-eligible-host' and rejected:
                raise Refused('no-runtime-qualified-host:' + ','.join(rejected)) from None
            raise
        chosen = dict(job, host=host)
        token = '0' * 32
        try:
            check = remote('check', {'job': chosen, 'receipt': {'card': job['card'], 'host': host,
                'provider': job['provider'], 'token': token, 'unit': 'skfleet-fiber-' + job['card'] + '-' + token + '.service'}})
            if any(check.get(k) != v for k, v in {'state': 'ready', 'card': job['card'], 'host': host, 'model': job['model']}.items()):
                raise Refused('remote-preflight-mismatch')
            break
        except (Refused, subprocess.SubprocessError):
            rejected.append(host)
            snapshot['hosts'][host] = dict(snapshot['hosts'][host], ready=False)
    proof = qualify_route(chosen)
    fresh = collect()
    fresh['providers'][job['provider']] = proof
    return chosen, fresh


def cycle(check=False):
    if socket.gethostname().split('.')[0].lower() != 'chiap08':
        raise Refused('not-authority-host')
    if check:
        return {'mode': 'check', 'observation': collect(), 'queued': len(list(QUEUE.glob('*.json')))}
    with authority(STATE):
        recovered = reconcile(STATE, observe)
        if HOLD.exists():
            return {'state': 'admission-held', 'reconciled': recovered}
    paths = sorted(QUEUE.glob('*.json'))
    if not paths:
        return {'state': 'waiting-for-reviewed-requests', 'reconciled': recovered}
    jobs = [json.loads(path.read_text()) for path in paths]
    board = cards([job['card'] for job in jobs])
    results = []
    for job in jobs:
        token = '0' * 32
        validate({'job': job, 'receipt': {'card': job['card'], 'host': job['host'],
                  'provider': job['provider'], 'token': token, 'unit': 'skfleet-fiber-' + job['card'] + '-' + token + '.service'}})
        row = board.get(job['card'])
        if not row:
            results.append({'card': job['card'], 'state': 'blocked', 'reason': 'unknown-card'})
            continue
        try:
            verify_source(row, job)
            job['claimable'] = eligible(row, board)
            result = dispatch_one(STATE, job, lambda: observation(job),
                launch_claimed, observe, hold_file=HOLD, prepare=prepare_job)
        except Refused as exc:
            result = {'card': job['card'], 'state': 'waiting', 'reason': str(exc)}
        results.append(result)
        if result.get('new_launch') or result.get('reason') in {'estate-capacity', 'actual-start-spacing', 'unresolved-launch', 'authority-busy'}:
            break
    return {'state': 'cycle-finished', 'jobs': results}


def set_hold():
    if socket.gethostname().split('.')[0].lower() != 'chiap08':
        raise Refused('not-authority-host')
    # Freeze and admission use the same lock. An already-acknowledged worker
    # is preserved; no subsequent dispatch can slip between freeze and launch.
    with authority(STATE):
        HOLD.parent.mkdir(parents=True, exist_ok=True)
        if not HOLD.exists():
            with HOLD.open('x') as stream:
                stream.write('New admission held by f1be0929 operator. Existing workers are preserved.\n')
        return {'state': 'admission-held', 'workers_stopped': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--hold', action='store_true')
    parser.add_argument('--describe-card')
    parser.add_argument('--stage', choices=sorted(STAGE_HOSTS), default='implementation')
    parser.add_argument('--family', choices=sorted(ROUTES), default='deepseek')
    parser.add_argument('--producer-family', choices=sorted(ROUTES))
    args = parser.parse_args()
    try:
        value = set_hold() if args.hold else describe(args.describe_card, args.stage, args.family, producer_family=args.producer_family) if args.describe_card else cycle(check=not args.once)
        print(json.dumps(value, sort_keys=True))
    except Refused as exc:
        print(json.dumps({'state': 'waiting', 'reason': str(exc)}))
    except Exception as exc:
        print(json.dumps({'state': 'blocked', 'reason': type(exc).__name__}))
        raise SystemExit(70)
