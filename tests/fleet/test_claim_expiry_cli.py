import json

from skcapstone.fleet import claim_expiry_cli


def test_reports_reclaimable_as_json(tmp_path, capsys, monkeypatch):
    from tests.fleet.test_claim_expiry_observe import _card, _iso

    _card(
        tmp_path,
        "aaaa1111",
        [
            {
                "action": "claim",
                "owner": "jarvis",
                "writer": "jarvis",
                "claim_revision": "rev1",
                "ts": _iso(-100),
            },
        ],
    )
    _card(
        tmp_path,
        "bbbb2222",
        [
            {
                "action": "claim",
                "owner": "fresh",
                "writer": "fresh",
                "claim_revision": "rev2",
                "ts": _iso(-1),
            },
        ],
    )
    rc = claim_expiry_cli.main(["--home", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    ids = {r["card_id"]: r for r in payload["verdicts"]}
    assert ids["aaaa1111"]["reclaimable"] is True
    assert ids["bbbb2222"]["reclaimable"] is False
    assert payload["mode"] == "off"
    assert payload["reclaimable_count"] == 1


def test_exit_code_is_zero_even_with_findings(tmp_path):
    """A report is not a failure. Nothing in CI should go red because the
    fleet has expired claims."""
    assert claim_expiry_cli.main(["--home", str(tmp_path), "--json"]) == 0


def test_reclaimable_only_filters_output(tmp_path, capsys):
    from tests.fleet.test_claim_expiry_observe import _card, _iso

    _card(
        tmp_path,
        "aaaa1111",
        [
            {
                "action": "claim",
                "owner": "jarvis",
                "writer": "jarvis",
                "claim_revision": "rev1",
                "ts": _iso(-100),
            },
        ],
    )
    _card(
        tmp_path,
        "bbbb2222",
        [
            {
                "action": "claim",
                "owner": "fresh",
                "writer": "fresh",
                "claim_revision": "rev2",
                "ts": _iso(-1),
            },
        ],
    )
    rc = claim_expiry_cli.main(["--home", str(tmp_path), "--json", "--reclaimable-only"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    ids = {r["card_id"] for r in payload["verdicts"]}
    assert ids == {"aaaa1111"}
    # counts still reflect the whole fleet, not just the filtered rows shown
    assert payload["held_count"] == 2
    assert payload["reclaimable_count"] == 1


def test_text_output_is_not_json(tmp_path, capsys):
    from tests.fleet.test_claim_expiry_observe import _card, _iso

    _card(
        tmp_path,
        "aaaa1111",
        [
            {
                "action": "claim",
                "owner": "jarvis",
                "writer": "jarvis",
                "claim_revision": "rev1",
                "ts": _iso(-10),
            },
        ],
    )
    rc = claim_expiry_cli.main(["--home", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mode=off" in out
    assert "aaaa1111" in out
    assert "RECLAIM" not in out  # -10h is within the 48h default TTL


def test_never_writes_anything_under_home(tmp_path):
    """A report CLI must be read only. Nothing new should appear under
    --home after a run, in JSON or text mode, filtered or not."""
    from tests.fleet.test_claim_expiry_observe import _card, _iso

    _card(
        tmp_path,
        "aaaa1111",
        [
            {
                "action": "claim",
                "owner": "jarvis",
                "writer": "jarvis",
                "claim_revision": "rev1",
                "ts": _iso(-100),
            },
        ],
    )
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    claim_expiry_cli.main(["--home", str(tmp_path), "--json"])
    claim_expiry_cli.main(["--home", str(tmp_path)])
    claim_expiry_cli.main(["--home", str(tmp_path), "--reclaimable-only"])
    after = sorted(str(p) for p in tmp_path.rglob("*"))
    assert before == after


def test_reports_mode_from_env(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SKFLEET_CLAIM_TTL_MODE", "report")
    rc = claim_expiry_cli.main(["--home", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "report"
