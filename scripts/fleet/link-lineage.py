#!/usr/bin/env python3
"""Read-only reconciliation of open Link PRs to CardStore card lineage.

This is deliberately a dry-run tool: it never writes CardStore or the Link
observation feed.  CardStore is read through its projection and event files are
parsed as JSON, so malformed evidence cannot silently become lineage.
"""
from __future__ import annotations
import argparse, hashlib, json, re, subprocess
from pathlib import Path
from typing import Any

PR_RE = re.compile(r"\bPR\s*#?\s*(\d+)\b", re.I)
CARD_RE = re.compile(r"\b([0-9a-f]{8})\b", re.I)

def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def _events(home: Path, card_id: str) -> list[dict[str, Any]]:
    root = home / "cards" / card_id / "events"
    out = []
    for path in sorted(root.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)  # fail closed on malformed CardStore evidence
            if not isinstance(item, dict):
                raise ValueError(f"non-object CardStore event: {path}")
            out.append(item)
    return out

def _revision(home: Path, card_id: str) -> str | None:
    events = _events(home, card_id)
    if not events:
        return None
    # The event identity is the exact immutable revision, not lifecycle status.
    event = events[-1]
    return str(event.get("event_id") or event.get("claim_revision") or hashlib.sha256(_json(event).encode()).hexdigest())

def _pr_number(text: str) -> int | None:
    m = PR_RE.search(text)
    return int(m.group(1)) if m else None

def reconcile(open_prs: list[dict[str, Any]], cards: list[dict[str, Any]], home: Path, exclusions: dict[str, str] | None = None) -> dict[str, Any]:
    exclusions = exclusions or {}
    by_pr: dict[int, list[dict[str, Any]]] = {}
    for card in cards:
        text = " ".join(str(card.get(k, "")) for k in ("title", "description", "acceptance_criteria"))
        n = _pr_number(text)
        if n is not None:
            by_pr.setdefault(n, []).append(card)
    records = []
    for pr in sorted(open_prs, key=lambda x: int(x["number"])):
        number = int(pr["number"])
        if str(number) in exclusions:
            records.append({"pr": number, "classification": "excluded", "reason": exclusions[str(number)]})
            continue
        candidates = by_pr.get(number, [])
        sources = [c for c in candidates if "review" not in str(c.get("title", "")).lower()]
        reviews = [c for c in candidates if "review" in str(c.get("title", "")).lower()]
        if len(sources) != 1:
            classification = "unresolved"
        elif len(reviews) != 1:
            classification = "unresolved"
        else:
            source, review = sources[0], reviews[0]
            records.append({"pr": number, "classification": "lineage-complete", "source_card": source["id"], "source_generation": _revision(home, source["id"]), "review_card": review["id"], "review_revision": _revision(home, review["id"]), "head_revision": pr.get("headRefOid")})
            continue
        records.append({"pr": number, "classification": classification, "candidate_source_cards": [c["id"] for c in sources], "candidate_review_cards": [c["id"] for c in reviews]})
    counts = {k: sum(r["classification"] == k for r in records) for k in ("lineage-complete", "excluded", "unresolved")}
    body = {"schema": "skcapstone.link-lineage.dry-run.v1", "source_revision": hashlib.sha256(_json(open_prs).encode()).hexdigest(), "coverage": counts, "unresolved_prs": [r["pr"] for r in records if r["classification"] == "unresolved"], "records": records}
    body["evidence_hash"] = hashlib.sha256(_json(body).encode()).hexdigest()
    return body

def fetch_prs(repo: str) -> list[dict[str, Any]]:
    cmd = ["gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "500", "--json", "number,title,headRefOid,baseRefName"]
    return json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)

def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--repo", default="smilinTux/skcapstone"); ap.add_argument("--home", type=Path, default=Path.home()/".skcapstone"); ap.add_argument("--output", type=Path); ap.add_argument("--exclude", type=Path)
    args = ap.parse_args()
    from skcoord.card_store import CardStore
    cards = [c.model_dump(mode="json") for c in CardStore(args.home).list_cards(include_archived=True)]
    exclusions = json.loads(args.exclude.read_text()) if args.exclude else {}
    report = reconcile(fetch_prs(args.repo), cards, args.home, exclusions)
    rendered = _json(report) + "\n"
    if args.output: args.output.write_text(rendered, encoding="utf-8")
    else: print(rendered, end="")
    return 0
if __name__ == "__main__": raise SystemExit(main())
