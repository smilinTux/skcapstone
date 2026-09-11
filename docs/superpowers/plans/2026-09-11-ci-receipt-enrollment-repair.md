# Exact-card CI receipts and governed enrollment implementation plan

> For agentic workers: execute this single bounded repair inline, test-first, as assigned by card 48f0ae70. No delegation or runtime installation.

Goal: reject malformed exact-card evidence and allow only centrally approved manifest-only initial enrollment.

Architecture: SKCapstone owns a strict bounded per-card event reader because the installed SKCoord reader does not reject duplicate JSON keys. A shared link adapter sends ci_applicability to CardStore.append_event; ordinary links retain their existing overlay path. A checked-in typed registry pins approved initial repository policy digests. Established profiles remain byte-identical.

Tech stack: Python, pathlib/os descriptor-relative reads, hashlib, Git subprocesses, pytest, existing SKCoord CardStore writer.

Spec: docs/superpowers/specs/2026-09-11-repository-ci-applicability-design.md, as amended by the explicit repair card and coordinating parent's registry and reader decisions.

## Global constraints

- Start at exact clean commit 979572e3e2270b5661b2626760e0d3d9c1a5dc08.
- Do not edit SKCoord, installed packages, or re-export shims.
- No HOME reassignment, large new environments, live install, migration, merge, push, or completion.
- Preserve offline Git controls, exact immutable source pins, every policy declaration, legacy behavior, and all four completion entrypoints.
- Registry entry: normalized https://github.com/smilinTux/skgateway maps to f48fc610962a8da11d1d673665ac4d369dc6f28d43c745c9cc52f6b122bdb24b.

## Single repair task

Files: ci_applicability.py (reader and binder), ci_profile_registry.py (typed pinned registry), coord_links.py (shared link writer), cli/coord.py and mcp_tools/coord_card_tools.py (adapter calls), test_ci_applicability.py and test_coord_completion_parity.py (contracts and public boundaries), CHANGELOG.md and the existing design (updated contract).

- [ ] Add failing tests for exact per-card persistence and validation, global-overlay non-authority, unrelated-card corruption isolation, escaped identifiers, duplicate keys, malformed newer receipts, cross-writer equal-time conflicts, hash-chain tampering, symlinks/hardlinks/FIFO/conflict siblings, and bounded reads. Synthetic rows use cards/<id>/events/<writer>.jsonl and production append_event where possible.
- [ ] Add failing enrollment tests: approved manifest-only addition passes; unregistered/wrong-digest, other changed/deleted/renamed paths, symlink/directory policy, and changed established policy reject. Assert the authorized SKGateway compact JSON plus LF hashes to the exact registry digest. At least one required SUCCESS and every declaration remain mandatory at completion.
- [ ] Run the new tests and retain actual failures before product edits.
- [ ] Implement descriptor-relative no-follow directory and file reads, regular single-link checks, sync-conflict refusal, 8 MiB total/100000 row/1024 file bounds, strict outer JSON, and per-file prev_hash verification. Missing prev_hash remains legacy-compatible; present values must exactly match the previous raw line digest. Normalize file/parser/type failures to ValueError.
- [ ] Implement append_coord_link(home, card_id, key, value, writer): ci_applicability uses strict JSON object parsing and CardStore.append_event(card_id, 'link', writer, link_key=key, link_value=value); other keys use the unchanged CardEventLog path. CLI and MCP invoke this same helper.
- [ ] Implement candidate manifest validation first, exact base manifest presence inspection, and an absent-base branch requiring the normalized repository's registry digest plus git diff --no-ext-diff --no-textconv --no-renames --name-only -z base candidate to equal only .skcapstone/ci-profile.json plus NUL. A present base still requires exact policy bytes.
- [ ] Run focused and four-entrypoint regression tests in retained isolated environments; run Black, Ruff, shim/guidance, docs, build/Twine, diff/Unicode, and pinned Gitleaks checks. Record real failures and limitations, without claiming previous candidate results for this one.
- [ ] Commit the exact candidate, seal source pins and evidence hashes, link the assigned card and move it to review. Verify mediated readback. Do not complete it.
