"""The .100 smoke probe must not certify sovereignty from a model NAME.

Card 16af7915. The probe used to carry
``SOVEREIGN_MODELS="ornith qwen llama mxbai beellama"`` and match it as a
SUBSTRING against the ``model`` field of the gateway's response body. Measured
against the live gateway ledger (``skgateway/data/metrics.db``, ``energy_log``,
opened read-only) on 2026-08-17: **76 rows** carry one of those tokens while
running on ``backend=nvidia``, ``basis=imputed_cloud``. The allowlist certified
cloud-served open weights as sovereign, and the probe printed PASS through all
of it.

Every fixture below is a row that really exists in that table.

MERGE ORDER: the one definition lives in skharness
(``skharness/autocode/sovereignty.py``, PR on `feat/one-sovereignty-definition`)
and this repo CALLS it rather than mirroring it, so the two ends cannot drift.
These tests therefore require that skharness change to be present. They do NOT
skip when it is absent: a gate that reports green because it could not run is
the same class of defect as the allowlist it replaces.

The import is DEFERRED into ``_sov()`` rather than done at module scope, and
that is not a style choice. A module-scope import of a missing module raises
during COLLECTION, and a collection error aborts the entire pytest run: the
first push of this file took all 5,915 other tests down with it. Deferring the
import confines the blast radius to these tests, which still FAIL (loudly, with
the ImportError as the reason) rather than skip. Red here, green everywhere
else, is the honest shape of "this needs the other PR first".
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fleet" / "dot100-inference-smoke.sh"


def _sov():
    """THE definition, imported from skharness. Never reimplemented here.

    Deliberately NOT wrapped in a try/except that skips: if this raises, these
    tests fail and say why. See the module docstring.
    """
    from skharness.autocode import sovereignty

    return sovereignty


# (backend, basis, node, model) exactly as recorded in energy_log.
CLOUD_LLAMA = ("nvidia", "imputed_cloud", None, "meta/llama-3.3-70b-instruct")
CLOUD_NEMOTRON = ("nvidia", "imputed_cloud", None, "nvidia/llama-3.3-nemotron-super-49b-v1")
CLOUD_ORNITH = ("nvidia", "imputed_cloud", None, "ornith-big")
CLOUD_QWEN = ("nvidia", "imputed_cloud", None, "qwen3.8-27b-huihui-abliterated-q4_k_m")
SOVEREIGN_ORNITH = ("reg:ornith", "measured_gpu", "ollama", "ornith-1.0-9b")

#: The exact token list the retired allowlist carried.
RETIRED_ALLOWLIST = ("ornith", "qwen", "llama", "mxbai", "beellama")


# --------------------------------------------------------------- the rule ---


@pytest.mark.parametrize(
    "row", [CLOUD_LLAMA, CLOUD_NEMOTRON, CLOUD_ORNITH, CLOUD_QWEN], ids=lambda r: r[3]
)
def test_a_cloud_served_model_with_an_allowlisted_name_is_not_sovereign(row):
    """THE required negative control. Each of these model names contains a
    token from the retired allowlist AND ran on backend=nvidia. A substring
    match calls all four sovereign. The definition must not."""
    backend, basis, node, model = row
    assert any(
        t in model.lower() for t in RETIRED_ALLOWLIST
    ), "fixture no longer exercises the retired allowlist"
    verdict = _sov().classify(backend, basis, node)
    assert verdict.state == _sov().VIOLATED, verdict.reason
    assert verdict.sovereign is False


def test_a_genuinely_sovereign_row_stays_sovereign():
    """The positive control, so an always-false classifier cannot pass this
    file by refusing everything."""
    backend, basis, node, _model = SOVEREIGN_ORNITH
    assert _sov().classify(backend, basis, node).state == _sov().SOVEREIGN


def test_the_same_weights_flip_verdict_with_the_backend_alone():
    """Sovereignty is a claim about hardware and jurisdiction. Hold the model
    id fixed at `ornith-1.0-9b`, change only who served it, and the answer must
    change. That is the whole card in two lines."""
    assert _sov().classify("nvidia", "imputed_cloud", None).state == _sov().VIOLATED
    assert _sov().classify("reg:ornith", "measured_gpu", "ollama").state == _sov().SOVEREIGN


# ------------------------------------------------------- the script itself ---


def test_the_model_name_allowlist_is_gone_from_the_script():
    """Structural pin. The allowlist must not come back, in any spelling."""
    text = SCRIPT.read_text(encoding="utf-8")
    body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "SOVEREIGN_MODELS" not in body
    assert "SMOKE_SOVEREIGN_MODELS" not in body


def test_the_script_calls_the_one_definition_rather_than_mirroring_it():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "skharness.autocode.sovereignty" in text
    assert "x-sk-backend" in text and "x-sk-energy-basis" in text


# ------------------------------------------------------ end to end, on wire ---


class _StubGateway(BaseHTTPRequestHandler):
    """A gateway that answers one chat completion with a chosen attribution.

    Set ``_StubGateway.attribution`` to (backend, basis, node, model); a None
    field means the header is ABSENT, which is exactly how skgateway reports an
    observable it does not have.
    """

    attribution: tuple = SOVEREIGN_ORNITH

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        backend, basis, node, model = self.attribution
        payload = json.dumps(
            {
                "model": model,
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        if backend:
            self.send_header("x-sk-backend", backend)
        if basis:
            self.send_header("x-sk-energy-basis", basis)
        if node:
            self.send_header("x-sk-energy-node", node)
        self.send_header("x-sk-model-served", model)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):  # noqa: D102
        return


def _run_probe(attribution) -> dict:
    """Run the REAL script against a stub gateway and return the probe row.

    Every other probe in the script is pointed at a closed port on loopback so
    it fails fast; only ``gateway-sovereignty`` is under test here. Reading the
    script's own --json output means this exercises the shipped classification
    path, not a reimplementation of it.
    """
    _StubGateway.attribution = attribution
    server = HTTPServer(("127.0.0.1", 0), _StubGateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = dict(os.environ)
        env.update(
            {
                "SMOKE_GATEWAY_URL": f"http://127.0.0.1:{server.server_port}",
                "SMOKE_HOST": "127.0.0.1",
                "SMOKE_OLLAMA_PORT": "1",
                "SMOKE_EMBED_ARC_PORT": "1",
                "SMOKE_ORNITH_PORT": "1",
                "SMOKE_QWEN_PORT": "1",
                "SMOKE_COMFYUI_PORT": "1",
                "SMOKE_F5TTS_PORT": "1",
                "SMOKE_WHISPER_PORT": "1",
                "SMOKE_CURL_TIMEOUT": "2",
                "SMOKE_CHAT_TIMEOUT": "10",
                "SMOKE_SSH_TIMEOUT": "1",
                "SMOKE_SSH": "nobody@127.0.0.1",
                # The definition lives in skharness and is imported, never copied.
                "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            }
        )
        out = subprocess.run(
            ["bash", str(SCRIPT), "--json"], env=env, capture_output=True, text=True, timeout=180
        )
        report = json.loads(out.stdout)
    finally:
        server.shutdown()
        server.server_close()
    rows = [p for p in report["probes"] if p["name"] == "gateway-sovereignty"]
    assert len(rows) == 1, report
    return rows[0]


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required to run the probe")
def test_the_probe_fails_on_a_cloud_served_allowlisted_name():
    """End to end negative control, on the wire, through the shipped script.
    The response body says `meta/llama-3.3-70b-instruct` and the old substring
    rule passed it. The backend says nvidia."""
    row = _run_probe(CLOUD_LLAMA)
    assert row["result"] == "FAIL", row
    assert "nvidia" in row["detail"]


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required to run the probe")
def test_the_probe_passes_a_genuinely_sovereign_serving():
    """End to end positive control. Without this, a probe that failed
    unconditionally would satisfy the test above."""
    row = _run_probe(SOVEREIGN_ORNITH)
    assert row["result"] == "PASS", row
    assert "reg:ornith" in row["detail"]


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required to run the probe")
def test_the_probe_fails_closed_when_attribution_is_unobservable():
    """A gateway that emits no attribution headers has told us nothing. The
    probe must not read nothing as sovereign: unknown, matched and violated are
    three distinct states and only one of them is a pass."""
    row = _run_probe((None, None, None, "ornith-1.0-9b"))
    assert row["result"] == "FAIL", row
    assert "UNOBSERVED" in row["detail"]


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required to run the probe")
def test_the_probe_fails_closed_when_the_definition_cannot_be_loaded():
    """If the classifier itself will not run, the probe reports that it cannot
    classify. It does NOT fall back to a name check, and it does not pass."""
    _StubGateway.attribution = SOVEREIGN_ORNITH
    server = HTTPServer(("127.0.0.1", 0), _StubGateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        env = dict(os.environ)
        env.update(
            {
                "SMOKE_GATEWAY_URL": f"http://127.0.0.1:{server.server_port}",
                "SMOKE_HOST": "127.0.0.1",
                "SMOKE_OLLAMA_PORT": "1",
                "SMOKE_EMBED_ARC_PORT": "1",
                "SMOKE_ORNITH_PORT": "1",
                "SMOKE_QWEN_PORT": "1",
                "SMOKE_COMFYUI_PORT": "1",
                "SMOKE_F5TTS_PORT": "1",
                "SMOKE_WHISPER_PORT": "1",
                "SMOKE_CURL_TIMEOUT": "2",
                "SMOKE_CHAT_TIMEOUT": "10",
                "SMOKE_SSH_TIMEOUT": "1",
                "SMOKE_SSH": "nobody@127.0.0.1",
                "SMOKE_SOVEREIGNTY_MODULE": "skharness.autocode.no_such_definition",
            }
        )
        out = subprocess.run(
            ["bash", str(SCRIPT), "--json"], env=env, capture_output=True, text=True, timeout=180
        )
        report = json.loads(out.stdout)
    finally:
        server.shutdown()
        server.server_close()
    row = next(p for p in report["probes"] if p["name"] == "gateway-sovereignty")
    assert row["result"] == "FAIL", row
    assert "cannot classify" in row["detail"]
