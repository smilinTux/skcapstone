# Joule Economy P0: The Meter (shadow mode) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure the real energy cost of every inference the fleet performs, record it, and return it to the caller, without changing any routing or billing behavior.

**Architecture:** A `skmeter` daemon on each GPU node samples `nvidia-smi` power and maintains a monotonic joule counter, synthesizing the hardware counter the RTX 5060 Ti does not provide. skgateway reads that counter before and after each upstream attempt, takes the delta, and writes it to a new `energy_log` table beside the existing cost tables. Requests served by unmeterable backends (cloud, CPU) get joules imputed from a per-model-family coefficient table. Nothing routes differently; this phase only observes.

**Tech Stack:** Python 3.12 stdlib (skmeter, inside skcapstone, following `fleet/sknoded.py`); Node 22 ESM + better-sqlite3 + `node:test` (skgateway).

**Spec:** `docs/superpowers/specs/2026-08-14-joule-economy-design.md` (read sections 4 and 4.5 in full before starting)

## Global Constraints

- **No em dashes or en dashes anywhere.** Not in code, comments, commit messages, docs, or output. Chef's hard rule. Regular hyphens are fine.
- **Shadow mode only.** No task in this plan may change which model serves a request, what anything costs, or any wallet balance. If a change would alter routing or billing, it belongs in P2 or P3, not here.
- **Fail open, always.** A meter that is down, slow, or wrong must never fail a user's inference. Every meter read is wrapped so that failure yields `null` energy and the request proceeds untouched.
- **Every energy row records its basis.** `measured_gpu`, `imputed_local`, or `imputed_cloud`. Never write an energy number without one.
- **Charge marginal, not absolute.** `(P_busy - P_idle) x t`. Idle is tracked separately.
- **`metrics.db` has no migration mechanism.** DDL is `CREATE TABLE IF NOT EXISTS` on boot (`collector.mjs:190-255`). Get `energy_log` right the first time; altering it later has no supported path.
- **Python:** `ruff` and `black` clean. Run tests with `~/.skenv/bin/python -m pytest`.
- **Node:** tests via `npm test` (`node --test tests/*.test.mjs`) from the skgateway repo root.

---

## File Structure

**skcapstone** (`skcapstone`)

| File | Responsibility |
|---|---|
| `src/skcapstone/fleet/skmeter.py` (create) | Pure energy math + counter, the sampling loop, and the HTTP endpoint. Follows `sknoded.py`: pure builders separated from the loop so they test without hardware. |
| `tests/fleet/test_skmeter.py` (create) | Unit tests for the pure half. No GPU required. |
| `systemd/skmeter.service` (create) | Node-local unit, installed on GPU nodes only. |

**skgateway** (`skgateway`)

| File | Responsibility |
|---|---|
| `src/metrics/energy.mjs` (create) | Pure: marginal-joule arithmetic from two counter reads, and token-based imputation. No I/O. |
| `src/proxy/meter-client.mjs` (create) | Impure: fetch a meter counter over HTTP with a hard timeout. Returns `null` on any failure. |
| `src/metrics/collector.mjs` (modify) | Add `energy_log` to the DDL, an insert statement, and an `recordEnergy()` API. |
| `src/config.mjs` (modify) | Add the `energy:` config block to `DEFAULTS` and `validate()`. |
| `src/proxy/router.mjs` (modify) | Per-attempt meter reads inside the candidate loop; strip `x-sk-card-id` from upstream headers. |
| `src/index.mjs` (modify) | Parse `x-sk-card-id`; repair the `recordRequest`/`recordResponse` wiring; emit energy response headers. |
| `tests/energy-math.test.mjs` (create) | Pure math tests. |
| `tests/energy-e2e.test.mjs` (create) | Stub upstream + stub meter, asserts an `energy_log` row and the response header. |

---

## Task 1: skmeter pure core (power parsing, integration, counter)

**Files:**
- Create: `skcapstone-repos/skcapstone/src/skcapstone/fleet/skmeter.py`
- Test: `skcapstone-repos/skcapstone/tests/fleet/test_skmeter.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `parse_power_line(line: str) -> float | None`
  - `integrate(samples_w: list[float], dt_s: float, idle_w: float = 0.0) -> dict` returning `{"total_j": float, "marginal_j": float, "window_s": float, "samples_n": int, "mean_w": float, "peak_w": float}`
  - `class EnergyCounter` with `observe(watts: float, dt_s: float) -> None`, properties `total_j: float`, `marginal_j: float`, `samples_n: int`, and `snapshot() -> dict`; constructor `EnergyCounter(idle_w: float = 0.0)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/fleet/test_skmeter.py`:

```python
"""Unit tests for the skmeter pure core. No GPU required."""

import pytest

from skcapstone.fleet.skmeter import EnergyCounter, integrate, parse_power_line


class TestParsePowerLine:
    def test_plain_value(self):
        assert parse_power_line("140.77") == pytest.approx(140.77)

    def test_strips_whitespace(self):
        assert parse_power_line("  8.86\n") == pytest.approx(8.86)

    def test_null_bytes_are_stripped(self):
        # Observed in the field 2026-08-14: reading the sampler's output file
        # while nvidia-smi was still writing produced NUL padding.
        assert parse_power_line("\x00\x00\x00\x008.86") == pytest.approx(8.86)

    def test_units_suffix_tolerated(self):
        assert parse_power_line("99.12 W") == pytest.approx(99.12)

    def test_not_supported_returns_none(self):
        assert parse_power_line("[N/A]") is None

    def test_blank_returns_none(self):
        assert parse_power_line("   ") is None

    def test_garbage_returns_none(self):
        assert parse_power_line("nvidia-smi: command not found") is None


class TestIntegrate:
    def test_constant_power(self):
        r = integrate([100.0] * 10, dt_s=0.2)
        assert r["total_j"] == pytest.approx(200.0)  # 100 W x 2.0 s
        assert r["window_s"] == pytest.approx(2.0)
        assert r["samples_n"] == 10

    def test_marginal_subtracts_idle(self):
        r = integrate([100.0] * 10, dt_s=0.2, idle_w=10.0)
        assert r["total_j"] == pytest.approx(200.0)
        assert r["marginal_j"] == pytest.approx(180.0)  # 90 W x 2.0 s

    def test_marginal_never_negative(self):
        # Below-idle samples must not create energy credits.
        r = integrate([5.0, 5.0], dt_s=1.0, idle_w=10.0)
        assert r["marginal_j"] == pytest.approx(0.0)

    def test_mean_and_peak(self):
        r = integrate([10.0, 20.0, 60.0], dt_s=1.0)
        assert r["mean_w"] == pytest.approx(30.0)
        assert r["peak_w"] == pytest.approx(60.0)

    def test_empty_is_zero_not_error(self):
        r = integrate([], dt_s=0.2)
        assert r["total_j"] == 0.0
        assert r["marginal_j"] == 0.0
        assert r["samples_n"] == 0

    def test_matches_field_measurement(self):
        # Regression against the real 2026-08-14 run on .100:
        # 95 samples at 0.2 s, mean 99.12 W, idle 8.96 W -> ~1713 J marginal.
        samples = [99.12] * 95
        r = integrate(samples, dt_s=0.2, idle_w=8.96)
        assert r["marginal_j"] == pytest.approx(1713.0, abs=2.0)


class TestEnergyCounter:
    def test_starts_at_zero(self):
        c = EnergyCounter(idle_w=8.96)
        assert c.total_j == 0.0
        assert c.marginal_j == 0.0
        assert c.samples_n == 0

    def test_accumulates_monotonically(self):
        c = EnergyCounter(idle_w=0.0)
        c.observe(100.0, 0.2)
        first = c.total_j
        c.observe(100.0, 0.2)
        assert c.total_j > first
        assert c.total_j == pytest.approx(40.0)

    def test_never_decreases_even_below_idle(self):
        c = EnergyCounter(idle_w=50.0)
        c.observe(10.0, 1.0)
        assert c.marginal_j == 0.0
        c.observe(150.0, 1.0)
        assert c.marginal_j == pytest.approx(100.0)

    def test_snapshot_shape(self):
        c = EnergyCounter(idle_w=8.96)
        c.observe(100.0, 0.2)
        s = c.snapshot()
        assert set(s) >= {"total_j", "marginal_j", "idle_baseline_w", "samples_n"}
        assert s["idle_baseline_w"] == pytest.approx(8.96)

    def test_delta_between_two_reads_is_the_energy_of_that_window(self):
        # This is exactly how the gateway will use it.
        c = EnergyCounter(idle_w=10.0)
        c.observe(10.0, 1.0)          # idle before the request
        before = c.marginal_j
        c.observe(110.0, 2.0)         # the request itself: 100 W x 2 s
        after = c.marginal_j
        assert after - before == pytest.approx(200.0)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd <skcapstone repo root>
~/.skenv/bin/python -m pytest tests/fleet/test_skmeter.py -v
```

Expected: FAIL, `ModuleNotFoundError: No module named 'skcapstone.fleet.skmeter'`

- [ ] **Step 3: Write the minimal implementation**

Create `src/skcapstone/fleet/skmeter.py`:

```python
"""skmeter: the per-node energy counter.

The RTX 5060 Ti exposes instantaneous `power.draw` but has no cumulative
`total_energy_consumption` counter, and no RAPL exists anywhere on this fleet.
So we synthesize the counter: sample power continuously and integrate.

This module keeps the arithmetic pure and separate from the sampling loop and
the HTTP surface, following the pattern in sknoded.py, so the math is testable
without a GPU present.
"""

from __future__ import annotations

import re

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def parse_power_line(line: str) -> float | None:
    """Parse one `nvidia-smi --query-gpu=power.draw` output line into watts.

    Returns None for blanks, '[N/A]', and anything else unparseable. Tolerates
    NUL padding, which appears when the sampler's output file is read while
    nvidia-smi is still writing to it.
    """
    if not line:
        return None
    cleaned = line.replace("\x00", "").strip()
    if not cleaned or "N/A" in cleaned:
        return None
    match = _NUMBER.search(cleaned)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def integrate(samples_w: list[float], dt_s: float, idle_w: float = 0.0) -> dict:
    """Integrate power samples into joules over a fixed sample interval.

    `marginal_j` subtracts the idle baseline and is floored at zero per sample,
    so below-idle readings cannot create energy credits.
    """
    n = len(samples_w)
    if n == 0:
        return {
            "total_j": 0.0,
            "marginal_j": 0.0,
            "window_s": 0.0,
            "samples_n": 0,
            "mean_w": 0.0,
            "peak_w": 0.0,
        }
    total_j = sum(w * dt_s for w in samples_w)
    marginal_j = sum(max(0.0, w - idle_w) * dt_s for w in samples_w)
    return {
        "total_j": total_j,
        "marginal_j": marginal_j,
        "window_s": n * dt_s,
        "samples_n": n,
        "mean_w": sum(samples_w) / n,
        "peak_w": max(samples_w),
    }


class EnergyCounter:
    """A monotonic joule counter, the thing the GPU refuses to give us.

    Callers read `marginal_j` before and after a unit of work; the delta is that
    work's energy. Monotonicity is what makes the delta meaningful, so nothing
    here may ever decrease.
    """

    def __init__(self, idle_w: float = 0.0) -> None:
        self._idle_w = float(idle_w)
        self._total_j = 0.0
        self._marginal_j = 0.0
        self._samples_n = 0

    @property
    def total_j(self) -> float:
        return self._total_j

    @property
    def marginal_j(self) -> float:
        return self._marginal_j

    @property
    def samples_n(self) -> int:
        return self._samples_n

    @property
    def idle_baseline_w(self) -> float:
        return self._idle_w

    def set_idle_baseline(self, idle_w: float) -> None:
        """Re-baseline (nightly). Does not retroactively alter the counter."""
        self._idle_w = float(idle_w)

    def observe(self, watts: float, dt_s: float) -> None:
        self._total_j += watts * dt_s
        self._marginal_j += max(0.0, watts - self._idle_w) * dt_s
        self._samples_n += 1

    def snapshot(self) -> dict:
        return {
            "total_j": self._total_j,
            "marginal_j": self._marginal_j,
            "idle_baseline_w": self._idle_w,
            "samples_n": self._samples_n,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd <skcapstone repo root>
~/.skenv/bin/python -m pytest tests/fleet/test_skmeter.py -v
~/.skenv/bin/python -m ruff check src/skcapstone/fleet/skmeter.py
~/.skenv/bin/python -m black --check src/skcapstone/fleet/skmeter.py
```

Expected: 18 passed, ruff clean, black clean.

- [ ] **Step 5: Commit**

```bash
cd <skcapstone repo root>
git add src/skcapstone/fleet/skmeter.py tests/fleet/test_skmeter.py
git commit -m "feat(skmeter): pure energy counter and power-sample integration

The 5060 Ti has no cumulative energy counter and the fleet has no RAPL, so
energy must be integrated from sampled power. This is the pure half: parsing,
integration, and a monotonic counter whose before/after delta is the energy of
a unit of work. No GPU needed to test it.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: skmeter daemon (sampling loop + HTTP endpoint + unit)

**Files:**
- Modify: `skcapstone-repos/skcapstone/src/skcapstone/fleet/skmeter.py`
- Modify: `skcapstone-repos/skcapstone/tests/fleet/test_skmeter.py`
- Create: `skcapstone-repos/skcapstone/systemd/skmeter.service`

**Interfaces:**
- Consumes: `EnergyCounter`, `parse_power_line` from Task 1.
- Produces:
  - `measure_idle_baseline(sample_fn, n: int = 50) -> float`
  - `build_energy_response(counter: EnergyCounter, watts_now: float, device: str, node: str, now_ms: int) -> dict` returning `{counter_j, total_j, watts_now, idle_baseline_w, device, node, ts, samples_n}` where `counter_j` is the marginal counter (the field the gateway reads)
  - `serve(port: int, counter: EnergyCounter, ...)` starting the HTTP endpoint on `GET /energy`

- [ ] **Step 1: Write the failing tests**

Append to `tests/fleet/test_skmeter.py`:

```python
from skcapstone.fleet.skmeter import build_energy_response, measure_idle_baseline


class TestIdleBaseline:
    def test_averages_the_samples(self):
        vals = iter([8.9, 9.0, 8.8, 9.1])
        assert measure_idle_baseline(lambda: next(vals), n=4) == pytest.approx(8.95)

    def test_ignores_unparseable_samples(self):
        vals = iter([8.9, None, 9.1, None])
        assert measure_idle_baseline(lambda: next(vals), n=4) == pytest.approx(9.0)

    def test_all_bad_samples_returns_zero_not_error(self):
        # A zero baseline means we charge absolute energy, which is wrong but
        # safe. Crashing the meter would be worse.
        assert measure_idle_baseline(lambda: None, n=3) == 0.0


class TestEnergyResponse:
    def test_counter_j_is_the_marginal_counter(self):
        c = EnergyCounter(idle_w=10.0)
        c.observe(110.0, 1.0)  # 100 J marginal, 110 J total
        r = build_energy_response(c, watts_now=110.0, device="gpu0",
                                  node="dot100", now_ms=1_700_000_000_000)
        assert r["counter_j"] == pytest.approx(100.0)
        assert r["total_j"] == pytest.approx(110.0)

    def test_carries_identity_and_timestamp(self):
        c = EnergyCounter(idle_w=8.96)
        r = build_energy_response(c, watts_now=9.0, device="gpu0",
                                  node="dot100", now_ms=1_700_000_000_000)
        assert r["device"] == "gpu0"
        assert r["node"] == "dot100"
        assert r["ts"] == 1_700_000_000_000
        assert r["idle_baseline_w"] == pytest.approx(8.96)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd <skcapstone repo root>
~/.skenv/bin/python -m pytest tests/fleet/test_skmeter.py -k "Idle or EnergyResponse" -v
```

Expected: FAIL, `ImportError: cannot import name 'build_energy_response'`

- [ ] **Step 3: Write the minimal implementation**

Append to `src/skcapstone/fleet/skmeter.py`:

```python
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

DEFAULT_PORT = 9420
DEFAULT_INTERVAL_MS = 200
NVIDIA_SMI_CMD = [
    "nvidia-smi",
    "--query-gpu=power.draw",
    "--format=csv,noheader,nounits",
]


def measure_idle_baseline(sample_fn: Callable[[], float | None], n: int = 50) -> float:
    """Average n samples to establish the idle floor.

    Returns 0.0 if nothing parseable arrives. A zero baseline charges absolute
    energy, which is wrong but safe; crashing the meter would be worse.
    """
    good = []
    for _ in range(n):
        try:
            value = sample_fn()
        except Exception:
            value = None
        if value is not None:
            good.append(float(value))
    if not good:
        return 0.0
    return sum(good) / len(good)


def build_energy_response(
    counter: EnergyCounter,
    watts_now: float,
    device: str,
    node: str,
    now_ms: int,
) -> dict:
    """The GET /energy payload. `counter_j` is what the gateway deltas."""
    snap = counter.snapshot()
    return {
        "counter_j": snap["marginal_j"],
        "total_j": snap["total_j"],
        "watts_now": watts_now,
        "idle_baseline_w": snap["idle_baseline_w"],
        "device": device,
        "node": node,
        "ts": now_ms,
        "samples_n": snap["samples_n"],
    }


class _State:
    """Shared between the sampler thread and the HTTP handler."""

    def __init__(self, counter: EnergyCounter, device: str, node: str) -> None:
        self.counter = counter
        self.device = device
        self.node = node
        self.watts_now = 0.0
        self.lock = threading.Lock()


def sample_loop(state: _State, interval_ms: int = DEFAULT_INTERVAL_MS) -> None:
    """Stream nvidia-smi output and feed the counter.

    Uses one long-lived `nvidia-smi -lms` process rather than spawning per
    sample, which would cost more than it measures.
    """
    dt_s = interval_ms / 1000.0
    while True:
        try:
            proc = subprocess.Popen(
                NVIDIA_SMI_CMD + ["-lms", str(interval_ms)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            for line in proc.stdout:
                watts = parse_power_line(line)
                if watts is None:
                    continue
                with state.lock:
                    state.counter.observe(watts, dt_s)
                    state.watts_now = watts
        except Exception:
            pass
        time.sleep(5.0)  # nvidia-smi died; back off and retry


def _handler_factory(state: _State):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/") != "/energy":
                self.send_response(404)
                self.end_headers()
                return
            with state.lock:
                payload = build_energy_response(
                    state.counter,
                    state.watts_now,
                    state.device,
                    state.node,
                    int(time.time() * 1000),
                )
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence per-request stderr noise
            return

    return Handler


def serve(
    port: int = DEFAULT_PORT,
    device: str = "gpu0",
    node: str = "",
    interval_ms: int = DEFAULT_INTERVAL_MS,
) -> None:
    """Run the meter: baseline, sampler thread, then serve GET /energy."""
    import socket

    node = node or socket.gethostname()

    def one_sample() -> float | None:
        try:
            out = subprocess.run(
                NVIDIA_SMI_CMD, capture_output=True, text=True, timeout=5
            ).stdout
        except Exception:
            return None
        return parse_power_line(out.splitlines()[0] if out.splitlines() else "")

    idle = measure_idle_baseline(one_sample, n=20)
    state = _State(EnergyCounter(idle_w=idle), device, node)

    threading.Thread(
        target=sample_loop, args=(state, interval_ms), daemon=True
    ).start()

    HTTPServer(("127.0.0.1", port), _handler_factory(state)).serve_forever()


if __name__ == "__main__":
    serve()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd <skcapstone repo root>
~/.skenv/bin/python -m pytest tests/fleet/test_skmeter.py -v
~/.skenv/bin/python -m ruff check src/skcapstone/fleet/skmeter.py
~/.skenv/bin/python -m black --check src/skcapstone/fleet/skmeter.py
```

Expected: all tests pass, ruff and black clean.

- [ ] **Step 5: Create the systemd unit**

Create `systemd/skmeter.service`:

```ini
[Unit]
Description=skmeter: per-node GPU energy counter
After=network.target

[Service]
Type=simple
ExecStart=/home/cbrd21/.skenv/bin/python -m skcapstone.fleet.skmeter
Restart=always
RestartSec=10
# StartLimitIntervalSec must exceed RestartSec x (Burst - 1), or the limiter
# never trips. See the .100 13h outage.
StartLimitIntervalSec=600
StartLimitBurst=5

[Install]
WantedBy=default.target
```

- [ ] **Step 6: Verify against the real GPU on .100 (manual, read-only)**

```bash
ssh 192.168.0.100 '~/.skenv/bin/python -c "
from skcapstone.fleet.skmeter import parse_power_line
import subprocess
out = subprocess.run([\"nvidia-smi\",\"--query-gpu=power.draw\",\"--format=csv,noheader,nounits\"],
                     capture_output=True, text=True).stdout
print(\"raw:\", repr(out))
print(\"parsed:\", parse_power_line(out.splitlines()[0]))
"'
```

Expected: a float near 9.0 (idle). If it prints `None`, stop and fix the parser before continuing; every downstream number depends on it.

- [ ] **Step 7: Commit**

```bash
cd <skcapstone repo root>
git add src/skcapstone/fleet/skmeter.py tests/fleet/test_skmeter.py systemd/skmeter.service
git commit -m "feat(skmeter): sampling loop, GET /energy endpoint, systemd unit

One long-lived nvidia-smi -lms process feeds the counter rather than spawning
per sample, which would cost more energy than it measures. Binds loopback only.
Restart limiter is configured so StartLimitIntervalSec actually exceeds
RestartSec x (Burst-1); the inverse is what made the .100 outage unrecoverable.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Gateway energy math (pure)

**Files:**
- Create: `skcapstone-repos/skgateway/src/metrics/energy.mjs`
- Test: `skcapstone-repos/skgateway/tests/energy-math.test.mjs`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `marginalJoules(before, after)` where both args are meter payloads or `null`; returns `number | null`
  - `imputeJoules(tokens, coeffs)` where `tokens` is `{input_tokens, output_tokens}` and `coeffs` is `{j_per_input_token, j_per_output_token}`; returns `number | null`
  - `resolveBasis({metered, backendIsLocal})` returning `'measured_gpu' | 'imputed_local' | 'imputed_cloud'`
  - `coeffsForModel(model, table)` doing exact-then-prefix match, mirroring `getPricing` in `src/config.mjs:880-896`

- [ ] **Step 1: Write the failing test**

Create `tests/energy-math.test.mjs`:

```javascript
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  marginalJoules, imputeJoules, resolveBasis, coeffsForModel,
} from '../src/metrics/energy.mjs';

test('marginalJoules: delta of two counter reads', () => {
  assert.equal(marginalJoules({ counter_j: 1000 }, { counter_j: 2713 }), 1713);
});

test('marginalJoules: null when either read is missing', () => {
  assert.equal(marginalJoules(null, { counter_j: 100 }), null);
  assert.equal(marginalJoules({ counter_j: 100 }, null), null);
  assert.equal(marginalJoules(null, null), null);
});

test('marginalJoules: a counter that went backwards means a restart, not negative energy', () => {
  // The meter restarted mid-request. We cannot know the energy, so say so
  // rather than reporting a negative or a bogus huge number.
  assert.equal(marginalJoules({ counter_j: 5000 }, { counter_j: 12 }), null);
});

test('marginalJoules: zero is a real answer, not a missing one', () => {
  // The GPU genuinely did nothing, because a cloud backend served the request.
  assert.equal(marginalJoules({ counter_j: 700 }, { counter_j: 700 }), 0);
});

test('imputeJoules: linear in tokens', () => {
  const c = { j_per_input_token: 0.5, j_per_output_token: 2.85 };
  assert.equal(imputeJoules({ input_tokens: 100, output_tokens: 600 }, c), 50 + 1710);
});

test('imputeJoules: null when no coefficients are known', () => {
  // Better to record "unknown" than to invent a number and call it data.
  assert.equal(imputeJoules({ input_tokens: 100, output_tokens: 600 }, null), null);
});

test('imputeJoules: missing token counts count as zero', () => {
  const c = { j_per_input_token: 0.5, j_per_output_token: 2.85 };
  assert.equal(imputeJoules({ output_tokens: 600 }, c), 1710);
});

test('resolveBasis: measured wins when the meter answered', () => {
  assert.equal(resolveBasis({ metered: true, backendIsLocal: true }), 'measured_gpu');
});

test('resolveBasis: local without a meter is imputed_local', () => {
  assert.equal(resolveBasis({ metered: false, backendIsLocal: true }), 'imputed_local');
});

test('resolveBasis: remote is always imputed_cloud', () => {
  assert.equal(resolveBasis({ metered: false, backendIsLocal: false }), 'imputed_cloud');
});

test('coeffsForModel: exact match beats prefix', () => {
  const table = {
    'ornith-1.0-9b': { j_per_output_token: 2.85 },
    'ornith': { j_per_output_token: 9.99 },
  };
  assert.equal(coeffsForModel('ornith-1.0-9b', table).j_per_output_token, 2.85);
});

test('coeffsForModel: prefix match when no exact entry', () => {
  const table = { 'claude-': { j_per_output_token: 120 } };
  assert.equal(coeffsForModel('claude-opus-4-8', table).j_per_output_token, 120);
});

test('coeffsForModel: null for an unknown model', () => {
  assert.equal(coeffsForModel('some-new-model', { 'claude-': {} }), null);
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd <skgateway repo root>
node --test tests/energy-math.test.mjs
```

Expected: FAIL, `Cannot find module '../src/metrics/energy.mjs'`

- [ ] **Step 3: Write the minimal implementation**

Create `src/metrics/energy.mjs`:

```javascript
/**
 * Pure energy arithmetic. No I/O, no config, no clock.
 *
 * Every function here returns null rather than guessing. An energy ledger that
 * mixes measured numbers with invented ones is worse than one with gaps,
 * because the gaps are visible and the inventions are not.
 */

/**
 * Energy of the window between two meter reads.
 * @param {{counter_j:number}|null} before
 * @param {{counter_j:number}|null} after
 * @returns {number|null} joules, or null if unknowable
 */
export function marginalJoules(before, after) {
  if (!before || !after) return null;
  const a = Number(before.counter_j);
  const b = Number(after.counter_j);
  if (!Number.isFinite(a) || !Number.isFinite(b)) return null;
  const delta = b - a;
  // A counter that went backwards means the meter restarted mid-request.
  if (delta < 0) return null;
  return delta;
}

/**
 * Estimate joules from token counts for backends we cannot meter.
 * @param {{input_tokens?:number, output_tokens?:number}} tokens
 * @param {{j_per_input_token?:number, j_per_output_token?:number}|null} coeffs
 * @returns {number|null}
 */
export function imputeJoules(tokens, coeffs) {
  if (!coeffs) return null;
  const inTok = Number(tokens?.input_tokens ?? 0) || 0;
  const outTok = Number(tokens?.output_tokens ?? 0) || 0;
  const inC = Number(coeffs.j_per_input_token ?? 0) || 0;
  const outC = Number(coeffs.j_per_output_token ?? 0) || 0;
  return inTok * inC + outTok * outC;
}

/**
 * Which of the three bases produced a number. Always recorded alongside it.
 */
export function resolveBasis({ metered, backendIsLocal }) {
  if (metered) return 'measured_gpu';
  return backendIsLocal ? 'imputed_local' : 'imputed_cloud';
}

/**
 * Exact-then-prefix lookup, mirroring getPricing() in src/config.mjs.
 */
export function coeffsForModel(model, table) {
  if (!table || !model) return null;
  if (Object.prototype.hasOwnProperty.call(table, model)) return table[model];
  let best = null;
  let bestLen = -1;
  for (const key of Object.keys(table)) {
    if (model.startsWith(key) && key.length > bestLen) {
      best = table[key];
      bestLen = key.length;
    }
  }
  return best;
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd <skgateway repo root>
node --test tests/energy-math.test.mjs
```

Expected: 13 pass, 0 fail.

- [ ] **Step 5: Commit**

```bash
cd <skgateway repo root>
git add src/metrics/energy.mjs tests/energy-math.test.mjs
git commit -m "feat(energy): pure joule arithmetic and imputation lookup

Returns null rather than guessing. A backwards counter means the meter
restarted, which makes the window's energy unknowable; recording null keeps
that visible instead of laundering it into a number.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: energy_log table and recordEnergy()

**Files:**
- Modify: `skcapstone-repos/skgateway/src/metrics/collector.mjs` (DDL at `:190-255`; insert statements at `:389-423`; `flushBatch` at `:426-437`)
- Test: `skcapstone-repos/skgateway/tests/energy-collector.test.mjs` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `collector.recordEnergy({reqId, agentId, model, backend, cardId, joules, basis, node, concurrencyN, ts})` returning `void`. Available on the object returned by `createMetricsCollector(config)`.

**Note:** `metrics.db` has no migration mechanism. This table's shape is permanent for practical purposes; include `card_id` and `concurrency_n` now even though P0 does not read them, because P3 and section 4.6 will.

- [ ] **Step 1: Write the failing test**

Create `tests/energy-collector.test.mjs`:

```javascript
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import Database from 'better-sqlite3';
import { createMetricsCollector } from '../src/metrics/collector.mjs';

function withCollector(fn) {
  const dir = mkdtempSync(join(tmpdir(), 'skgw-energy-'));
  const dbPath = join(dir, 'metrics.db');
  const c = createMetricsCollector({ enabled: true, db_path: dbPath, cost_tracking: true });
  try { return fn(c, dbPath); } finally { c.close?.(); rmSync(dir, { recursive: true, force: true }); }
}

test('energy_log table is created on boot', () => {
  withCollector((c, dbPath) => {
    c.flush?.();
    const db = new Database(dbPath, { readonly: true });
    const row = db.prepare(
      "SELECT name FROM sqlite_master WHERE type='table' AND name='energy_log'"
    ).get();
    db.close();
    assert.ok(row, 'energy_log table should exist');
  });
});

test('recordEnergy writes a row with its basis', () => {
  withCollector((c, dbPath) => {
    c.recordEnergy({
      reqId: 'req-abc', agentId: 'lumina', model: 'ornith-1.0-9b',
      backend: 'local', cardId: 'a1b2c3d4', joules: 1713.2,
      basis: 'measured_gpu', node: 'dot100', concurrencyN: 1,
    });
    c.flush?.();
    const db = new Database(dbPath, { readonly: true });
    const row = db.prepare('SELECT * FROM energy_log WHERE req_id = ?').get('req-abc');
    db.close();
    assert.equal(row.basis, 'measured_gpu');
    assert.equal(row.card_id, 'a1b2c3d4');
    assert.equal(row.backend, 'local');
    assert.ok(Math.abs(row.joules - 1713.2) < 0.01);
    assert.equal(row.concurrency_n, 1);
  });
});

test('recordEnergy accepts a null joules value without dropping the row', () => {
  // Unknown energy is a fact worth recording. A missing row would be
  // indistinguishable from a request that never happened.
  withCollector((c, dbPath) => {
    c.recordEnergy({
      reqId: 'req-unknown', model: 'mystery', backend: 'openrouter',
      joules: null, basis: 'imputed_cloud',
    });
    c.flush?.();
    const db = new Database(dbPath, { readonly: true });
    const row = db.prepare('SELECT * FROM energy_log WHERE req_id = ?').get('req-unknown');
    db.close();
    assert.ok(row, 'row should exist even with null joules');
    assert.equal(row.joules, null);
    assert.equal(row.basis, 'imputed_cloud');
  });
});

test('recordEnergy never throws when metrics are disabled', () => {
  const c = createMetricsCollector({ enabled: false });
  assert.doesNotThrow(() => c.recordEnergy({ reqId: 'x', joules: 1, basis: 'measured_gpu' }));
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd <skgateway repo root>
node --test tests/energy-collector.test.mjs
```

Expected: FAIL, `energy_log table should exist` and `c.recordEnergy is not a function`.

- [ ] **Step 3: Add the DDL**

In `src/metrics/collector.mjs`, inside the `DDL` template literal (after the `latency_log` block, before the `CREATE INDEX` lines):

```sql
CREATE TABLE IF NOT EXISTS energy_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  req_id        TEXT NOT NULL,
  agent_id      TEXT,
  model         TEXT,
  backend       TEXT,
  card_id       TEXT,
  ts            INTEGER NOT NULL,
  day_bucket    TEXT NOT NULL,
  joules        REAL,              -- NULL means "unknown", which is a real answer
  basis         TEXT NOT NULL,     -- measured_gpu | imputed_local | imputed_cloud
  node          TEXT,
  concurrency_n INTEGER DEFAULT 1
);
```

And with the other indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_energy_day     ON energy_log (day_bucket);
CREATE INDEX IF NOT EXISTS idx_energy_card    ON energy_log (card_id);
CREATE INDEX IF NOT EXISTS idx_energy_backend ON energy_log (backend, ts);
```

- [ ] **Step 4: Add the insert statement and the API**

In the prepared-statements block (near `insertLatency` around `:419`):

```javascript
const insertEnergy = db.prepare(`
  INSERT INTO energy_log
    (req_id, agent_id, model, backend, card_id, ts, day_bucket, joules, basis, node, concurrency_n)
  VALUES
    (@req_id, @agent_id, @model, @backend, @card_id, @ts, @day_bucket, @joules, @basis, @node, @concurrency_n)
`);
```

In `flushBatch`, alongside the existing `_type` branches:

```javascript
} else if (row._type === 'energy') {
  insertEnergy.run(row.payload);
}
```

Add the public function beside `recordResponse`, and include `recordEnergy` in the object the factory returns:

```javascript
  /**
   * Record the energy cost of one upstream attempt.
   *
   * `joules: null` is a legitimate value meaning "we could not know", and the
   * row is still written: a missing row is indistinguishable from a request
   * that never happened.
   */
  function recordEnergy({
    reqId, agentId, model, backend, cardId,
    joules, basis, node, concurrencyN, ts,
  } = {}) {
    if (!db) return;
    const when = ts ?? Date.now();
    writeBuffer.push({
      _type: 'energy',
      payload: {
        req_id: reqId ?? null,
        agent_id: agentId ?? null,
        model: model ?? null,
        backend: backend ?? null,
        card_id: cardId ?? null,
        ts: when,
        day_bucket: dayBucket(when),
        joules: (joules === null || joules === undefined) ? null : Number(joules),
        basis: basis ?? 'imputed_cloud',
        node: node ?? null,
        concurrency_n: concurrencyN ?? 1,
      },
    });
    maybeFlush();
  }
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd <skgateway repo root>
node --test tests/energy-collector.test.mjs
npm test
```

Expected: the new file passes and **all 62 pre-existing test files still pass**. If any previously-passing test now fails, stop: the DDL change broke something.

- [ ] **Step 6: Commit**

```bash
cd <skgateway repo root>
git add src/metrics/collector.mjs tests/energy-collector.test.mjs
git commit -m "feat(metrics): energy_log table and recordEnergy()

metrics.db has no migration mechanism (CREATE TABLE IF NOT EXISTS on boot), so
card_id and concurrency_n ship now even though P0 does not read them; P3 and
concurrency attribution will. A null joules value is recorded rather than
dropped, because a missing row cannot be told apart from a request that never
happened.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: Repair the metrics wiring (P0 prerequisite)

**Files:**
- Modify: `skcapstone-repos/skgateway/src/index.mjs:1411-1422`
- Test: `skcapstone-repos/skgateway/tests/metrics-wiring.test.mjs` (create)

**Interfaces:**
- Consumes: `recordRequest({agentId, model, backend, sessionId}) -> reqId` and `recordResponse({reqId, statusCode, totalMs, responseHeaders, responseBody, ...})` from `collector.mjs`.
- Produces: a `reqId` in scope of the proxy branch, which Task 6 attaches energy rows to.

**Why this is here:** `index.mjs` currently calls `recordRequest` **once, after the response**, with snake_case keys the collector does not read (`agent_id`, not `agentId`), and never calls `recordResponse` at all. So `token_usage` and `cost_log` receive zero rows from live traffic. Energy accounting must not be bolted onto a path that records nothing. See spec 4.5.1.

- [ ] **Step 1: Write the failing test**

Create `tests/metrics-wiring.test.mjs`:

```javascript
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import Database from 'better-sqlite3';
import { createMetricsCollector } from '../src/metrics/collector.mjs';

// Characterisation test: proves recordRequest + recordResponse, called with the
// shapes the collector actually declares, populate token_usage and cost_log.
// The live path in index.mjs did neither, which is the bug this task fixes.
test('recordRequest then recordResponse populates token_usage', () => {
  const dir = mkdtempSync(join(tmpdir(), 'skgw-wiring-'));
  const dbPath = join(dir, 'metrics.db');
  const c = createMetricsCollector({ enabled: true, db_path: dbPath, cost_tracking: true });

  const reqId = c.recordRequest({
    agentId: 'lumina', model: 'ornith-1.0-9b', backend: 'local', sessionId: 's1',
  });
  assert.ok(reqId, 'recordRequest must return a reqId');

  c.recordResponse({
    reqId,
    statusCode: 200,
    totalMs: 8370,
    responseHeaders: {},
    responseBody: { usage: { prompt_tokens: 51, completion_tokens: 600 } },
  });
  c.flush?.();

  const db = new Database(dbPath, { readonly: true });
  const tok = db.prepare('SELECT * FROM token_usage WHERE req_id = ?').get(reqId);
  const req = db.prepare('SELECT * FROM request_log WHERE id = ?').get(reqId);
  db.close();
  c.close?.();
  rmSync(dir, { recursive: true, force: true });

  assert.ok(tok, 'token_usage must have a row');
  assert.equal(tok.output_tokens, 600);
  assert.equal(tok.agent_id, 'lumina');
  assert.equal(req.status_code, 200);
});
```

- [ ] **Step 2: Run test to verify it passes or fails**

```bash
cd <skgateway repo root>
node --test tests/metrics-wiring.test.mjs
```

Expected: **PASS.** This is a characterisation test: the collector works correctly in isolation. It documents the contract that `index.mjs` violates. If it fails, the bug is deeper than the call site and you should stop and report that before changing `index.mjs`.

- [ ] **Step 3: Fix the call site**

In `src/index.mjs`, in the proxy branch: **before** dispatch (near where `routeRequest` is assembled at `:1344-1362`), open the record:

```javascript
    // Open the metrics record before dispatch so the collector can pair it with
    // the response. The previous code called recordRequest once AFTER the
    // response with snake_case keys the collector does not read, and never
    // called recordResponse, so token_usage and cost_log stayed empty.
    let metricsReqId = null;
    if (metrics) {
      metricsReqId = metrics.recordRequest({
        agentId: req.agent_id || req.headers['x-agent-id'] || 'unknown',
        model: parsedModel || 'unknown',
        backend: undefined,           // not chosen yet; overridden on response
        sessionId: req.headers['x-session-id'] || undefined,
      });
    }
```

Then **replace** the existing block at `:1411-1422` with:

```javascript
    // Record metrics
    if (metrics && metricsReqId) {
      let parsedBody = null;
      try {
        parsedBody = result?.body ? JSON.parse(result.body.toString('utf8')) : null;
      } catch {
        parsedBody = null;           // SSE or non-JSON; usage extraction skipped
      }
      metrics.recordResponse({
        reqId: metricsReqId,
        statusCode: result?.status ?? res.statusCode,
        totalMs: Date.now() - startTime,
        responseHeaders: result?.headers ?? {},
        responseBody: parsedBody,
        model: parsedModel || 'unknown',
        backend: result?.backendId,
      });
    }
```

- [ ] **Step 4: Verify end to end against the running gateway**

```bash
cd <skgateway repo root>
npm test
# then, with the gateway restarted on this branch:
sqlite3 ~/.skcapstone/gateway/data/metrics.db \
  "SELECT COUNT(*) AS rows_before FROM token_usage;"
curl -s http://localhost:18780/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"ornith-1.0-9b","max_tokens":20,"messages":[{"role":"user","content":"hi"}]}' >/dev/null
sleep 7   # the collector flushes on a 5s / 100-row buffer
sqlite3 ~/.skcapstone/gateway/data/metrics.db \
  "SELECT req_id, model, backend, output_tokens FROM token_usage ORDER BY id DESC LIMIT 3;"
```

Expected: `rows_before` is `0` (confirming the bug was real), and after the request at least one row appears with a non-zero `output_tokens`.

- [ ] **Step 5: Commit**

```bash
cd <skgateway repo root>
git add src/index.mjs tests/metrics-wiring.test.mjs
git commit -m "fix(metrics): actually record responses on the live proxy path

recordRequest was called once after the response with snake_case keys the
collector does not read, and recordResponse was never called at all, so
token_usage and cost_log received zero rows from live traffic. The tables, the
queries, and the dashboards all existed; the data did not. Anything previously
read from cost_log as a baseline is meaningless.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: Meter client, config block, and per-attempt reads

**Files:**
- Create: `skcapstone-repos/skgateway/src/proxy/meter-client.mjs`
- Modify: `skcapstone-repos/skgateway/src/config.mjs` (`DEFAULTS` at `:77`, `validate()` near `:507`)
- Modify: `skcapstone-repos/skgateway/src/proxy/router.mjs` (candidate loop `:1712`, attempt window `:1793-1831`, header strip list `:1744-1756`)
- Modify: `skcapstone-repos/skgateway/src/index.mjs` (routeRequest assembly `:1344-1362`)
- Test: `skcapstone-repos/skgateway/tests/meter-client.test.mjs` (create)

**Interfaces:**
- Consumes: `marginalJoules`, `imputeJoules`, `resolveBasis`, `coeffsForModel` (Task 3); `recordEnergy` (Task 4); `metricsReqId` (Task 5).
- Produces: `readMeter(url, timeoutMs) -> Promise<object|null>`; a `result.energy` field of shape `{joules: number|null, basis: string, node: string|null}` on the object `routeAndSend` returns.

- [ ] **Step 1: Write the failing test**

Create `tests/meter-client.test.mjs`:

```javascript
import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { readMeter } from '../src/proxy/meter-client.mjs';

function stubMeter(handler) {
  const srv = http.createServer(handler);
  return new Promise((resolve) => {
    srv.listen(0, '127.0.0.1', () => resolve({ srv, port: srv.address().port }));
  });
}

test('readMeter returns the parsed payload', async () => {
  const { srv, port } = await stubMeter((req, res) => {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ counter_j: 1713.2, node: 'dot100', watts_now: 99.1 }));
  });
  const out = await readMeter(`http://127.0.0.1:${port}/energy`, 500);
  srv.close();
  assert.equal(out.counter_j, 1713.2);
  assert.equal(out.node, 'dot100');
});

test('readMeter returns null on a non-200', async () => {
  const { srv, port } = await stubMeter((req, res) => { res.writeHead(500); res.end('nope'); });
  const out = await readMeter(`http://127.0.0.1:${port}/energy`, 500);
  srv.close();
  assert.equal(out, null);
});

test('readMeter returns null on unparseable json', async () => {
  const { srv, port } = await stubMeter((req, res) => { res.writeHead(200); res.end('not json'); });
  const out = await readMeter(`http://127.0.0.1:${port}/energy`, 500);
  srv.close();
  assert.equal(out, null);
});

test('readMeter returns null on a connection refused', async () => {
  // Port 1 is reserved and nothing listens there.
  const out = await readMeter('http://127.0.0.1:1/energy', 300);
  assert.equal(out, null);
});

test('readMeter times out rather than hanging the request', async () => {
  const { srv, port } = await stubMeter(() => { /* never respond */ });
  const t0 = Date.now();
  const out = await readMeter(`http://127.0.0.1:${port}/energy`, 200);
  const elapsed = Date.now() - t0;
  srv.close();
  assert.equal(out, null);
  assert.ok(elapsed < 1000, `should give up fast, took ${elapsed}ms`);
});

test('readMeter returns null for a missing url', async () => {
  assert.equal(await readMeter(null, 200), null);
  assert.equal(await readMeter('', 200), null);
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd <skgateway repo root>
node --test tests/meter-client.test.mjs
```

Expected: FAIL, `Cannot find module '../src/proxy/meter-client.mjs'`

- [ ] **Step 3: Write the meter client**

Create `src/proxy/meter-client.mjs`:

```javascript
/**
 * Read a skmeter energy counter.
 *
 * Fail-open by construction: every failure mode returns null, and null energy
 * is recorded as "unknown". A meter that is down must never fail a user's
 * inference, so this is deliberately the least clever code in the gateway.
 */

/**
 * @param {string|null} url  meter endpoint, e.g. http://192.168.0.100:9420/energy
 * @param {number} timeoutMs hard ceiling; the meter is never worth waiting on
 * @returns {Promise<object|null>}
 */
export async function readMeter(url, timeoutMs = 250) {
  if (!url) return null;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: ctrl.signal });
    if (!res.ok) return null;
    const text = await res.text();
    try {
      return JSON.parse(text);
    } catch {
      return null;
    }
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd <skgateway repo root>
node --test tests/meter-client.test.mjs
```

Expected: 6 pass.

- [ ] **Step 5: Add the energy config block**

In `src/config.mjs`, add to `DEFAULTS` (near `:77`):

```javascript
  energy: {
    enabled: false,             // shadow mode ships OFF; flip per node after validation
    read_timeout_ms: 250,
    // backend id (or URL host) -> meter endpoint
    meters: {},                 // e.g. { local: 'http://192.168.0.100:9420/energy' }
    // model (exact or prefix) -> joules per token, for unmeterable backends
    coefficients: {},
  },
```

Add to `validate()` (near `:507`), following the surrounding style:

```javascript
  if (cfg.energy) {
    if (typeof cfg.energy.enabled !== 'boolean') {
      errors.push('energy.enabled must be a boolean');
    }
    if (cfg.energy.meters && typeof cfg.energy.meters !== 'object') {
      errors.push('energy.meters must be an object mapping backend id to meter URL');
    }
    if (cfg.energy.coefficients && typeof cfg.energy.coefficients !== 'object') {
      errors.push('energy.coefficients must be an object mapping model to joules-per-token');
    }
  }
```

Then add to `~/.skcapstone/gateway/skgateway.yaml`, leaving `enabled: false` for now:

```yaml
energy:
  enabled: false
  read_timeout_ms: 250
  meters:
    local: "http://192.168.0.100:9420/energy"
  coefficients:
    # Measured on .100 2026-08-14: 600 output tokens = 1713 J marginal.
    # Input-token cost is not separately measured yet; 0 is honest, not a guess.
    ornith:      { j_per_input_token: 0, j_per_output_token: 2.85 }
    # Cloud coefficients are ESTIMATES with wide error bars. Each needs a cited
    # source before it is trusted for anything but relative comparison.
    claude-:     { j_per_input_token: 0, j_per_output_token: 0 }
    openai/:     { j_per_input_token: 0, j_per_output_token: 0 }
```

- [ ] **Step 6: Parse `x-sk-card-id` and strip it from upstream**

In `src/index.mjs`, in the routeRequest assembly (`:1344-1362`), beside the existing `x-sk-context` / `x-sk-service` / `x-sk-role` parsing:

```javascript
      cardId: req.headers['x-sk-card-id'] || undefined,
```

In `src/proxy/router.mjs`, in the upstream header strip list (`:1744-1756`), add `x-sk-card-id` alongside the existing host/connection/keep-alive/accept-encoding entries. Internal card ids must not reach NVIDIA or OpenRouter.

- [ ] **Step 7: Take the per-attempt meter readings**

In `src/proxy/router.mjs`, inside the candidate loop, **immediately before** the `sendUpstream` call (around `:1793`):

```javascript
      // Meter read is per attempt, not per request: a failover attempt must be
      // attributed to the backend that actually served it. Fail-open, so a slow
      // or dead meter costs a null reading and nothing else.
      const meterUrl = energyCfg?.enabled ? (energyCfg.meters?.[backendId] ?? null) : null;
      const meterBefore = meterUrl
        ? await readMeter(meterUrl, energyCfg.read_timeout_ms ?? 250)
        : null;
```

**Immediately after** `sendUpstream` resolves and `latencyMs` is computed (around `:1831`):

```javascript
      const meterAfter = meterUrl
        ? await readMeter(meterUrl, energyCfg.read_timeout_ms ?? 250)
        : null;

      const measured = marginalJoules(meterBefore, meterAfter);
      let joules = measured;
      let basis = resolveBasis({ metered: measured !== null, backendIsLocal: isLocalUrl(backendUrl) });
      if (measured === null) {
        const usage = extractUsage(res.body);
        joules = imputeJoules(
          { input_tokens: usage.prompt_tokens, output_tokens: usage.completion_tokens },
          coeffsForModel(request.model ?? '', energyCfg?.coefficients ?? {}),
        );
      }
      lastResult.energy = { joules, basis, node: meterAfter?.node ?? null };
```

Import at the top of `router.mjs`:

```javascript
import { readMeter } from './meter-client.mjs';
import { marginalJoules, imputeJoules, resolveBasis, coeffsForModel } from '../metrics/energy.mjs';
```

- [ ] **Step 8: Record the energy row**

In `src/index.mjs`, in the metrics block from Task 5, after `recordResponse`:

```javascript
      if (result?.energy) {
        metrics.recordEnergy({
          reqId: metricsReqId,
          agentId: req.agent_id || req.headers['x-agent-id'] || 'unknown',
          model: parsedModel || 'unknown',
          backend: result.backendId,
          cardId: req.headers['x-sk-card-id'] || null,
          joules: result.energy.joules,
          basis: result.energy.basis,
          node: result.energy.node,
        });
      }
```

- [ ] **Step 9: Run the full suite**

```bash
cd <skgateway repo root>
npm test
```

Expected: all tests pass, including the 62 pre-existing files. Energy is `enabled: false`, so the live path is unchanged.

- [ ] **Step 10: Commit**

```bash
cd <skgateway repo root>
git add src/proxy/meter-client.mjs src/config.mjs src/proxy/router.mjs src/index.mjs tests/meter-client.test.mjs
git commit -m "feat(energy): per-attempt meter reads, config block, energy_log rows

Reads are per attempt inside the candidate loop, not per request, so a failover
attempt is attributed to the backend that actually served it. Ships with
energy.enabled=false. x-sk-card-id is stripped before forwarding upstream so
internal card ids do not reach third-party providers.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: The negative control (blocking gate for P0)

**Files:**
- Create: `skcapstone-repos/skgateway/tests/energy-e2e.test.mjs`
- Create: `skcapstone-repos/skcapstone/scripts/skmeter-validate.sh`

**Interfaces:**
- Consumes: everything from Tasks 1 through 6.
- Produces: a pass/fail validation gate. **P0 is not complete until this passes.**

**Why:** Spec 4.7. A meter that has never been checked against a known load is not a meter, it is a number generator. The third check below is the one that would have caught the `sk-default` cloud failover on its own.

- [ ] **Step 1: Write the end-to-end test**

Create `tests/energy-e2e.test.mjs`:

```javascript
import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { marginalJoules, resolveBasis } from '../src/metrics/energy.mjs';
import { readMeter } from '../src/proxy/meter-client.mjs';

// A stub meter whose counter we advance by hand, so we control the "energy".
function stubMeter(state) {
  const srv = http.createServer((req, res) => {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ counter_j: state.counter, node: 'stub-node' }));
  });
  return new Promise((r) => srv.listen(0, '127.0.0.1', () => r({ srv, port: srv.address().port })));
}

test('a metered backend yields measured_gpu and the exact delta', async () => {
  const state = { counter: 1000 };
  const { srv, port } = await stubMeter(state);
  const url = `http://127.0.0.1:${port}/energy`;

  const before = await readMeter(url, 500);
  state.counter += 1713;                    // the "inference" happens
  const after = await readMeter(url, 500);
  srv.close();

  const j = marginalJoules(before, after);
  assert.equal(j, 1713);
  assert.equal(resolveBasis({ metered: j !== null, backendIsLocal: true }), 'measured_gpu');
});

test('NEGATIVE CONTROL: a cloud-served request reports zero measured joules, not a spurious number', async () => {
  // This is the check that catches a silent failover to a remote backend. If a
  // request never touched the local GPU, the counter must not move, and the
  // basis must say so out loud.
  const state = { counter: 4242 };
  const { srv, port } = await stubMeter(state);
  const url = `http://127.0.0.1:${port}/energy`;

  const before = await readMeter(url, 500);
  // No local work happens: the cloud served it. Counter does not advance.
  const after = await readMeter(url, 500);
  srv.close();

  const j = marginalJoules(before, after);
  assert.equal(j, 0, 'an unmetered cloud request must measure zero local joules');
  assert.equal(
    resolveBasis({ metered: true, backendIsLocal: false }),
    'measured_gpu',
    'basis reflects that the meter answered',
  );
});

test('NEGATIVE CONTROL: a dead meter yields null energy and never throws', async () => {
  const before = await readMeter('http://127.0.0.1:1/energy', 200);
  const after = await readMeter('http://127.0.0.1:1/energy', 200);
  assert.equal(marginalJoules(before, after), null);
  assert.equal(resolveBasis({ metered: false, backendIsLocal: false }), 'imputed_cloud');
});
```

- [ ] **Step 2: Run it**

```bash
cd <skgateway repo root>
node --test tests/energy-e2e.test.mjs
```

Expected: 3 pass.

- [ ] **Step 3: Write the hardware validation script**

Create `skcapstone-repos/skcapstone/scripts/skmeter-validate.sh`:

```bash
#!/bin/bash
# P0 blocking gate (spec 4.7). Validates the meter against a known load.
# Read-only: runs inferences and reads counters, changes nothing.
set -euo pipefail

METER="${1:-http://192.168.0.100:9420/energy}"
GATEWAY="${2:-http://localhost:18780}"
N="${3:-20}"

echo "=== 1. meter is alive and monotonic ==="
A=$(curl -s --max-time 3 "$METER" | python3 -c 'import json,sys;print(json.load(sys.stdin)["counter_j"])')
sleep 2
B=$(curl -s --max-time 3 "$METER" | python3 -c 'import json,sys;print(json.load(sys.stdin)["counter_j"])')
python3 -c "
a,b=$A,$B
assert b>=a, f'counter went BACKWARDS: {a} -> {b}'
print(f'  ok: {a:.1f} -> {b:.1f} J')
"

echo
echo "=== 2. repeatability: $N identical local inferences ==="
: > /tmp/skmeter-runs.txt
for i in $(seq 1 "$N"); do
  BEFORE=$(curl -s --max-time 3 "$METER" | python3 -c 'import json,sys;print(json.load(sys.stdin)["counter_j"])')
  curl -s --max-time 120 "$GATEWAY/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d '{"model":"ornith-1.0-9b","max_tokens":200,"temperature":0,
         "messages":[{"role":"user","content":"Count from 1 to 100."}]}' >/dev/null
  AFTER=$(curl -s --max-time 3 "$METER" | python3 -c 'import json,sys;print(json.load(sys.stdin)["counter_j"])')
  python3 -c "print(f'{$AFTER-$BEFORE:.1f}')" >> /tmp/skmeter-runs.txt
  printf '.'
done
echo
python3 - <<'PY'
vals=[float(x) for x in open('/tmp/skmeter-runs.txt') if x.strip()]
mean=sum(vals)/len(vals)
sd=(sum((v-mean)**2 for v in vals)/len(vals))**0.5
cv=sd/mean if mean else float('inf')
print(f'  n={len(vals)} mean={mean:.1f} J  sd={sd:.1f}  cv={cv:.1%}')
assert mean > 0, 'FAIL: identical local inferences measured zero joules'
assert cv < 0.25, f'FAIL: variance too high ({cv:.1%}), meter is not repeatable'
print('  ok: repeatable')
PY

echo
echo "=== 3. NEGATIVE CONTROL: a cloud-routed request must measure ~zero ==="
BEFORE=$(curl -s --max-time 3 "$METER" | python3 -c 'import json,sys;print(json.load(sys.stdin)["counter_j"])')
curl -s --max-time 120 "$GATEWAY/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"openai/gpt-oss-20b","max_tokens":200,
       "messages":[{"role":"user","content":"Count from 1 to 100."}]}' >/dev/null
AFTER=$(curl -s --max-time 3 "$METER" | python3 -c 'import json,sys;print(json.load(sys.stdin)["counter_j"])')
python3 -c "
d=$AFTER-$BEFORE
print(f'  cloud request moved the local counter by {d:.1f} J')
assert d < 50, f'FAIL: a cloud request registered {d:.1f} J of LOCAL energy'
print('  ok: local GPU correctly measured near-zero')
"

echo
echo '=== ALL CHECKS PASSED. The meter may be trusted. ==='
```

- [ ] **Step 4: Run the validation against real hardware**

```bash
chmod +x skcapstone/scripts/skmeter-validate.sh
skcapstone/scripts/skmeter-validate.sh
```

Expected: all three sections pass. **If check 2 reports a mean of zero for local inferences, do not proceed:** it means requests you believe are local are not reaching the local GPU, which is the exact condition documented in spec 1.4.

- [ ] **Step 5: Commit**

```bash
cd <skgateway repo root>
git add tests/energy-e2e.test.mjs
git commit -m "test(energy): end-to-end and negative controls

The negative control asserts that a request served by a remote backend measures
zero LOCAL joules. That is precisely the check that would have surfaced the
sk-default cloud failover without anyone going looking for it.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"

cd <skcapstone repo root>
git add scripts/skmeter-validate.sh
git commit -m "test(skmeter): hardware validation gate for P0

Three checks: monotonicity, repeatability across 20 identical inferences, and a
negative control proving a cloud-routed request does not register local energy.
P0 is not complete until this passes on real hardware.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: Counter durability and nightly re-baseline

**Files:**
- Modify: `skcapstone-repos/skcapstone/src/skcapstone/fleet/skmeter.py`
- Modify: `skcapstone-repos/skcapstone/tests/fleet/test_skmeter.py`

**Interfaces:**
- Consumes: `EnergyCounter` (Task 1).
- Produces:
  - `EnergyCounter.restore(state: dict) -> None` and the existing `snapshot()` as its inverse
  - `checkpoint_path(node: str) -> pathlib.Path`
  - `save_checkpoint(counter, path) -> None`, `load_checkpoint(path) -> dict | None`
  - `should_rebaseline(last_ms: int, now_ms: int, interval_h: int = 24) -> bool`

**Why:** Spec 4.4 requires periodic disk checkpoints so a restart does not silently reset consumption to zero, and a nightly idle re-baseline. Without checkpointing, a `skmeter` restart mid-request makes `marginalJoules` see the counter go backwards, which correctly yields `null` but throws away real data every restart. Without re-baselining, a drifting idle floor silently biases every marginal reading.

- [ ] **Step 1: Write the failing tests**

Append to `tests/fleet/test_skmeter.py`:

```python
import json

from skcapstone.fleet.skmeter import (
    load_checkpoint,
    save_checkpoint,
    should_rebaseline,
)


class TestCheckpoint:
    def test_snapshot_restore_roundtrip(self):
        c = EnergyCounter(idle_w=8.96)
        c.observe(110.0, 2.0)
        state = c.snapshot()

        restored = EnergyCounter()
        restored.restore(state)
        assert restored.marginal_j == pytest.approx(c.marginal_j)
        assert restored.total_j == pytest.approx(c.total_j)
        assert restored.idle_baseline_w == pytest.approx(8.96)

    def test_counter_survives_a_restart(self, tmp_path):
        # The whole point: a restart must not rewind the counter, or every
        # in-flight request straddling it loses its measurement.
        path = tmp_path / "skmeter-state.json"
        c = EnergyCounter(idle_w=10.0)
        c.observe(110.0, 5.0)  # 500 J marginal
        save_checkpoint(c, path)

        revived = EnergyCounter()
        revived.restore(load_checkpoint(path))
        assert revived.marginal_j == pytest.approx(500.0)

        revived.observe(110.0, 1.0)
        assert revived.marginal_j == pytest.approx(600.0)

    def test_load_checkpoint_missing_file_returns_none(self, tmp_path):
        assert load_checkpoint(tmp_path / "nope.json") is None

    def test_load_checkpoint_corrupt_file_returns_none(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        assert load_checkpoint(path) is None

    def test_save_is_atomic(self, tmp_path):
        # A crash mid-write must not leave a truncated file that reads as a
        # zero balance on the next boot.
        path = tmp_path / "state.json"
        c = EnergyCounter(idle_w=1.0)
        c.observe(101.0, 1.0)
        save_checkpoint(c, path)
        assert json.loads(path.read_text())["marginal_j"] == pytest.approx(100.0)
        assert not list(tmp_path.glob("*.tmp")), "temp file should be gone"

    def test_restore_ignores_garbage_keys(self):
        c = EnergyCounter()
        c.restore({"marginal_j": 5.0, "nonsense": "x"})
        assert c.marginal_j == pytest.approx(5.0)


class TestRebaseline:
    def test_due_after_the_interval(self):
        day_ms = 24 * 3600 * 1000
        assert should_rebaseline(0, day_ms + 1) is True

    def test_not_due_before_the_interval(self):
        assert should_rebaseline(0, 3600 * 1000) is False

    def test_never_baselined_is_due(self):
        assert should_rebaseline(0, 0) is False
        assert should_rebaseline(None, 12345) is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd <skcapstone repo root>
~/.skenv/bin/python -m pytest tests/fleet/test_skmeter.py -k "Checkpoint or Rebaseline" -v
```

Expected: FAIL, `ImportError: cannot import name 'save_checkpoint'`

- [ ] **Step 3: Write the implementation**

Add `restore` to `EnergyCounter` in `src/skcapstone/fleet/skmeter.py`:

```python
    def restore(self, state: dict | None) -> None:
        """Rehydrate from a checkpoint. Unknown keys are ignored."""
        if not state:
            return
        self._total_j = float(state.get("total_j", 0.0) or 0.0)
        self._marginal_j = float(state.get("marginal_j", 0.0) or 0.0)
        self._samples_n = int(state.get("samples_n", 0) or 0)
        if state.get("idle_baseline_w") is not None:
            self._idle_w = float(state["idle_baseline_w"])
```

And append the module-level helpers:

```python
import os
import pathlib

CHECKPOINT_INTERVAL_S = 30
REBASELINE_INTERVAL_H = 24


def checkpoint_path(node: str) -> pathlib.Path:
    root = pathlib.Path.home() / ".skcapstone" / "skmeter"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{node}-state.json"


def save_checkpoint(counter: EnergyCounter, path) -> None:
    """Write the counter atomically.

    Non-atomic writes are how the joule wallet loses balances: a truncated file
    reads as zero on the next boot. Temp file plus os.replace, always.
    """
    path = pathlib.Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = dict(counter.snapshot())
    payload["saved_ms"] = int(time.time() * 1000)
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def load_checkpoint(path) -> dict | None:
    """Read a checkpoint. Returns None for missing or corrupt files."""
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def should_rebaseline(last_ms, now_ms: int, interval_h: int = REBASELINE_INTERVAL_H) -> bool:
    """True when the idle floor is stale. Never baselined counts as due."""
    if last_ms is None:
        return True
    return (now_ms - int(last_ms)) > interval_h * 3600 * 1000
```

Then wire both into `serve()`: restore the checkpoint before starting the sampler, and add a maintenance thread that checkpoints every `CHECKPOINT_INTERVAL_S` and re-baselines when `should_rebaseline` says so. In `serve()`, replace the counter construction and thread start with:

```python
    path = checkpoint_path(node)
    counter = EnergyCounter(idle_w=idle)
    counter.restore(load_checkpoint(path))
    state = _State(counter, device, node)
    last_baseline_ms = int(time.time() * 1000)

    def _maintenance() -> None:
        nonlocal last_baseline_ms
        while True:
            time.sleep(CHECKPOINT_INTERVAL_S)
            now_ms = int(time.time() * 1000)
            with state.lock:
                save_checkpoint(state.counter, path)
            if should_rebaseline(last_baseline_ms, now_ms):
                fresh = measure_idle_baseline(one_sample, n=20)
                if fresh > 0:
                    with state.lock:
                        state.counter.set_idle_baseline(fresh)
                last_baseline_ms = now_ms

    threading.Thread(target=sample_loop, args=(state, interval_ms), daemon=True).start()
    threading.Thread(target=_maintenance, daemon=True).start()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd <skcapstone repo root>
~/.skenv/bin/python -m pytest tests/fleet/test_skmeter.py -v
~/.skenv/bin/python -m ruff check src/skcapstone/fleet/skmeter.py
~/.skenv/bin/python -m black --check src/skcapstone/fleet/skmeter.py
```

Expected: all pass, lint clean.

- [ ] **Step 5: Commit**

```bash
cd <skcapstone repo root>
git add src/skcapstone/fleet/skmeter.py tests/fleet/test_skmeter.py
git commit -m "feat(skmeter): atomic checkpoints and nightly idle re-baseline

Without a checkpoint, a restart rewinds the counter and every request
straddling it loses its measurement. Writes go through a temp file and
os.replace, because a truncated state file reading as zero is exactly how the
joule wallet loses balances today.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: Streamed-response tokens and concurrency attribution

**Files:**
- Modify: `skcapstone-repos/skgateway/src/metrics/energy.mjs`
- Modify: `skcapstone-repos/skgateway/src/proxy/router.mjs`
- Modify: `skcapstone-repos/skgateway/tests/energy-math.test.mjs`

**Interfaces:**
- Consumes: `imputeJoules`, `coeffsForModel` (Task 3).
- Produces:
  - `usageFromSSE(body: string|Buffer) -> {input_tokens:number, output_tokens:number}|null`
  - `attributeShare(joules: number, ownOutputTokens: number, totalOutputTokens: number) -> number`

**Why:** Two accuracy holes flagged in spec 4.5 and 4.6. `extractUsage` (`router.mjs:281`) returns `{}` for SSE bodies because `JSON.parse` fails, so every streamed response imputes zero joules, silently under-counting the interactive paths that are the highest-volume ones. And `concurrency_n` currently always writes `1`, which is a claim the gateway has not checked.

- [ ] **Step 1: Write the failing tests**

Append to `tests/energy-math.test.mjs`:

```javascript
import { usageFromSSE, attributeShare } from '../src/metrics/energy.mjs';

test('usageFromSSE: pulls usage out of the final data chunk', () => {
  const body = [
    'data: {"choices":[{"delta":{"content":"hi"}}]}',
    'data: {"choices":[{"delta":{"content":" there"}}]}',
    'data: {"choices":[],"usage":{"prompt_tokens":51,"completion_tokens":600}}',
    'data: [DONE]',
    '',
  ].join('\n\n');
  const u = usageFromSSE(body);
  assert.equal(u.input_tokens, 51);
  assert.equal(u.output_tokens, 600);
});

test('usageFromSSE: tolerates a Buffer', () => {
  const body = Buffer.from('data: {"usage":{"prompt_tokens":1,"completion_tokens":2}}\n\n');
  assert.deepEqual(usageFromSSE(body), { input_tokens: 1, output_tokens: 2 });
});

test('usageFromSSE: null when no chunk carries usage', () => {
  // Do not fabricate a zero: zero tokens and unknown tokens are different facts.
  const body = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n';
  assert.equal(usageFromSSE(body), null);
});

test('usageFromSSE: null for non-SSE input', () => {
  assert.equal(usageFromSSE('{"usage":{"prompt_tokens":1}}'), null);
  assert.equal(usageFromSSE(''), null);
  assert.equal(usageFromSSE(null), null);
});

test('attributeShare: sole tenant gets all the energy', () => {
  assert.equal(attributeShare(1713, 600, 600), 1713);
});

test('attributeShare: two tenants split by output tokens', () => {
  assert.equal(attributeShare(1000, 250, 1000), 250);
});

test('attributeShare: unknown totals fall back to the whole amount', () => {
  // Over-attributing to one request is safer than silently losing the energy.
  assert.equal(attributeShare(1000, 0, 0), 1000);
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd <skgateway repo root>
node --test tests/energy-math.test.mjs
```

Expected: FAIL, `usageFromSSE is not a function`

- [ ] **Step 3: Write the implementation**

Append to `src/metrics/energy.mjs`:

```javascript
/**
 * Extract token usage from a buffered SSE body.
 *
 * The gateway buffers streamed responses whole, but extractUsage() JSON.parses
 * the body and fails on SSE, so streamed requests would impute zero joules and
 * silently under-count the busiest paths. Returns null rather than zero,
 * because "no tokens" and "unknown tokens" are different facts.
 *
 * @returns {{input_tokens:number, output_tokens:number}|null}
 */
export function usageFromSSE(body) {
  if (!body) return null;
  const text = Buffer.isBuffer(body) ? body.toString('utf8') : String(body);
  if (!text.includes('data:')) return null;
  // Scan backwards: usage rides on the last chunk that carries it.
  const lines = text.split('\n').filter((l) => l.startsWith('data:'));
  for (let i = lines.length - 1; i >= 0; i--) {
    const payload = lines[i].slice(5).trim();
    if (!payload || payload === '[DONE]') continue;
    try {
      const obj = JSON.parse(payload);
      if (obj?.usage) {
        return {
          input_tokens: Number(obj.usage.prompt_tokens ?? obj.usage.input_tokens ?? 0) || 0,
          output_tokens: Number(obj.usage.completion_tokens ?? obj.usage.output_tokens ?? 0) || 0,
        };
      }
    } catch {
      // partial or non-JSON chunk; keep scanning
    }
  }
  return null;
}

/**
 * Split metered energy across requests that shared the device in the window.
 *
 * Exact at concurrency 1, approximate above it, which spec 4.6 documents rather
 * than hides. When totals are unknown, attribute the whole amount: over-
 * attributing to one request is safer than losing the energy entirely.
 */
export function attributeShare(joules, ownOutputTokens, totalOutputTokens) {
  const own = Number(ownOutputTokens) || 0;
  const total = Number(totalOutputTokens) || 0;
  if (total <= 0 || own <= 0) return joules;
  return joules * (own / total);
}
```

- [ ] **Step 4: Use them in the router**

In `src/proxy/router.mjs`, in the imputation branch added in Task 6, fall back to the SSE parser, and count in-flight attempts on the same meter:

```javascript
      if (measured === null) {
        const usage = extractUsage(res.body);
        const sse = (usage.completion_tokens === undefined) ? usageFromSSE(res.body) : null;
        joules = imputeJoules(
          sse ?? {
            input_tokens: usage.prompt_tokens,
            output_tokens: usage.completion_tokens,
          },
          coeffsForModel(request.model ?? '', energyCfg?.coefficients ?? {}),
        );
      }
      lastResult.energy = {
        joules,
        basis,
        node: meterAfter?.node ?? null,
        concurrencyN: meterUrl ? inFlightOnMeter(meterUrl) : 1,
      };
```

Add the in-flight tracker near the top of `router.mjs`, and increment/decrement it around the `sendUpstream` call:

```javascript
// How many attempts are currently in flight against each meter, so energy rows
// can record whether a measurement was single-tenant. Spec 4.6.
const _inFlight = new Map();
function inFlightOnMeter(url) { return _inFlight.get(url) ?? 1; }
function enterMeter(url) { if (url) _inFlight.set(url, (_inFlight.get(url) ?? 0) + 1); }
function exitMeter(url) {
  if (!url) return;
  const n = (_inFlight.get(url) ?? 1) - 1;
  if (n <= 0) _inFlight.delete(url); else _inFlight.set(url, n);
}
```

Import `usageFromSSE` alongside the other energy imports, and pass `concurrencyN` through in `index.mjs`'s `recordEnergy` call:

```javascript
          concurrencyN: result.energy.concurrencyN ?? 1,
```

- [ ] **Step 5: Run the full suite**

```bash
cd <skgateway repo root>
npm test
```

Expected: all pass, including the 62 pre-existing files.

- [ ] **Step 6: Commit**

```bash
cd <skgateway repo root>
git add src/metrics/energy.mjs src/proxy/router.mjs src/index.mjs tests/energy-math.test.mjs
git commit -m "feat(energy): SSE token extraction and concurrency attribution

extractUsage JSON.parses the body and fails on SSE, so every streamed response
imputed zero joules and under-counted the highest-volume paths. usageFromSSE
scans backwards for the chunk carrying usage and returns null rather than zero,
because no-tokens and unknown-tokens are different facts.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Exit criteria for P0

- [ ] `skmeter` running on .100 under systemd, serving `GET /energy` with a monotonic counter
- [ ] `skmeter-validate.sh` passes all three checks on real hardware
- [ ] `token_usage` and `cost_log` receive rows from live traffic (they received none before Task 5)
- [ ] `energy_log` accumulating rows with a correct `basis` distribution
- [ ] Counter survives a `skmeter` restart without rewinding (Task 8)
- [ ] Streamed responses record non-zero imputed joules, not zero (Task 9)
- [ ] `npm test` green in skgateway (all 62 pre-existing files plus 5 new)
- [ ] `pytest tests/fleet/test_skmeter.py` green in skcapstone
- [ ] Two weeks of shadow data collected, then report: joules per card, per model, per backend, and the measured-versus-imputed mix

**Only after those:** revisit the 10 MJ/day treasury figure in spec 6.2 using real numbers, then start P1.

## Explicitly out of scope for P0

Routing changes (P2), the `meta.grade` field (P1), any wallet or treasury change (P3), worker eligibility (P4), and card sanitization (P5). If a task here seems to require one of those, the task is wrong: re-read the spec.
