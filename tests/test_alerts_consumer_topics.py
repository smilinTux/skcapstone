"""T3 — alerts command surfaces consumer <service>.<severity> topics."""

from __future__ import annotations

from skcapstone.cli.alerts import DEFAULT_TOPICS, _style_for_topic


def test_severity_wildcards_subscribed():
    """The command subscribes to severity wildcards so any service is seen."""
    for sev in ("*.critical", "*.error", "*.warn"):
        assert sev in DEFAULT_TOPICS


def test_exact_topic_style_wins():
    assert _style_for_topic("agent.critical") == "bold red"


def test_consumer_topic_styled_by_severity_suffix():
    assert _style_for_topic("skmemory.error") == "red"
    assert _style_for_topic("sksecurity.critical") == "bold red"
    assert _style_for_topic("skvoice.warn") == "yellow"
    assert _style_for_topic("skseed.info") == "cyan"


def test_unknown_topic_is_dim():
    assert _style_for_topic("something.random") == "dim"
    assert _style_for_topic("noseparator") == "dim"
