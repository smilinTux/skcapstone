"""Daemon PATH must not hide the installed native review wrapper."""

import pytest

from skcapstone.fleet import production_builder as production
from skcapstone.fleet import review_dispatch
from skcapstone.fleet.production_review import review_family_allowed


def test_review_runtime_uses_shared_wrapper_resolver(tmp_path, monkeypatch):
    pi = tmp_path / "pi"
    pi.write_text("# synthetic Pi runtime\n")
    wrapper = tmp_path / "skfleet-worker-wrapper.py"
    wrapper.write_text("# synthetic native wrapper\n")
    monkeypatch.setenv("SKFLEET_PI", str(pi))
    monkeypatch.setattr(production, "review_wrapper_path", lambda: str(wrapper))
    assert production.review_runtime() == (str(pi), str(wrapper))


def test_missing_review_runtime_refuses_before_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(review_dispatch.dispatch, "status_path", lambda *args: tmp_path / "status")
    monkeypatch.setattr(review_dispatch.dispatch, "_validated_status", lambda *args: None)
    monkeypatch.setattr(review_dispatch, "validate_request", lambda *args: object())

    def missing():
        raise ValueError("managed review worker runtime unavailable")

    monkeypatch.setattr(production, "review_runtime", missing)
    monkeypatch.setattr(
        review_dispatch,
        "native_command",
        lambda *args: pytest.fail("runtime failure claimed work"),
    )
    with pytest.raises(ValueError, match="runtime unavailable"):
        review_dispatch.consume_review(object(), tmp_path, "node-test", {"card_id": "1234abcd"})


@pytest.mark.parametrize(
    "source,reviewer,labels,expected",
    [
        ("glm", "glm", ["review"], True),
        ("deepseek", "deepseek", ["review"], True),
        ("glm", "deepseek", ["review"], True),
        ("glm", "codex", ["review"], True),
        ("glm", "glm", ["review", "glm-only", "review-distinct-agent"], True),
        ("deepseek", "deepseek", ["review", "glm-only"], False),
        ("glm", "codex", ["review", "glm-only", "review-distinct-agent"], False),
        ("unknown", "glm", ["review"], False),
    ],
)
def test_any_known_family_may_review_unless_pinned(source, reviewer, labels, expected):
    assert review_family_allowed(source, reviewer, labels) is expected
