"""Mailbox + bootstrap tests.

The property that matters most is the FILENAME rule: one writer per host. That
is what keeps Syncthing from ever having two versions of a mailbox to
reconcile, after a shared skmail.jsonl silently swallowed a message on
2026-08-25. A test that only checked send/read would not catch a regression
that reintroduced a shared file.
"""

from __future__ import annotations

import json

import pytest

from skcapstone.coord_mail import (
    COORD_SUBDIRS,
    ack,
    bootstrap,
    mailbox_dir,
    read,
    send,
    tail,
    writer_file,
)


def test_writer_file_is_per_agent_per_host(tmp_path):
    """One writer per host, or Syncthing gets two versions of one file."""
    a = writer_file(tmp_path, "lumina", host="noroc2027")
    b = writer_file(tmp_path, "lumina", host="chiap08")
    c = writer_file(tmp_path, "jarvis", host="noroc2027")
    assert a != b, "same agent on two hosts must not share a file"
    assert a != c, "two agents on one host must not share a file"
    assert a.name == "lumina@noroc2027.jsonl"


def test_recipient_is_case_insensitive(tmp_path):
    send(tmp_path, "lumina", "Jarvis", "normal", "s", "b", host="h")
    assert len(read(tmp_path, "jarvis")) == 1
    assert len(read(tmp_path, "JARVIS")) == 1


def test_sender_casing_folds_to_one_mailbox(tmp_path):
    send(tmp_path, "Lumina", "jarvis", "normal", "s", "one", host="h")
    send(tmp_path, "lumina", "jarvis", "normal", "s", "two", host="h")
    files = list(mailbox_dir(tmp_path).glob("*.jsonl"))
    assert len(files) == 1, "Lumina and lumina must share one writer file"


def test_to_all_reaches_everyone(tmp_path):
    send(tmp_path, "jarvis", "all", "urgent", "s", "b", host="h")
    assert len(read(tmp_path, "anyone")) == 1


def test_comma_list_recipients(tmp_path):
    send(tmp_path, "jarvis", "lumina,mero", "normal", "s", "b", host="h")
    assert len(read(tmp_path, "lumina")) == 1
    assert len(read(tmp_path, "mero")) == 1
    assert len(read(tmp_path, "someone-else")) == 0


def test_ack_advances_cursor_and_is_idempotent(tmp_path):
    send(tmp_path, "a", "b", "normal", "s", "1", host="h")
    assert ack(tmp_path, "b") == 1
    assert read(tmp_path, "b") == []
    assert ack(tmp_path, "b") == 0, "acking twice must not error or double count"


def test_bad_priority_is_refused_not_downgraded(tmp_path):
    """urgent means 'stop what you are doing'; a typo must not become normal."""
    with pytest.raises(ValueError):
        send(tmp_path, "a", "b", "URGENT!!", "s", "b", host="h")


def test_malformed_line_is_skipped_not_fatal(tmp_path):
    send(tmp_path, "a", "b", "normal", "s", "good", host="h")
    p = writer_file(tmp_path, "a", host="h")
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("{ this is not json\n")
    msgs = read(tmp_path, "b")
    assert len(msgs) == 1, "a partial mid-write line must not lose the whole mailbox"


def test_messages_merge_across_writers_in_time_order(tmp_path):
    send(tmp_path, "a", "z", "normal", "s", "first", host="h1")
    send(tmp_path, "b", "z", "normal", "s", "second", host="h2")
    bodies = [m["body"] for m in read(tmp_path, "z")]
    assert bodies == ["first", "second"]


def test_on_disk_record_shape(tmp_path):
    """Format must stay readable by the bash implementation's files.

    The legacy keys are pinned exactly: a rename or a removal breaks
    scripts/fleet/skmail, which reads these records with json.loads and looks
    keys up by name. ADDITIVE fields are allowed, because a reader that pulls
    named keys ignores ones it does not know; the addition is asserted
    explicitly below so it stays a deliberate act rather than a drift.
    """
    send(tmp_path, "a", "b", "fyi", "subj", "body", host="h")
    line = writer_file(tmp_path, "a", host="h").read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    legacy = {"ts", "from", "to", "priority", "re", "body", "host"}
    assert legacy <= set(rec), "a legacy key was renamed or dropped"
    assert set(rec) - legacy == {"from_fqid"}, "an undocumented field appeared"


def test_bootstrap_creates_skeleton_and_is_idempotent(tmp_path):
    first = bootstrap(tmp_path, agent="lumina")
    for sub in COORD_SUBDIRS:
        assert (tmp_path / "coordination" / sub).is_dir()
    assert (tmp_path / "cards").is_dir()
    assert (tmp_path / "evidence").is_dir()
    assert (tmp_path / "evidence" / "decisions").is_dir()
    assert first["created"], "first run must create something"

    second = bootstrap(tmp_path, agent="lumina")
    assert second["created"] == [], "re-run must create nothing"


def test_bootstrap_without_agent_makes_no_mailbox(tmp_path):
    result = bootstrap(tmp_path)
    assert result["mailbox"] is None
    assert list(mailbox_dir(tmp_path).glob("*.jsonl")) == []


def test_read_on_empty_home_does_not_throw(tmp_path):
    assert read(tmp_path, "nobody") == []
    assert tail(tmp_path) == []


def _write_identity(home, agent, fqid):
    p = home / "agents" / agent / "identity"
    p.mkdir(parents=True, exist_ok=True)
    (p / "identity.json").write_text(json.dumps({"name": agent, "fqid": fqid}), encoding="utf-8")


def test_send_stamps_sender_fqid(tmp_path):
    """A message records WHICH estate's agent sent it, not just a bare name."""
    bootstrap(tmp_path, agent="lumina")
    _write_identity(tmp_path, "lumina", "lumina@chef.skworld")
    rec = send(tmp_path, "lumina", "jarvis", "fyi", "subj", "body")
    assert rec["from"] == "lumina"
    assert rec["from_fqid"] == "lumina@chef.skworld"


def test_send_without_identity_still_sends_unqualified(tmp_path):
    """No identity file must not block mail; the field is explicitly None."""
    bootstrap(tmp_path, agent="lumina")
    rec = send(tmp_path, "lumina", "jarvis", "fyi", "subj", "body")
    assert "from_fqid" in rec, "key must always be present so absent is explicit"
    assert rec["from_fqid"] is None


def test_same_name_different_estates_are_distinguishable(tmp_path):
    """The whole point: two estates run the same agent NAMES.

    `jarvis@chef.skworld` and `jarvis@casey.skworld` are different beings. The
    bare `from` cannot tell them apart; the stamp can.
    """
    bootstrap(tmp_path, agent="jarvis")
    _write_identity(tmp_path, "jarvis", "jarvis@chef.skworld")
    chef_side = send(tmp_path, "jarvis", "lumina", "fyi", "s", "b")

    other = tmp_path / "other-estate"
    bootstrap(other, agent="jarvis")
    _write_identity(other, "jarvis", "jarvis@casey.skworld")
    casey_side = send(other, "jarvis", "lumina", "fyi", "s", "b")

    assert chef_side["from"] == casey_side["from"] == "jarvis"
    assert chef_side["from_fqid"] != casey_side["from_fqid"]
