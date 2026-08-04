"""Tests for retry-on-timeout in service_health probes.

A single timed-out probe is not proof a service is down. A warm-idle service
(e.g. skchat-webui-opus) can cold-start past CHECK_TIMEOUT on the first hit and
answer in milliseconds on the next. Before the fix, that first-hit timeout flapped
the service to "down" and filed a false sev4 ITIL incident on every 5-minute
health sweep. The probe now retries once, and only on the ``timeout`` failure
class - a refused/5xx/unreachable result is a real signal and returns immediately.
"""

from __future__ import annotations

import pytest

from skcapstone import service_health


def _make_flaky(statuses):
    """Return a probe callable that yields the given result dicts in order."""
    calls = {"n": 0}

    def probe():
        i = calls["n"]
        calls["n"] += 1
        return dict(statuses[min(i, len(statuses) - 1)])

    return probe, calls


def test_retry_recovers_service_that_times_out_once():
    """A timeout on attempt 1 followed by 'up' on attempt 2 reports up."""
    probe, calls = _make_flaky(
        [
            {"name": "svc", "status": "down", "error": "<urlopen error timed out>"},
            {"name": "svc", "status": "up", "error": None},
        ]
    )
    result = service_health._retry_on_timeout(probe)
    assert result["status"] == "up"
    assert result["retried"] is True
    assert calls["n"] == 2  # exactly one retry


def test_persistent_timeout_stays_down_and_stops_retrying():
    """A service that times out on every attempt is reported down, bounded retries."""
    probe, calls = _make_flaky(
        [{"name": "svc", "status": "down", "error": "timed out"}]
    )
    result = service_health._retry_on_timeout(probe)
    assert result["status"] == "down"
    assert "retried" not in result
    # 1 initial + RETRY_ON_TIMEOUT retries, no more.
    assert calls["n"] == 1 + service_health.RETRY_ON_TIMEOUT


def test_non_timeout_failure_is_not_retried():
    """Connection refused is a real signal - return immediately, no retry."""
    probe, calls = _make_flaky(
        [{"name": "svc", "status": "down", "error": "[Errno 111] Connection refused"}]
    )
    result = service_health._retry_on_timeout(probe)
    assert result["status"] == "down"
    assert calls["n"] == 1  # no retry for refused


def test_healthy_service_probed_once():
    """An immediately-up service is not probed a second time."""
    probe, calls = _make_flaky([{"name": "svc", "status": "up", "error": None}])
    result = service_health._retry_on_timeout(probe)
    assert result["status"] == "up"
    assert "retried" not in result
    assert calls["n"] == 1


def test_http_check_wrapper_retries_on_timeout(monkeypatch):
    """_http_check delegates to _http_check_once through the retry wrapper."""
    seq = iter(
        [
            {"name": "web", "status": "down", "error": "<urlopen error timed out>"},
            {"name": "web", "status": "up", "latency_ms": 0.9, "error": None},
        ]
    )
    monkeypatch.setattr(
        service_health, "_http_check_once", lambda *a, **k: dict(next(seq))
    )
    result = service_health._http_check("web", "http://localhost:9/health")
    assert result["status"] == "up"
    assert result["retried"] is True


def test_tcp_check_wrapper_retries_on_timeout(monkeypatch):
    """_tcp_check delegates to _tcp_check_once through the retry wrapper."""
    seq = iter(
        [
            {"name": "graph", "status": "down", "error": "timed out"},
            {"name": "graph", "status": "up", "latency_ms": 1.2, "error": None},
        ]
    )
    monkeypatch.setattr(
        service_health, "_tcp_check_once", lambda *a, **k: dict(next(seq))
    )
    result = service_health._tcp_check("graph", "localhost", 6379)
    assert result["status"] == "up"
    assert result["retried"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
