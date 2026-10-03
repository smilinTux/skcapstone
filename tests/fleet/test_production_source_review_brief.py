"""Execute the actual post-claim selection and source-only native handoff."""

import ast
import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from skcapstone.fleet.production_brief import (
    production_source_review_brief,
    production_worker_brief,
)
from skcapstone.review_verdict import _REQUIRED_CI_LINK_KEYS, validate_review_completion

CARD = "ab264e92"
PARENT = "89508f83"
OWNER = "pi-seraph-host-ab264e92"
CLAIM = "a" * 32
ROTATE = Path(__file__).parents[2] / "scripts/fleet/skfleet-rotate.py"


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def review(tmp_path):
    work = tmp_path / "isolated source"
    work.mkdir()
    git(work, "init", "-b", "review/source")
    git(work, "config", "user.name", "Fixture")
    git(work, "config", "user.email", "fixture@example.invalid")
    report = work / f"docs/evidence/agents/{PARENT}/COMPLETION-EVIDENCE.md"
    report.parent.mkdir(parents=True)
    report.write_text("Actual producer evidence\n")
    (work / "source.py").write_text("value = 1\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "source")
    head = git(work, "rev-parse", "HEAD")
    core = {
        "id": CARD,
        "title": "[REVIEW] Exact source review",
        "owner": OWNER,
        "status": "doing",
        "labels": ["source-only", "review"],
        "links": {},
        "initial_labels": ["source-only", "review"],
        "initial_owner": OWNER,
        "initial_claim_revision": CLAIM,
        "description": "Local reviewer evidence commit authorized; no push.",
        "acceptance_criteria": ["Review exact source and run real required tests"],
        "meta": {
            "_claim_revision": CLAIM,
            "link_source_card": PARENT,
            "link_head_revision": head,
            "candidate_tree": git(work, "rev-parse", "HEAD^{tree}"),
            "producer_identity": "pi-deepseek-builder-node-host-89508f83",
            "candidate_evidence_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        },
    }
    home = tmp_path / "private home"
    home.mkdir()
    native = home / ".skcapstone"
    path = native / "cards" / CARD / "core.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(core))
    binary = tmp_path / "bin"
    binary.mkdir()
    cli = binary / "skcapstone"
    cli.write_text(f"#!{sys.executable}\n" + """import json,os,sys
from pathlib import Path
from skcapstone.card import CardEvent,CardEventLog
from skcapstone.seraph_review_cardstore import LiveCardStoreGateway
args=sys.argv[1:]; home=Path(os.environ['HOME'])/'.skcapstone'
if args[:2]==['coord','show']:
    print((home/'cards'/args[2]/'core.json').read_text())
else:
    assert args[:2]==['coord','link']
    from skcapstone.review_verdict import _REQUIRED_CI_LINK_KEYS
    assert args[3] in {'evidence','review_evidence','review_evidence_sha256',
        'evidence_sha256','reviewer_evidence_sha256','verdict','applicability_receipt',
        'hosted_checks'} | _REQUIRED_CI_LINK_KEYS
    if os.environ.get('TEST_MISSING_GUARDS'): raise SystemExit('No such option: --json')
    corepath=home/'cards'/args[2]/'core.json'; core=json.loads(corepath.read_text())
    assert '--json' in args and len(args[args.index('--transition-id')+1])==64
    assert args[args.index('--expected-claim-revision')+1]==core['meta']['_claim_revision']
    current=LiveCardStoreGateway(home).read_card(args[2]).revision
    assert args[args.index('--expected-source-revision')+1]==current
    CardEventLog(home).append(CardEvent(card_id=args[2],action='link',link_key=args[3],
        link_value=args[4],writer=args[args.index('--agent')+1]))
    revision=LiveCardStoreGateway(home).read_card(args[2]).revision
    print(json.dumps({'card_id':args[2],'source_revision':revision}))
    if args[3]=='evidence' and os.environ.get('TEST_RACE'):
        if os.environ['TEST_RACE']=='claim': core['meta']['_claim_revision']='b'*32
        else: core['meta']['source_revision']='external change'
        corepath.write_text(json.dumps(core))
""")
    cli.chmod(0o700)
    environment = {
        **os.environ,
        "HOME": str(home),
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        "PYTHONPATH": str(ROTATE.parents[2] / "src"),
        "REVIEW_VERDICT": "PASS",
    }
    return dict(
        work=work,
        core=core,
        home=home,
        native=native,
        path=path,
        env=environment,
        head=head,
        brief_path=tmp_path / "brief.txt",
    )


def selected(review, *, production=True, seat="seraph", labels=None):
    """Execute the exact post-claim if/elif and its actual brief-file write."""
    from skcapstone.fleet.production_hosted_review_brief import production_hosted_review_brief

    tree = ast.parse(ROTATE.read_text())
    node = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.If)
        and any(
            isinstance(x, ast.Call)
            and isinstance(x.func, ast.Name)
            and x.func.id == "production_worker_brief"
            for x in ast.walk(n)
        )
    )
    loop = ast.For(
        target=ast.Name(id="_fixture", ctx=ast.Store()),
        iter=ast.List(elts=[ast.Constant(1)], ctx=ast.Load()),
        body=[node],
        orelse=[],
    )
    namespace = dict(
        PRODUCTION_POLICY={} if not production else {"enabled": True},
        _review_seat=seat,
        _source_spec=("repo", "main", review["head"]),
        fresh_claimability={
            "core": review["core"],
            "labels": labels if labels is not None else review["core"]["labels"],
        },
        cid=review["core"]["id"],
        name=review["core"]["owner"],
        claimed_revision=CLAIM,
        workspace=str(review["work"]),
        bf=str(review["brief_path"]),
        brief="LEGACY SIX CI INSTRUCTIONS",
        production_source_review_brief=production_source_review_brief,
        production_worker_brief=production_worker_brief,
        production_hosted_review_brief=production_hosted_review_brief,
        _fanout_request=None,
        _worker_mail_instructions=lambda _: "",
        _worker_mail_routing=lambda *_: "",
        os=os,
        core=review["core"],
        d=None,
        HOST="host",
        log=lambda *_: None,
    )
    review["brief_path"].write_text(namespace["brief"])
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[])),
            str(ROTATE),
            "exec",
        ),
        namespace,
    )
    return review["brief_path"].read_text()


def script(brief, index):
    return brief.split("```bash\n")[index + 1].split("```", 1)[0]


def run_script(review, text):
    return subprocess.run(
        ["bash", "-c", text],
        cwd=review["work"],
        env=review["env"],
        capture_output=True,
        text=True,
        umask=0,
    )


def prepare(review):
    brief = selected(review)
    report = review["work"] / f"docs/evidence/agents/{CARD}/COMPLETION-EVIDENCE.md"
    report.parent.mkdir(parents=True)
    report.write_text("Independent actual test result: fixture PASS.\n")
    result = run_script(review, script(brief, 0))
    assert result.returncode == 0, result.stderr
    git(review["work"], "add", ".")
    git(review["work"], "commit", "-m", "review evidence")
    return brief


def test_actual_postclaim_review_brief_uses_existing_native_applicability(review):
    brief = prepare(review)
    assert "LEGACY SIX CI" not in brief and "ci_check_docs SUCCESS" not in brief
    assert "coord complete" not in brief and "release-claim" not in brief
    assert "skmail" not in brief
    result = run_script(review, script(brief, 1))
    assert result.returncode == 0, result.stderr
    validate_review_completion(CARD, review["core"]["title"], review["native"])
    decision = json.loads(
        (review["work"] / f"docs/evidence/agents/{CARD}/REVIEW-DECISION.json").read_text()
    )
    assert decision["source_head"] == review["head"]
    assert decision["source_tree"] == review["core"]["meta"]["candidate_tree"]
    assert decision["schema"] == "skfleet.source-review-decision/v1"
    report = review["work"] / f"docs/evidence/agents/{CARD}/COMPLETION-EVIDENCE.md"
    assert decision["report_sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
    assert "evidence_commit" not in decision
    assert git(review["work"], "rev-parse", "HEAD") != review["head"]
    assert not git(review["work"], "status", "--porcelain")
    shared = review["native"] / "evidence/work" / CARD
    assert shared.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in shared.iterdir())


@pytest.mark.parametrize(
    "production,seat,labels,expected",
    [
        (False, "seraph", ["source-only", "review"], "LEGACY SIX CI INSTRUCTIONS"),
        (True, None, ["source-only"], "PRODUCTION SOURCE WORKER"),
    ],
)
def test_actual_selection_preserves_other_contracts(review, production, seat, labels, expected):
    assert selected(review, production=production, seat=seat, labels=labels).startswith(expected)


@pytest.mark.parametrize("problem", ["malformed-head", "conflict", "self-review", "hosted"])
def test_invalid_review_binding_never_selects_executable_new_brief(review, problem):
    c = review["core"]
    if problem == "malformed-head":
        c["meta"]["link_head_revision"] += "abcdef"
    elif problem == "conflict":
        c["links"]["candidate_tree"] = "b" * 40
    elif problem == "self-review":
        c["meta"]["producer_identity"] = OWNER
    else:
        c["links"]["pr"] = "https://example.invalid/pull/1"
    assert selected(review) == "LEGACY SIX CI INSTRUCTIONS"
    assert not (review["native"] / "coordination/card_events").exists()


@pytest.mark.parametrize(
    "problem",
    [
        "bad-head",
        "bad-schema",
        "self-hash",
        "report-change",
        "source-change",
        "claim-change",
        "producer-change",
        "candidate-digest-change",
        "source-modified",
    ],
)
def test_handoff_refuses_malformed_evidence_or_changed_card_before_board_writes(review, problem):
    brief = prepare(review)
    decision = review["work"] / f"docs/evidence/agents/{CARD}/REVIEW-DECISION.json"
    if problem in {"bad-head", "self-hash", "bad-schema"}:
        value = json.loads(decision.read_text())
        if problem == "bad-head":
            value["source_head"] += "abcdef"
        elif problem == "bad-schema":
            value["schema"] = "unknown"
        else:
            value["evidence_commit"] = git(review["work"], "rev-parse", "HEAD")
        decision.write_text(json.dumps(value))
        git(review["work"], "add", ".")
        git(review["work"], "commit", "-m", "invalid evidence")
    elif problem == "report-change":
        report = decision.with_name("COMPLETION-EVIDENCE.md")
        report.write_text("changed after decision digest\n")
        git(review["work"], "add", ".")
        git(review["work"], "commit", "-m", "changed report")
    elif problem == "source-modified":
        (review["work"] / "source.py").write_text("value = 2\n")
        git(review["work"], "add", ".")
        git(review["work"], "commit", "-m", "unauthorized source")
    else:
        core = copy.deepcopy(review["core"])
        key = {
            "source-change": "link_head_revision",
            "claim-change": "_claim_revision",
            "producer-change": "producer_identity",
            "candidate-digest-change": "candidate_evidence_sha256",
        }[problem]
        core["meta"][key] = "changed"
        review["path"].write_text(json.dumps(core))
    result = run_script(review, script(brief, 1))
    assert result.returncode != 0
    assert not (review["native"] / "coordination/card_events").exists()


@pytest.mark.parametrize(
    "verdict", ["FAIL", "BLOCKED blocked_on=card referent=ac:1 exact requirement unmet"]
)
def test_nonpass_outcome_does_not_publish_pass_applicability(review, verdict):
    review["env"]["REVIEW_VERDICT"] = verdict
    brief = prepare(review)
    result = run_script(review, script(brief, 1))
    assert result.returncode == 0, result.stderr
    validate_review_completion(CARD, review["core"]["title"], review["native"])
    logs = list((review["native"] / "coordination/card_events").glob("*.jsonl"))
    assert logs and not any('"link_key": "applicability_receipt"' in p.read_text() for p in logs)


@pytest.mark.parametrize("race", ["claim", "source"])
def test_guarded_write_chain_refuses_change_after_first_link(review, race):
    brief = prepare(review)
    review["env"]["TEST_RACE"] = race
    result = run_script(review, script(brief, 1))
    assert result.returncode != 0
    logs = list((review["native"] / "coordination/card_events").glob("*.jsonl"))
    rows = [json.loads(line) for p in logs for line in p.read_text().splitlines()]
    assert [row["link_key"] for row in rows] == ["evidence"]
    with pytest.raises(ValueError):
        validate_review_completion(CARD, review["core"]["title"], review["native"])


def test_missing_authority_guard_support_has_no_legacy_write_fallback(review):
    brief = prepare(review)
    review["env"]["TEST_MISSING_GUARDS"] = "1"
    result = run_script(review, script(brief, 1))
    assert result.returncode != 0
    assert not (review["native"] / "coordination/card_events").exists()


def hosted(review, repository="https://github.com/smilinTux/sklegal"):
    """Bind a hosted review and private evidence to real Git and native state."""
    core = review["core"]
    core["labels"] = core["initial_labels"] = ["review"]
    core["description"] = "Review only. No commits or source changes."
    core["meta"]["repository"] = repository
    core["links"]["pr"] = repository + "/pull/28"
    review["path"].write_text(json.dumps(core))
    directory = review["native"] / "evidence/work" / CARD
    directory.mkdir(parents=True, mode=0o700)
    report = directory / "review summary.md"
    report.write_text("Actual fixture review. Receipt and tests inspected separately.\n")
    report.chmod(0o600)
    review["env"]["REVIEW_EVIDENCE"] = str(report)
    checks = (
        {key: "SUCCESS" for key in _REQUIRED_CI_LINK_KEYS}
        if repository.rstrip("/").removesuffix(".git") == "https://github.com/smilinTux/skcapstone"
        else {"hosted_checks": f"4/4 SUCCESS at exact head {review['head']}"}
    )
    review["env"]["REVIEW_CHECKS_JSON"] = json.dumps(checks)
    return selected(review), report


@pytest.mark.parametrize(
    "repository,has_tree",
    [
        ("https://github.com/smilinTux/sklegal", True),
        ("https://github.com/smilinTux/sklegal", False),
        ("https://github.com/smilinTux/skcapstone.git/", True),
        ("https://skgit.skstack01.douno.it/smilinTux/skcapstone.git", True),
    ],
)
def test_hosted_real_brief_binds_summary_and_applicable_ci(review, repository, has_tree):
    from skcapstone.seraph_review_cardstore import LiveCardStoreGateway

    brief, report = hosted(review, repository)
    if not has_tree:
        del review["core"]["meta"]["candidate_tree"]
        review["path"].write_text(json.dumps(review["core"]))
        brief = selected(review)
    assert brief.startswith("PRODUCTION HOSTED INDEPENDENT REVIEW")
    for forbidden in (
        "PASS_FOR_REVIEW",
        "coord complete",
        "release-claim",
        "git commit",
        "anything you write after",
        "LEGACY SIX CI",
    ):
        assert forbidden not in brief
    expected_checks = json.loads(review["env"]["REVIEW_CHECKS_JSON"])
    assert ("ci_check_docs" in brief) == ("ci_check_docs" in expected_checks)
    assert ("hosted_checks" in brief) == ("hosted_checks" in expected_checks)
    result = run_script(review, script(brief, 0))
    assert result.returncode == 0, result.stderr
    validate_review_completion(CARD, review["core"]["title"], review["native"])
    LiveCardStoreGateway(review["native"]).read_card(CARD)
    rows = [
        json.loads(line)
        for p in (review["native"] / "coordination/card_events").glob("*.jsonl")
        for line in p.read_text().splitlines()
    ]
    links = {row["link_key"]: row["link_value"] for row in rows}
    assert links["evidence"] == links["review_evidence"] == str(report)
    assert {
        links[key]
        for key in ("review_evidence_sha256", "evidence_sha256", "reviewer_evidence_sha256")
    } == {hashlib.sha256(report.read_bytes()).hexdigest()}
    assert {
        key: value
        for key, value in links.items()
        if key == "hosted_checks" or key.startswith("ci_check_")
    } == expected_checks
    assert rows[-1]["link_key"] == "verdict" and links["verdict"] == "PASS"
    assert git(review["work"], "rev-parse", "HEAD") == review["head"]
    assert not git(review["work"], "status", "--porcelain")


@pytest.mark.parametrize(
    "problem",
    [
        "missing-ci",
        "stale-ci",
        "partial-ci",
        "wrong-ci",
        "producer-verdict",
        "bare-blocked",
        "source-dirty",
        "source-commit",
        "claim",
        "binding",
        "mode",
        "symlink",
        "outside",
    ],
)
def test_hosted_handoff_refuses_bad_inputs_before_native_writes(review, problem):
    brief, report = hosted(review)
    env = review["env"]
    if problem in {"missing-ci", "stale-ci", "partial-ci", "wrong-ci"}:
        checks = {"hosted_checks": f"4/4 SUCCESS at exact head {review['head']}"}
        if problem == "missing-ci":
            checks = {}
        elif problem == "stale-ci":
            checks["hosted_checks"] = "4/4 SUCCESS at exact head " + "f" * 40
        elif problem == "partial-ci":
            checks["hosted_checks"] = f"3/4 SUCCESS at exact head {review['head']}"
        else:
            checks = {key: "SUCCESS" for key in _REQUIRED_CI_LINK_KEYS}
        env["REVIEW_CHECKS_JSON"] = json.dumps(checks)
    elif problem in {"producer-verdict", "bare-blocked"}:
        env["REVIEW_VERDICT"] = "PASS_FOR_REVIEW" if problem == "producer-verdict" else "BLOCKED"
    elif problem.startswith("source-"):
        (review["work"] / "source.py").write_text("value = 2\n")
        if problem == "source-commit":
            git(review["work"], "commit", "-am", "unauthorized source mutation")
    elif problem in {"claim", "binding"}:
        key = "_claim_revision" if problem == "claim" else "candidate_evidence_sha256"
        review["core"]["meta"][key] = "b" * 64
        review["path"].write_text(json.dumps(review["core"]))
    elif problem == "mode":
        report.chmod(0o644)
    elif problem == "symlink":
        alternate = report.with_name("alternate.md")
        report.rename(alternate)
        report.symlink_to(alternate)
    else:
        env["REVIEW_EVIDENCE"] = str(review["work"] / "source.py")
    result = run_script(review, script(brief, 0))
    assert result.returncode != 0
    assert not (review["native"] / "coordination/card_events").exists()


def test_hosted_blocked_needs_no_success_and_retains_source(review):
    brief, _ = hosted(review)
    review["env"]["REVIEW_VERDICT"] = "BLOCKED blocked_on=capability referent=ci:28 missing CI"
    review["env"].pop("REVIEW_CHECKS_JSON")
    result = run_script(review, script(brief, 0))
    assert result.returncode == 0, result.stderr
    validate_review_completion(CARD, review["core"]["title"], review["native"])
    assert git(review["work"], "rev-parse", "HEAD") == review["head"]


@pytest.mark.parametrize("race", ["claim", "source"])
def test_hosted_guard_chain_stops_after_concurrent_change(review, race):
    brief, _ = hosted(review)
    review["env"]["TEST_RACE"] = race
    result = run_script(review, script(brief, 0))
    assert result.returncode != 0
    rows = [
        json.loads(line)
        for p in (review["native"] / "coordination/card_events").glob("*.jsonl")
        for line in p.read_text().splitlines()
    ]
    assert [row["link_key"] for row in rows] == ["evidence"]
