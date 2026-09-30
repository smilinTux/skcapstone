"""Finish an exact source/review pair through the existing mediated CLI.

The authority lock is held by the caller. Immutable per-step intents recover
lost replies, without adopting a newer claim or emitting a second receipt.
"""
import json
from pathlib import Path
import subprocess
import time

from fiber_admission import Refused
from fiber_bundle import read_blob
from fiber_refill import _read, digest
from fiber_test_acceptance import verified_tests


def native_state(home, card):
    """Read native identity and predict only the existing completion mutation."""
    from skcapstone.card_store import CardStore, card_mutation_lock
    from skcapstone.seraph_review_cardstore import LiveCardStoreGateway, card_revision
    from skcapstone.seraph_review_contracts import _digest
    from skcapstone.blocked_verdict import is_outcome_key
    with card_mutation_lock(home, card):
        store = CardStore(home)
        row = store.fold(card)
        snapshot = LiveCardStoreGateway(home).read_card(card)
        after = row.model_copy(deep=True)
        after.status = type(row.status)('done')
        after.owner = None
        after.meta.pop('_claim_revision', None)
        bindings = {key: getattr(snapshot, key) for key in (
            'repository', 'number', 'head_sha', 'producer_identity', 'reviewer_identity',
            'unresolved_review', 'candidate_evidence_sha256')}
        native = store._read_events(card)
        events = native + store._legacy_events(card)
        events.sort(key=lambda e: (e.get('ts', ''), e.get('writer', ''), e.get('seq', 0)))
        outcomes = [e for e in events if e.get('action') == 'verdict' or (
            e.get('action') == 'link' and is_outcome_key(e.get('link_key') or e.get('key')))]
        # Match LiveCardStoreGateway's stable last-event rule, including tied
        # native/overlay copies; max() would select the first tied event.
        predicted = _digest({'card': card_revision(after), 'bindings': bindings,
                             'outcome': outcomes[-1] if outcomes else {}})
        completions = [event for event in native if event['action'] == 'complete']
        return {'revision': snapshot.revision, 'completion_revision': predicted,
                'complete_count': len(completions), 'last_complete': completions[-1] if completions else None,
                'owner': row.owner, 'claim_revision': row.meta.get('_claim_revision'),
                'status': row.status.value}


def native_command(home, args):
    """Write exclusively through coord; never fall back to raw events."""
    result = subprocess.run(['skcapstone', 'coord', *args, '--home', str(home)],
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise Refused('lifecycle-native-finish-refused')
    return json.loads(result.stdout) if '--json' in args else None


def _artifacts(producer, review):
    """Rehash retained reports and the exact collected reviewer proposal."""
    read_blob(Path(producer['evidence_path']), producer['candidate']['evidence_sha256'])
    read_blob(Path(review['evidence_path']), review['candidate']['evidence_sha256'])
    raw = read_blob(Path(review['proposal_path']), review['candidate']['review_decision_sha256'])
    if json.loads(raw) != review['proposal'] or review['proposal']['verdict'] != 'PASS':
        raise Refused('lifecycle-finish-review-not-pass')


def _steps(producer, review, acceptance):
    """Keep applicability last, with no fabricated hosted CI or source edits."""
    source, reviewer = producer['job'], review['job']
    head = producer['candidate']['commit']
    report_hash = review['candidate']['evidence_sha256']
    report = review['evidence_path']
    audit = json.dumps(acceptance, sort_keys=True, separators=(',', ':'))
    receipt = json.dumps({'type': 'source-only-applicability', 'card_id': reviewer['card'],
                         'source_head': head, 'reviewer': reviewer['owner'],
                         'evidence_digest': report_hash, 'governed_pr_ci': False}, sort_keys=True)
    repo = source['repository'].rstrip('/').removesuffix('.git').rsplit('/', 1)[-1]
    values = [('test_acceptance', audit), ('verdict', 'PASS'), ('evidence', report),
              ('reviewer_evidence_sha256', report_hash),
              ('review_evidence', report + '|sha256=' + report_hash),
              ('applicability_receipt', receipt)]
    steps = [('review', 'link', key, value) for key, value in values]
    steps.append(('review', 'complete', '', ''))
    values = [('review_join', reviewer['card']), ('test_acceptance', audit), ('commit_sha', head),
              ('branch', repo + ':' + producer['candidate']['ref'].removeprefix('refs/heads/')),
              ('head', head), ('candidate_evidence_sha256', producer['candidate']['evidence_sha256']),
              ('verdict', 'PASS'), ('evidence', producer['evidence_path']),
              ('evidence_sha256', producer['candidate']['evidence_sha256'])]
    steps.extend(('source', 'link', key, value) for key, value in values)
    steps.append(('source', 'complete', '', ''))
    return steps


def finish_pair(directory, producer, review, *, home, guard, once, clock=time.time,
                command=native_command, inspect=native_state):
    """Accept only current independent PASS plus operator-qualified real tests.

    All writes have exact native revision and original claim guards. Finished
    receipts are historical proof, not authority to repeat writes after expiry.
    """
    directory = Path(directory)
    complete_path, intent_path = directory / 'finished.json', directory / 'finish-intent.json'
    _artifacts(producer, review)
    completed = _read(complete_path) if complete_path.exists() or complete_path.is_symlink() else None
    final_ack_path = directory / 'finish-16.ack.json'
    sealed = _read(final_ack_path) if final_ack_path.exists() or final_ack_path.is_symlink() else None
    historical = completed or sealed
    acceptance = verified_tests(directory, producer, now=historical['validated_at'] if historical else clock())
    if acceptance is None:
        if intent_path.exists() or intent_path.is_symlink():
            raise Refused('lifecycle-finish-test-evidence-lost')
        return None
    parties = {'source': producer, 'review': review}
    initial = {'source': review['source_revision'], 'review': review['review_revision']}
    binding = {'producer_sha256': digest(producer), 'review_sha256': digest(review),
               'acceptance': acceptance, 'initial': initial}
    if not (intent_path.exists() or intent_path.is_symlink()):
        if historical:
            raise Refused('lifecycle-finish-receipt-incomplete')
        guard()
        if any(inspect(home, item['job']['card'])['revision'] != initial[name]
               for name, item in parties.items()):
            raise Refused('lifecycle-finish-generation-changed')
    once(intent_path, binding)
    expected = initial
    validated_at = None
    for index, (role, action, key, value) in enumerate(_steps(producer, review, acceptance)):
        item, other = parties[role], 'review' if role == 'source' else 'source'
        card, owner = item['job']['card'], item['job']['owner']
        request = {'binding': digest(binding), 'index': index, 'role': role, 'action': action,
                   'key': key, 'value': value, 'before': expected,
                   'card': card, 'owner': owner, 'claim_revision': item['claim_revision']}
        stem = directory / ('finish-%02d' % index)
        step_path, ack_path = stem.with_suffix('.intent.json'), stem.with_suffix('.ack.json')
        if step_path.exists() or step_path.is_symlink():
            step = _read(step_path)
            if any(step.get(name) != val for name, val in request.items()):
                raise Refused('lifecycle-finish-step-changed')
        else:
            if historical:
                raise Refused('lifecycle-finish-receipt-incomplete')
            guard()
            before = inspect(home, card)
            if (before['revision'] != expected[role] or before['owner'] != owner
                    or before['claim_revision'] != item['claim_revision']):
                raise Refused('lifecycle-finish-generation-changed')
            step = dict(request, native_before=before)
            once(step_path, step)
        if ack_path.exists() or ack_path.is_symlink():
            ack = _read(ack_path)
            if (set(ack) != {'step_sha256', 'after', 'validated_at'} or ack['step_sha256'] != digest(step)
                    or set(ack['after']) != set(expected)
                    or ack['after'][other] != expected[other]):
                raise Refused('lifecycle-finish-ack-changed')
            expected = ack['after']
            validated_at = ack['validated_at']
            continue
        if historical:
            raise Refused('lifecycle-finish-receipt-incomplete')
        guard()
        _artifacts(producer, review)
        validated_at = clock()
        if verified_tests(directory, producer, now=validated_at) != acceptance:
            raise Refused('lifecycle-finish-tests-changed')
        if inspect(home, parties[other]['job']['card'])['revision'] != expected[other]:
            raise Refused('lifecycle-finish-related-card-changed')
        args = [action, card]
        if action == 'link':
            args += [key, value, '--transition-id', digest(step), '--json']
        args += ['--agent', owner, '--expected-source-revision', expected[role],
                 '--expected-claim-revision', item['claim_revision']]
        if action == 'complete' and role == 'source':
            args += ['--review-card', review['job']['card'], '--expected-review-revision', expected['review']]
        before = step['native_before']
        if action == 'link':
            reply = command(home, args)
            after = inspect(home, card)
            if reply.get('card_id') != card or reply.get('source_revision') != after['revision']:
                raise Refused('lifecycle-finish-link-readback')
        else:
            after = inspect(home, card)
            if after['revision'] == expected[role]:
                command(home, args)
                after = inspect(home, card)
            last = after['last_complete'] or {}
            if (after['revision'] != before['completion_revision'] or after['status'] != 'done'
                    or after['owner'] is not None or last.get('writer') != owner
                    or after['complete_count'] != before['complete_count'] + 1):
                raise Refused('lifecycle-finish-completion-readback')
        if inspect(home, parties[other]['job']['card'])['revision'] != expected[other]:
            raise Refused('lifecycle-finish-related-card-changed')
        expected = dict(expected, **{role: after['revision']})
        once(ack_path, {'step_sha256': digest(step), 'after': expected, 'validated_at': validated_at})
    if any(inspect(home, item['job']['card'])['revision'] != expected[name]
           for name, item in parties.items()):
        raise Refused('lifecycle-finish-completed-pair-changed')
    result = {'card': producer['job']['card'], 'review': review['job']['card'],
              'state': 'source-complete', 'accepted': True, 'governed_pr_ci': False}
    receipt = {'binding_sha256': digest(binding), 'revisions': expected, 'result': result,
               'validated_at': validated_at}
    if completed:
        if {key: val for key, val in completed.items() if key != 'finished_at'} != receipt:
            raise Refused('lifecycle-finish-receipt-changed')
    else:
        once(complete_path, dict(receipt, finished_at=clock()))
    return result
