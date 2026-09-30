"""Connect exact terminal producers to Chef review work under one authority.

An explicit, expiring operator scope is mandatory. This pass records a review
request, never accepts the producer's result or launches another orchestrator.
The existing qualified-request inbox owns subsequent provider/host admission.
"""
import base64
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from fiber_admission import Refused, card_fingerprint
from fiber_bundle import read_blob, receive
from fiber_candidate import review_decision, validate_packet
from fiber_dispatch import _same_launch
from fiber_finish import finish_pair
from fiber_refill import SCHEMA, MAX_BYTES, _directory, _publish, _read, digest
from fiber_selection import _policy


def _once(path, value):
    """Persist an immutable intent or recover the identical prior write."""
    if len(json.dumps(value, sort_keys=True).encode()) > MAX_BYTES:
        raise Refused('lifecycle-intent-too-large')
    if path.exists() or path.is_symlink():
        if _read(path) != value:
            raise Refused('lifecycle-intent-conflict')
    else:
        _publish(path, value)


def _evidence(path, data):
    """Publish exact candidate bytes without overwriting earlier evidence."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _directory(path.parent)
    fd, name = tempfile.mkstemp(prefix='.candidate-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            import hashlib
            read_blob(path, hashlib.sha256(data).hexdigest())
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)


def _run(state, card):
    """Read one explicitly scoped ledger row, without creating a database."""
    path = state / 'admission.sqlite3'
    if not path.exists():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
            return None
        row = db.execute('SELECT * FROM runs WHERE card=?', (card,)).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def _producer(run, policy, board):
    """Match the exact authorized contract, owner and successful unit identity."""
    job = json.loads(run['binding'])
    receipt = json.loads(run['receipt'] or '{}')
    row = board.get(run['card']) or {}
    if (not re.fullmatch('[0-9a-f]{32}', str(run.get('token')))
            or not re.fullmatch('[0-9a-f]{32}', str(run.get('invocation')))):
        raise Refused('lifecycle-run-identity')
    if (job.get('card') != run['card'] or job.get('host') != run['host']
            or job.get('stage') != 'implementation' or job.get('provider') not in {'deepseek', 'zai'}
            or job.get('repository') != policy['repository']
            or job.get('card_fingerprint') != policy['cards'].get(run['card'])
            or card_fingerprint(row) != job.get('card_fingerprint')):
        raise Refused('lifecycle-contract-changed')
    if (row.get('owner') != job.get('owner') or row.get('archived')
            or row.get('status') not in {'ready', 'doing', 'review'}
            or (row.get('meta') or {}).get('claim_conflicts')
            or not re.fullmatch('[0-9a-f]{32}', str((row.get('meta') or {}).get('_claim_revision')))):
        raise Refused('lifecycle-claim-changed')
    if any(board.get(cid, {}).get('status') != 'done'
           or (board.get(cid, {}).get('links') or {}).get('verdict') != 'PASS'
           for cid in row.get('dependencies') or []):
        raise Refused('lifecycle-dependency-changed')
    if (not _same_launch(run, receipt) or receipt.get('state') != 'terminal'
            or type(receipt.get('main_pid')) is not int or receipt['main_pid'] != 0
            or type(receipt.get('exit_code')) is not int or receipt['exit_code'] != 0):
        raise Refused('lifecycle-terminal-unverified')
    return job, row


def _terminal_guard(guard, runs, intents, observe):
    """Reobserve both exact units once per locked completion pass, not per link."""
    checked = False
    def check():
        nonlocal checked
        guard()
        if not checked:
            if observe is None:
                raise Refused('lifecycle-finish-observer-unavailable')
            for run, intent in zip(runs, intents):
                expected = {'card': intent['job']['card'], 'host': intent['job']['host'],
                            'token': intent['token'], 'unit': intent['unit'],
                            'invocation': intent['invocation']}
                if (not isinstance(run, dict) or run.get('state') != 'terminal'
                        or not re.fullmatch('[0-9a-f]{32}', str(run.get('invocation')))
                        or any(run.get(key) != value for key, value in expected.items())
                        or json.loads(run['binding']) != intent['job']):
                    raise Refused('lifecycle-finish-unit-changed')
                current = observe(run)
                if (not _same_launch(run, current) or current.get('state') != 'terminal'
                        or type(current.get('main_pid')) is not int or current['main_pid'] != 0
                        or type(current.get('exit_code')) is not int or current['exit_code'] != 0):
                    raise Refused('lifecycle-finish-unit-changed')
            checked = True
    return check


def collect_review(state, directory, request, producer, *, home, read_cards,
                   collect_candidate, snapshot_revision, verify, guard, finish, clock, observe):
    """Retain one terminal independent proposal, with no acceptance mutation.

    A missing test-plane acceptance remains an explicit waiting state. Neither
    this proposal nor its successful model process can supply that evidence.
    """
    card = request['job']['card']
    run = _run(state, card)
    result = {'card': producer['job']['card'], 'review': card, 'state': 'prepared'}
    if run is None or run['state'] != 'terminal':
        return result
    job = json.loads(run['binding'])
    original = {key: value for key, value in job.items() if key not in {'claimable', '_qualification'}}
    if request['job'].get('host') == 'auto':
        original['host'] = 'auto'
    qualification = {key: value for key, value in request.items() if key != 'source_revision'}
    receipt = json.loads(run['receipt'] or '{}')
    if (original != request['job'] or job.get('_qualification') != qualification
            or not re.fullmatch('[0-9a-f]{32}', str(run.get('token')))
            or not re.fullmatch('[0-9a-f]{32}', str(run.get('invocation')))
            or job.get('host') != run['host'] or not _same_launch(run, receipt)
            or receipt.get('state') != 'terminal' or type(receipt.get('main_pid')) is not int
            or receipt['main_pid'] != 0 or type(receipt.get('exit_code')) is not int
            or receipt['exit_code'] != 0):
        raise Refused('lifecycle-review-terminal-binding')
    row = read_cards([card])[card]
    verify(row, job)
    if (row.get('owner') != job['owner'] or row.get('archived')
            or row.get('status') not in {'ready', 'doing', 'review'}
            or (row.get('meta') or {}).get('claim_conflicts')
            or not re.fullmatch('[0-9a-f]{32}', str((row.get('meta') or {}).get('_claim_revision')))):
        raise Refused('lifecycle-review-claim-changed')
    intent_path = directory / 'review-intent.json'
    if intent_path.exists() or intent_path.is_symlink():
        intent = _read(intent_path)
        if (intent.get('job') != job or intent.get('claim_revision') != row['meta']['_claim_revision']
                or intent.get('invocation') != run['invocation']
                or intent.get('review_revision') != snapshot_revision(card)
                or intent.get('source_revision') != request['source_revision']):
            raise Refused('lifecycle-review-intent-changed')
    else:
        revision = snapshot_revision(card)
        proof = collect_candidate(run)
        expected = {'schema': 'skfleet.candidate/v1', 'card': card, 'token': run['token'],
                    'host': run['host'], 'unit': run['unit'], 'invocation': run['invocation'],
                    'owner': job['owner'], 'claim_revision': row['meta']['_claim_revision'],
                    'base_revision': job['base_revision'], 'accepted': False}
        if (any(proof.get(key) != value for key, value in expected.items())
                or proof.get('accepted') is not False
                or not re.fullmatch('[0-9a-f]{64}', str(proof.get('execution_sha256')))):
            raise Refused('lifecycle-review-candidate-binding')
        packet = validate_packet(proof['candidate'], {'job': job, 'receipt': run}, include_bundle=True)
        decision = review_decision(packet, job)
        evidence = home / 'evidence/work' / card / ('fiber-' + run['token'] + '.md')
        proposal = evidence.with_suffix('.json')
        _evidence(evidence, base64.b64decode(packet['evidence_b64'], validate=True))
        _evidence(proposal, base64.b64decode(packet['review_decision_b64'], validate=True))
        guard()
        if (snapshot_revision(card) != revision
                or snapshot_revision(producer['job']['card']) != request['source_revision']):
            raise Refused('lifecycle-review-raced')
        intent = {'job': job, 'claim_revision': proof['claim_revision'],
                  'token': run['token'], 'unit': run['unit'],
                  'invocation': run['invocation'], 'execution_sha256': proof['execution_sha256'],
                  'source_revision': request['source_revision'], 'review_revision': revision,
                  'candidate': {key: value for key, value in packet.items() if not key.endswith('_b64')},
                  'evidence_path': str(evidence), 'proposal_path': str(proposal), 'proposal': decision}
        _once(intent_path, intent)
    read_blob(Path(intent['evidence_path']), intent['candidate']['evidence_sha256'])
    read_blob(Path(intent['proposal_path']), intent['candidate']['review_decision_sha256'])
    guard()
    if snapshot_revision(producer['job']['card']) != request['source_revision']:
        raise Refused('lifecycle-source-superseded')
    if intent['proposal']['verdict'] == 'PASS':
        terminal = _terminal_guard(guard, [_run(state, producer['job']['card']), run],
                                   [producer, intent], observe)
        accepted = finish(directory, producer, intent, home=home, guard=terminal, once=_once, clock=clock)
        if accepted is not None:
            return accepted
    return dict(result, state='awaiting-required-tests' if intent['proposal']['verdict'] == 'PASS'
                else 'review-needs-disposition', proposed_verdict=intent['proposal']['verdict'],
                accepted=False)


def connect_reviews(state, qualified, policy_path, hold, *, home, data_root,
                    read_cards, collect_candidate, snapshot_revision, record_verdict,
                    open_review, describe, eligible, verify, now, clock=time.time,
                    finish=finish_pair, observe=None):
    """Prepare at most one exact review request per call; caller holds authority.

    Runtime adapters use mediated coord commands. Injected adapters let tests
    exercise lost acknowledgements against a fully isolated native board.
    Finalization requires exact independent review and qualified test evidence.
    No hosted CI, merge, deployment or extra worker privilege is asserted.
    """
    state, qualified, policy_path, hold, home, data_root = map(
        Path, (state, qualified, policy_path, hold, home, data_root))
    result = {'state': 'lifecycle-checked', 'reviews': [], 'waiting': []}
    if hold.exists() or hold.is_symlink():
        return dict(result, state='admission-held')
    if not policy_path.exists() and not policy_path.is_symlink():
        return dict(result, state='disabled')
    policy = _policy(policy_path, now)
    def guard():
        if hold.exists() or hold.is_symlink():
            raise Refused('admission-held')
        if _policy(policy_path, clock()) != policy:
            raise Refused('lifecycle-policy-changed')
    _directory(state)
    _directory(qualified)
    for card in policy['cards']:
        run = _run(state, card)
        if run is None or run['state'] != 'terminal':
            result['waiting'].append({'card': card, 'reason': 'producer-not-terminal'})
            continue
        if not re.fullmatch('[0-9a-f]{32}', str(run.get('token'))):
            raise Refused('lifecycle-run-identity')
        directory = state / 'lifecycle' / run['token']
        finish_path = directory / 'finish-intent.json'
        if finish_path.exists() or finish_path.is_symlink():
            _directory(directory.parent)
            _directory(directory)
            intent = _read(directory / 'candidate-intent.json')
            if (intent['policy_sha256'] != digest(policy) or intent['job'] != json.loads(run['binding'])
                    or intent['invocation'] != run['invocation']):
                raise Refused('lifecycle-finish-run-changed')
            review = _read(directory / 'review-intent.json')
            terminal = _terminal_guard(guard, [run, _run(state, review['job']['card'])],
                                       [intent, review], observe)
            result['reviews'].append(finish(directory, intent, review,
                home=home, guard=terminal, once=_once, clock=clock))
            continue
        board = read_cards([card])
        job, row = _producer(run, policy, board)
        verify(row, job)
        directory.parent.mkdir(exist_ok=True, mode=0o700)
        _directory(directory.parent)
        directory.mkdir(exist_ok=True, mode=0o700)
        _directory(directory)
        intent_path = directory / 'candidate-intent.json'
        recovered_intent = intent_path.exists() or intent_path.is_symlink()
        if recovered_intent:
            intent = _read(intent_path)
            if (intent.get('job') != job or intent.get('claim_revision') != row['meta']['_claim_revision']
                    or intent.get('invocation') != run['invocation']
                    or intent.get('policy_sha256') != digest(policy)):
                raise Refused('lifecycle-intent-changed')
        else:
            proof = collect_candidate(run)
            expected = {'schema': 'skfleet.candidate/v1', 'card': card, 'token': run['token'],
                        'host': run['host'], 'unit': run['unit'], 'invocation': run['invocation'],
                        'owner': job['owner'], 'claim_revision': row['meta']['_claim_revision'],
                        'base_revision': job['base_revision'], 'accepted': False}
            if (any(proof.get(key) != value for key, value in expected.items())
                    or proof.get('accepted') is not False
                    or not re.fullmatch('[0-9a-f]{64}', str(proof.get('execution_sha256')))):
                raise Refused('lifecycle-candidate-binding')
            packet = validate_packet(proof['candidate'], {'job': job, 'receipt': run}, include_bundle=True)
            evidence = home / 'evidence/work' / card / ('fiber-' + run['token'] + '.md')
            _evidence(evidence, base64.b64decode(packet['evidence_b64'], validate=True))
            receive({'sha256': packet['bundle_sha256'], 'bundle_b64': packet['bundle_b64']}, data_root)
            revision = snapshot_revision(card)
            fresh_job, fresh_row = _producer(run, policy, read_cards([card]))
            verify(fresh_row, fresh_job)
            if fresh_job != job or snapshot_revision(card) != revision:
                raise Refused('lifecycle-source-raced')
            intent = {'job': job, 'claim_revision': proof['claim_revision'],
                      'token': run['token'], 'unit': run['unit'],
                      'invocation': run['invocation'], 'execution_sha256': proof['execution_sha256'],
                      'policy_sha256': digest(policy), 'source_revision': revision,
                      'candidate': {key: value for key, value in packet.items() if not key.endswith('_b64')},
                      'evidence_path': str(evidence)}
            intent['transition_id'] = digest(intent)
            _once(intent_path, intent)  # Intent survives a lost native write acknowledgement.
        read_blob(Path(intent['evidence_path']), intent['candidate']['evidence_sha256'])
        completed = directory / 'review-request.json'
        if completed.exists() or completed.is_symlink():
            request = _read(completed)
            if snapshot_revision(card) != request['source_revision']:
                raise Refused('lifecycle-source-superseded')
            guard()
            _once(qualified / (request['job']['card'] + '.json'),
                  {key: value for key, value in request.items() if key != 'source_revision'})
            result['reviews'].append(collect_review(state, directory, request, intent,
                home=home, read_cards=read_cards, collect_candidate=collect_candidate,
                snapshot_revision=snapshot_revision, verify=verify, guard=guard,
                finish=finish, clock=clock, observe=observe))
            continue
        if recovered_intent:
            # A lost acknowledgement is not permission to reuse a now-running,
            # replaced or edited terminal workspace for another board write.
            proof = collect_candidate(run)
            expected = {'schema': 'skfleet.candidate/v1', 'card': card, 'token': run['token'],
                        'host': run['host'], 'unit': run['unit'], 'invocation': run['invocation'],
                        'owner': job['owner'], 'claim_revision': intent['claim_revision'],
                        'base_revision': job['base_revision'], 'execution_sha256': intent['execution_sha256']}
            packet = validate_packet(proof['candidate'], {'job': job, 'receipt': run}, include_bundle=True)
            if (any(proof.get(key) != value for key, value in expected.items())
                    or proof.get('accepted') is not False
                    or {key: value for key, value in packet.items() if not key.endswith('_b64')} != intent['candidate']):
                raise Refused('lifecycle-recovered-candidate-changed')
        guard()
        record_verdict(intent)  # A guarded request for review, not acceptance of the work.
        verdict_path = directory / 'provisional-recorded.json'
        revision = snapshot_revision(card)
        _once(verdict_path, {'source_revision': revision, 'transition_id': intent['transition_id']})
        guard()
        opened = open_review(intent, revision)
        review = opened.get('review_card_id')
        if (opened.get('source_card') != card or opened.get('head_revision') != intent['candidate']['commit']
                or opened.get('launchable') is not True or not re.fullmatch('[0-9a-f]{8}', str(review))):
            raise Refused('lifecycle-review-binding')
        family = 'zai' if job['provider'] == 'deepseek' else 'deepseek'
        request = describe(review, 'review', family, producer_family=job['provider'])
        if (request.get('card') != review or request.get('stage') != 'review'
                or request.get('provider') != family or request.get('producer_provider') != job['provider']
                or request.get('repository') != job['repository']
                or request.get('base_revision') != intent['candidate']['commit']):
            raise Refused('lifecycle-review-request-changed')
        request['source_bundle'] = {'sha256': intent['candidate']['bundle_sha256'],
                                   'base_revision': job['base_revision'],
                                   'tree': intent['candidate']['tree'], 'ref': intent['candidate']['ref']}
        if 'test_environment' in job:
            request['test_environment'] = job['test_environment']
        current = read_cards([review])
        review_row = current[review]
        verify(review_row, request)
        if not eligible(review_row, current) or snapshot_revision(card) != revision:
            raise Refused('lifecycle-review-not-current')
        receipt = {'schema': SCHEMA, 'job': request, 'job_sha256': digest(request),
                   'card_links_sha256': digest(review_row.get('links') or {}),
                   'qualified_at': policy['qualified_at'], 'expires_at': policy['expires_at'],
                   'evidence_sha256': digest(intent)}
        guard()
        _once(qualified / (review + '.json'), receipt)
        _once(completed, {'source_revision': revision, **receipt})
        result['reviews'].append({'card': card, 'review': review, 'state': 'prepared'})
        break
    return result
