"""Synthetic retained test proof for real native CLI acceptance tests."""

import json
import socket

from skcapstone.fleet.production_test_plan import junit_counts, seal_plan, sha, write_once
from tests.fleet.test_production_tests import junit


def retained_proof(home, source, workspace, criteria_sha256):
    """Retain exact plan, outputs and JUnit; never execute or qualify a live worker."""
    binding = {
        "source_card": source["card"],
        "source_owner": source["owner"],
        "source_claim_revision": source["claim"],
        "source_head": source["head"],
        "source_tree": source["tree"],
        "source_revision": source["revision"],
        "criteria_sha256": criteria_sha256,
    }
    (home / "fleet").mkdir(mode=0o700, exist_ok=True)
    path = seal_plan(
        home,
        binding,
        workspace,
        {"authority_host": socket.gethostname().split(".")[0].lower()},
        "synthetic-test-operator",
        "b" * 64,
    )
    plan = json.loads(path.read_text())
    digest = sha(path.read_bytes())
    directory = home / "fleet/test-runs" / digest
    directory.mkdir(parents=True, mode=0o700)
    checks = []
    for check in plan["checks"]:
        raw = b"Synthetic test fixture output, not a production qualification.\n"
        output = directory / (check["id"] + ".log")
        output.write_bytes(raw)
        output.chmod(0o600)
        checks.append({**check, "exit_code": 0, "output_sha256": sha(raw)})
    raw = junit()
    output = directory / "pytest.xml"
    output.write_bytes(raw)
    output.chmod(0o600)
    receipt = {
        "binding": binding,
        "plan_sha256": digest,
        "checks": checks,
        "junit_sha256": sha(raw),
        "counts": junit_counts(raw),
    }
    path = directory / "receipt.json"
    write_once(path, receipt)
    audit = {
        "receipt_path": str(path),
        "receipt_sha256": sha(path.read_bytes()),
        "plan_sha256": digest,
        "source_head": source["head"],
        "checks": checks,
        "counts": receipt["counts"],
    }
    return binding, audit
