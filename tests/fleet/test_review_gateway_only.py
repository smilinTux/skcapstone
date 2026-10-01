"""Native review admission follows fresh gateway truth, never worker counts."""

import json
import hashlib
import time

import pytest

from skcapstone.fleet.review_capacity import evaluate_review_capacity, seal_review_capacity_truth
from skcapstone.review_admission import reviewer_capacity_evaluation


def snapshot(*, age=0, active=0, error=None):
    return seal_review_capacity_truth({
        'schema_version': 1, 'observed_at': time.time() - age,
        'cycle_id': 'native-review-regression', 'endpoint': 'https://gateway',
        'error': error, 'routes': [{
            'logical_route': 'review-model', 'capacity_domain': 'gateway-provider',
            'size_class': 'M', 'policy_tier': 'paid-cloud', 'state': 'healthy',
            'max': 2, 'gateway_active': active,
        }],
    }, {'gateway-provider': 99}, occupancy_ambiguous=True, physical_maximum=0)


def evaluate(value):
    return evaluate_review_capacity(value, 'S', [], 'source', 'pi-seraph-review',
                                    declared_seat='seraph', physical_free=0)


def test_gateway_free_requests_ignore_old_worker_limits_and_occupancy():
    legacy = snapshot()
    legacy['physical_maximum'] = 0
    legacy.pop('capacity_revision')
    legacy['capacity_revision'] = hashlib.sha256(json.dumps(
        legacy, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    result = evaluate(legacy)
    assert result['reason'] == 'eligible'
    assert result['available'] == 2
    assert result['occupancy'] == {'gateway-provider': 99}


def test_new_publisher_seal_omits_retired_physical_limits():
    value = snapshot()
    assert 'physical_maximum' not in value
    assert 'physical_free' not in value


@pytest.mark.parametrize('invalid', [[], None, {'observed_at': 'not-a-time'}])
def test_malformed_route_evidence_fails_closed(tmp_path, monkeypatch, invalid):
    monkeypatch.setenv('SKFLEET_EVIDENCE_HOST', 'chiap08')
    directory = tmp_path / 'evidence'
    directory.mkdir()
    (directory / 'fleet-review-routes.chiap08.json').write_text(json.dumps(invalid))
    result = reviewer_capacity_evaluation(tmp_path, {'title': '[S] review'}, [],
                                          'source', 'pi-seraph-review')
    assert result['reason'] == 'route-snapshot-ambiguity'


@pytest.mark.parametrize('age', [121, -1, float('nan')])
def test_stale_future_or_nonfinite_snapshot_cannot_admit(age):
    assert evaluate(snapshot(age=age))['reason'] == 'route-snapshot-stale'


def test_actual_gateway_exhaustion_and_tampered_seal_still_block():
    assert evaluate(snapshot(active=2))['reason'] == 'route-exhaustion'
    value = snapshot()
    value['routes'][0]['max'] = 99
    assert evaluate(value)['reason'] == 'route-snapshot-ambiguity'


def test_fresh_fallback_replaces_stale_preferred_host_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv('SKFLEET_EVIDENCE_HOST', 'chiap08')
    directory = tmp_path / 'evidence'
    directory.mkdir()
    (directory / 'fleet-review-routes.chiap08.json').write_text(json.dumps(snapshot(age=1000)))
    fresh = snapshot()
    (directory / 'fleet-review-routes.json').write_text(json.dumps(fresh))
    result = reviewer_capacity_evaluation(tmp_path, {'title': '[S] review'}, [],
                                          'source', 'pi-seraph-review')
    assert result['capacity_revision'] == fresh['capacity_revision']
    assert result['reason'] == 'eligible'


def test_newer_gateway_failure_is_not_hidden_by_older_healthy_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv('SKFLEET_EVIDENCE_HOST', 'chiap08')
    directory = tmp_path / 'evidence'
    directory.mkdir()
    old = snapshot(age=10)
    (directory / 'fleet-review-routes.json').write_text(json.dumps(old))
    failed = snapshot(error='TimeoutError')
    (directory / 'fleet-review-routes.chiap08.json').write_text(json.dumps(failed))
    result = reviewer_capacity_evaluation(tmp_path, {'title': '[S] review'}, [],
                                          'source', 'pi-seraph-review')
    assert result['capacity_revision'] == failed['capacity_revision']
    assert result['reason'] == 'route-snapshot-ambiguity'
