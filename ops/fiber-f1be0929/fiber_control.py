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
import sqlite3
import hashlib

from fiber_admission import ESTATE_CAP, STAGE_HOSTS, HOST_SLOTS, Refused, card_fingerprint, claim_for_worker, select_host
from fiber_dispatch import authority, dispatch_one, reconcile
from fiber_probe import collect
from fiber_refill import check_qualification, refill_queue
from fiber_selection import select_requests
from fiber_lifecycle import connect_reviews
import fiber_bundle
from fiber_worker import GATEWAY, ROUTES, source_binding, validate, verify_source

STATE = Path.home() / '.local/state/skfleet-fiber'
QUEUE = Path.home() / '.config/skfleet-fiber/jobs'
HOLD = Path.home() / '.config/skfleet-fiber/HOLD'
LIBRARY = '.local/lib/skfleet-fiber/fiber_worker.py'
CAPS = {'deepseek': 3, 'codex': 2, 'zai': 5}


def record_attempt(job, reason):
    """Bound repeated preflight faults; never turn ambiguity into a retry."""
    waits = {'estate-capacity', 'host-capacity', 'provider-capacity', 'actual-start-spacing',
             'launch-spacing', 'card-not-claimable', 'authority-busy', 'admission-held'}
    if not reason or reason in waits:
        return 0
    binding = json.dumps({k: v for k, v in job.items() if k != 'claimable'}, sort_keys=True)
    key = hashlib.sha256(binding.encode()).hexdigest()
    with authority(STATE):
        db = sqlite3.connect(STATE / 'retry-budget.sqlite3')
        try:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS faults (binding TEXT PRIMARY KEY, attempts INTEGER NOT NULL)')
                db.execute('INSERT INTO faults VALUES (?, 1) ON CONFLICT(binding) DO UPDATE SET attempts=attempts+1', (key,))
                attempts = db.execute('SELECT attempts FROM faults WHERE binding=?', (key,)).fetchone()[0]
        finally:
            db.close()
        ambiguous = reason in {'unresolved-launch', 'orphan-reservation', 'launch-unacknowledged', 'launch-receipt-invalid', 'worker-failed'}
        if (attempts >= 3 or ambiguous) and not HOLD.exists():
            HOLD.parent.mkdir(parents=True, exist_ok=True)
            with HOLD.open('x') as stream:
                stream.write('Guarded admission stopped: ' + ('ambiguous launch' if ambiguous else 'three preflight faults')
                             + '. Inspect card ' + job['card'] + ' before operator resumption. Existing work preserved.\n')
        return attempts


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
    """Reject obvious holds locally; require the native gate for admission."""
    if row.get('owner') or row.get('archived') or row.get('status') not in {'backlog', 'ready', 'doing', 'review'}:
        return False
    if any(board.get(cid, {}).get('status') != 'done' for cid in row.get('dependencies', [])):
        return False
    try:
        from skcapstone.coord_gate_diagnostic import diagnose
    except ImportError:
        raise Refused('board-gate-unavailable') from None
    return diagnose(Path.home() / '.skcapstone', row['id']).get('eligible') is True


def describe(card, stage, family, *, producer_family=None):
    row = cards([card]).get(card)
    if row is None:
        raise Refused('unknown-card')
    repository, base_ref, revision = source_binding(row, review=stage == 'review')
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
    account = 'skuser01'
    program = [LIBRARY, mode]
    wire = request
    if mode == 'candidate':
        program = ['.local/lib/skfleet-fiber/fiber_candidate.py', '--bundle']
    elif mode == 'bundle':
        program = ['.local/lib/skfleet-fiber/fiber_bundle.py']
        wire = request['bundle']
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', account + '@' + job['host'],
               shlex.join(['/home/' + account + '/.skenv/bin/python',
                           '/home/' + account + '/' + program[0], *program[1:]])]
    result = subprocess.run(command, input=json.dumps(wire), capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise Refused('remote-' + mode + '-failed')
    return json.loads(result.stdout)


def observe(run):
    job = json.loads(run['binding'])
    receipt = {key: run[key] for key in ['card', 'host', 'token', 'unit']}
    receipt['provider'] = job['provider']
    receipt['invocation'] = run.get('invocation')
    return remote('observe', {'job': job, 'receipt': receipt})


def collect_candidate(run):
    """Ask the same host for a fresh, sandboxed terminal candidate export."""
    job = json.loads(run['binding'])
    receipt = {key: run[key] for key in ['card', 'host', 'token', 'unit', 'invocation']}
    receipt['provider'] = job['provider']
    return remote('candidate', {'job': job, 'receipt': receipt})


def stage_bundle(job):
    """Copy opaque exact bytes before worker admission, without Git or a launch."""
    import base64
    value = fiber_bundle.select(job)
    if value is None:
        return
    root = Path.home() / '.local/share/skfleet-fiber'
    fiber_bundle._directory(root)
    fiber_bundle._directory(root / 'bundles')
    raw = fiber_bundle.read_blob(root / 'bundles' / (value['sha256'] + '.bundle'), value['sha256'])
    answer = remote('bundle', {'job': job, 'bundle': {
        'sha256': value['sha256'], 'bundle_b64': base64.b64encode(raw).decode()}})
    if answer != {'state': 'stored', 'sha256': value['sha256'], 'bytes': len(raw)}:
        raise Refused('bundle-transfer-unverified')


def native_revision(card):
    """Read the existing native snapshot including its latest typed outcome."""
    from skcapstone.seraph_review_cardstore import LiveCardStoreGateway
    return LiveCardStoreGateway(Path.home() / '.skcapstone').read_card(card).revision


def record_candidate(intent):
    """Relay a review request through the exact guarded CLI write boundary."""
    job, packet = intent['job'], intent['candidate']
    fiber_bundle.read_blob(Path(intent['evidence_path']), packet['evidence_sha256'])
    command = ['skcapstone', 'coord', 'verdict', job['card'], 'PASS_FOR_REVIEW',
               '--candidate', intent['evidence_path'], '--commit', packet['commit'],
               '--tree', packet['tree'], '--ref', packet['ref'], '--agent', job['owner'],
               '--expected-source-revision', intent['source_revision'],
               '--expected-claim-revision', intent['claim_revision'],
               '--expected-candidate-sha256', packet['evidence_sha256'],
               '--transition-id', intent['transition_id']]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise Refused('lifecycle-native-verdict-refused')


def open_candidate_review(intent, revision):
    """Use Chef's mediated review opener, not the old script's launcher."""
    command = ['skcapstone', 'coord', 'review-work', intent['job']['card'],
               '--producer', intent['job']['owner'], '--agent', 'link',
               '--expected-source-revision', revision,
               '--expected-claim-revision', intent['claim_revision']]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise Refused('lifecycle-native-review-refused')
    return json.loads(result.stdout)


def launch_claimed(job, receipt):
    """Claim only on the authority, then bind the exact generation to the run."""
    if socket.gethostname().split('.')[0].lower() != 'chiap08':
        raise Refused('not-authority-host')
    row = claim_for_worker(job['card'], job['owner'])
    verify_refill_request(row, job)
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
    recheck_qualification(job)
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
            stage_bundle(chosen)
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
    recheck_qualification(chosen)
    return chosen, fresh


def recheck_qualification(job):
    """Refill authority must survive queue waits and slow provider probes."""
    try:
        check_qualification(job, QUEUE.parent / 'qualified', read_cards=cards,
                            eligible=eligible, verify=verify_refill_request, clock=time.time)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, Refused):
            raise
        raise Refused('refill-qualification-unavailable') from None


def verify_refill_request(row, job):
    """Apply existing route, stage and source validation before queueing."""
    token = '0' * 32
    validate({'job': job, 'receipt': {'card': job['card'], 'host': job['host'],
              'provider': job['provider'], 'token': token,
              'unit': 'skfleet-fiber-' + job['card'] + '-' + token + '.service'}})
    verify_source(row, job)
    if 'source_bundle' in job:
        meta = row.get('meta') or {}
        if native_revision(meta.get('link_source_card')) != meta.get('source_revision'):
            raise Refused('lifecycle-source-superseded')


def cycle(check=False):
    if socket.gethostname().split('.')[0].lower() != 'chiap08':
        raise Refused('not-authority-host')
    if check:
        return {'mode': 'check', 'observation': collect(), 'queued': len(list(QUEUE.glob('*.json')))}
    with authority(STATE):
        recovered = reconcile(STATE, observe)
        if any(run.get('state') == 'terminal' and run.get('exit_code') != 0 for run in recovered) and not HOLD.exists():
            HOLD.parent.mkdir(parents=True, exist_ok=True)
            with HOLD.open('x') as stream:
                stream.write('Guarded admission stopped after a failed worker. Inspect terminal receipts before resuming.\n')
        if HOLD.exists():
            return {'state': 'admission-held', 'reconciled': recovered}
        lifecycle = connect_reviews(STATE, QUEUE.parent / 'qualified',
            QUEUE.parent / 'lifecycle.json', HOLD, home=Path.home() / '.skcapstone',
            data_root=Path.home() / '.local/share/skfleet-fiber', read_cards=cards,
            collect_candidate=collect_candidate, snapshot_revision=native_revision,
            record_verdict=record_candidate, open_review=open_candidate_review,
            describe=describe, eligible=eligible, verify=verify_refill_request,
            now=time.time(), observe=observe)
        selection = select_requests(STATE, QUEUE.parent / 'qualified',
                                    QUEUE.parent / 'selection.json', HOLD,
                                    read_cards=cards, describe=describe, eligible=eligible,
                                    verify=verify_refill_request, now=time.time())
        refill = refill_queue(STATE, QUEUE, QUEUE.parent / 'qualified', HOLD,
                              read_cards=cards, eligible=eligible,
                              verify=verify_refill_request, now=time.time())
    paths = sorted(QUEUE.glob('*.json'))
    if not paths:
        return {'state': 'waiting-for-reviewed-requests', 'reconciled': recovered,
                'selection': selection, 'refill': refill, 'lifecycle': lifecycle}
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
            result['fault_attempts'] = record_attempt(job, str(exc))
        if result.get('state') == 'terminal' and result.get('new_launch') and result.get('exit_code') != 0:
            record_attempt(job, 'worker-failed')
        results.append(result)
        if HOLD.exists() or result.get('new_launch') or result.get('reason') in {'estate-capacity', 'actual-start-spacing', 'unresolved-launch', 'authority-busy'}:
            break
    return {'state': 'cycle-finished', 'jobs': results, 'selection': selection,
            'refill': refill, 'lifecycle': lifecycle}


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
        if args.once:
            record_attempt({'card': 'controller'}, str(exc))
        print(json.dumps({'state': 'waiting', 'reason': str(exc)}))
    except Exception as exc:
        if args.once:
            record_attempt({'card': 'controller'}, type(exc).__name__)
        print(json.dumps({'state': 'blocked', 'reason': type(exc).__name__}))
        raise SystemExit(70)
