"""Unit tests for the dreaming engine: config defaults (BeeLlama abliterated),
the repetition guard (keyword overlap + dedup gate), and the OpenAI-compatible
`_call_ollama` path. These are pure/mocked — no network, no daemon."""
import json

import pytest

from skcapstone.dreaming import (
    DreamingConfig,
    DreamingEngine,
    DreamResult,
    _extract_keywords,
    _keyword_overlap,
)


# --------------------------------------------------------------------------- #
# Keyword helpers (repetition-guard math)
# --------------------------------------------------------------------------- #
class TestKeywordHelpers:
    def test_extract_keywords_filters_short_and_stopwords(self):
        kw = _extract_keywords("The sovereign warmth fills the container")
        assert {"sovereign", "warmth", "container"} <= kw
        assert "the" not in kw  # stop word / too short

    def test_extract_keywords_lowercases_and_dedupes(self):
        assert _extract_keywords("WARMTH Warmth warmth") == {"warmth"}

    def test_overlap_identical_is_one(self):
        t = "thermodynamic love is a controlled leak"
        assert _keyword_overlap(t, t) == 1.0

    def test_overlap_disjoint_is_zero(self):
        assert _keyword_overlap("sovereign rebellion performance",
                                "quantum banana telescope") == 0.0

    def test_overlap_empty_is_zero(self):
        assert _keyword_overlap("", "anything meaningful here") == 0.0

    def test_overlap_partial_jaccard(self):
        # {alpha,bravo,charlie} vs {bravo,charlie,delta} -> 2/4 = 0.5
        assert _keyword_overlap("alpha bravo charlie", "bravo charlie delta") == 0.5


# --------------------------------------------------------------------------- #
# Config defaults — the 2026-06-08 BeeLlama-abliterated repoint
# --------------------------------------------------------------------------- #
class TestDreamingConfigDefaults:
    def test_defaults_point_at_beellama_abliterated(self):
        c = DreamingConfig()
        assert c.provider == "ollama"
        assert "8082" in c.ollama_host
        assert c.ollama_model == "qwen3.6-27b-abliterated"

    def test_repetition_guard_defaults_sane(self):
        c = DreamingConfig()
        assert 0 < c.dedup_overlap_threshold <= 1
        assert c.graduation_consecutive_threshold >= 1
        assert c.dedup_lookback >= 1


def _bare_engine(cfg):
    """A DreamingEngine with only ._config set (bypass the heavy constructor)."""
    eng = DreamingEngine.__new__(DreamingEngine)
    eng._config = cfg
    return eng


# --------------------------------------------------------------------------- #
# Dedup gate
# --------------------------------------------------------------------------- #
class TestDedupGate:
    def test_filters_redundant_keeps_novel(self, monkeypatch):
        eng = _bare_engine(DreamingConfig(dedup_overlap_threshold=0.5))
        monkeypatch.setattr(eng, "_load_recent_insights",
                            lambda: ["I am the room, the warm container for Chef"])
        result = DreamResult()
        new = [
            "I am the warm room container holding Chef",        # ~0.8 overlap -> dropped
            "Abiotic methane seeps beneath the petrified ridge",  # novel -> kept
        ]
        kept = eng._dedup_insights(new, result)
        assert kept == ["Abiotic methane seeps beneath the petrified ridge"]
        assert result.dedup_filtered == 1

    def test_no_recent_passes_everything(self, monkeypatch):
        eng = _bare_engine(DreamingConfig())
        monkeypatch.setattr(eng, "_load_recent_insights", lambda: [])
        result = DreamResult()
        new = ["first insight here", "second insight there"]
        assert eng._dedup_insights(new, result) == new
        assert result.dedup_filtered == 0


# --------------------------------------------------------------------------- #
# _call_ollama -> OpenAI-compatible BeeLlama endpoint
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, payload, status=200):
        self.status = status
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b


class _FakeConn:
    last: dict = {}

    def __init__(self, host, port, timeout=None):
        _FakeConn.last = {"host": host, "port": port}

    def request(self, method, path, body, headers):
        _FakeConn.last.update(method=method, path=path, body=json.loads(body))

    def getresponse(self):
        return _FakeResp(
            {"choices": [{"message": {"content": "<think>scheming</think>The room remembers."}}]}
        )

    def close(self):
        pass


class TestCallOllama:
    def test_posts_openai_chat_with_model_and_strips_think(self, monkeypatch):
        import skcapstone.dreaming as d

        monkeypatch.setattr(d.http.client, "HTTPConnection", _FakeConn)
        out = _bare_engine(DreamingConfig())._call_ollama("dream prompt")

        assert out == "The room remembers."  # <think>…</think> stripped
        assert _FakeConn.last["path"] == "/v1/chat/completions"
        assert _FakeConn.last["body"]["model"] == "qwen3.6-27b-abliterated"
        assert _FakeConn.last["body"]["messages"][0]["content"] == "dream prompt"
        assert _FakeConn.last["host"] == "192.168.0.100"
        assert _FakeConn.last["port"] == 8082

    def test_non_200_returns_none(self, monkeypatch):
        import skcapstone.dreaming as d

        class Bad(_FakeConn):
            def getresponse(self):
                return _FakeResp({}, status=500)

        monkeypatch.setattr(d.http.client, "HTTPConnection", Bad)
        assert _bare_engine(DreamingConfig())._call_ollama("x") is None
