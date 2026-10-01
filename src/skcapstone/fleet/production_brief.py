"""Concise instructions for claimed production source workers."""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence


def _identity(card_id: str, owner: str, claim_revision: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{8}", card_id):
        raise ValueError("invalid production card")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", owner):
        raise ValueError("invalid production owner")
    if not re.fullmatch(r"[0-9a-f]{32,64}", claim_revision):
        raise ValueError("invalid production claim")


def production_completion_recipe(
    *, card_id: str, owner: str, claim_revision: str, base_revision: str
) -> str:
    """Render an executable artifact check and typed handoff, never commit/push."""
    _identity(card_id, owner, claim_revision)
    if not re.fullmatch(r"[0-9a-f]{40}", base_revision):
        raise ValueError("invalid production base")
    actor = shlex.quote(owner)
    check = (
        "import json,sys; c=json.load(sys.stdin); "
        "assert c['id']==sys.argv[1] and c['owner']==sys.argv[2] "
        "and c['meta']['_claim_revision']==sys.argv[3] "
        "and c['status'] not in ('done','archived') and not c.get('archived')"
    )
    guard = f"--agent {actor}"
    identity_args = f"{card_id} {actor} {claim_revision}"
    verdict = (
        f"skcapstone coord verdict {card_id} PASS_FOR_REVIEW"
        ' --candidate "$candidate" --commit "$head" --tree "$tree"'
        f' --ref "refs/heads/$branch" {guard}'
    )
    return f"""set -euo pipefail
test -d .git
test ! -L .git
test "$(git rev-parse --show-toplevel)" = "$PWD"
test -z "$(git status --porcelain)"
branch=$(git symbolic-ref --short HEAD)
test "$branch" != main
test "$branch" != master
git merge-base --is-ancestor {base_revision} HEAD
head=$(git rev-parse HEAD)
tree=$(git rev-parse 'HEAD^{{tree}}')
printf '%s\\n' "$head"
evidence=docs/evidence/agents/{card_id}/COMPLETION-EVIDENCE.md
test -s "$evidence"
test -f "$evidence"
test ! -L "$evidence"
cmp -- "$evidence" <(git cat-file blob "HEAD:$evidence")
skcapstone coord show {card_id} --json | python3 -c {shlex.quote(check)} {identity_args}
directory="$HOME/.skcapstone/evidence/work/{card_id}"
mkdir -p -- "$directory"
candidate=$(mktemp "$directory/completion-$head.XXXXXX.md")
cat -- "$evidence" > "$candidate"
cmp -- "$evidence" "$candidate"
ls -- "$candidate"
skcapstone coord link {card_id} evidence "$evidence" {guard}
skcapstone coord link {card_id} commit_sha "$head" {guard}
skcapstone coord link {card_id} branch "$branch" {guard}
{verdict}
"""


def production_worker_brief(
    *,
    card_id: str,
    owner: str,
    claim_revision: str,
    workspace: str,
    base_revision: str,
    title: str,
    description: str,
    acceptance_criteria: Sequence[str],
    mail_instructions: str = "",
) -> str:
    """Build the complete producer brief after the controller claims the card."""
    _identity(card_id, owner, claim_revision)
    if not re.fullmatch(r"[0-9a-f]{40}", base_revision):
        raise ValueError("invalid production base")
    if not workspace.startswith("/") or any(ord(char) < 32 for char in workspace):
        raise ValueError("invalid production workspace")
    criteria = "\n".join(f"{index}. {item}" for index, item in enumerate(acceptance_criteria, 1))
    recipe = production_completion_recipe(
        card_id=card_id, owner=owner, claim_revision=claim_revision, base_revision=base_revision
    )
    return f"""PRODUCTION SOURCE WORKER
Read the workspace AGENTS.md and the exact card's TDD before editing; Pi automatic
context/skills are disabled. Use only this card's authorized repository and scope.
Read the folded card through skcapstone coord show --json and verify the exact
owner/claim below. Stop on missing ownership, incomplete dependencies or conflicts.
Use skcapstone coord for all board writes; never edit CardStore files directly.

This workspace is already an isolated clone with a .git directory. Stay here;
do not create a nested worktree, replace its repository or reset another worker.
Use a named feature branch. Commit or push ONLY if the card expressly authorizes
that action. If the required source candidate cannot be committed under the card,
report BLOCKED with that precise unmet contract; this prompt grants no permission.
Do not merge, deploy, change services/credentials or perform external actions.
Preserve privacy/egress and protected Matter boundaries; provider access uses only
the assigned gateway route. Do not expose secrets or lower task requirements.

Run ls after every write; after every authorized commit run git rev-parse HEAD and
echo the hash. Stop immediately on inconsistent tool output. After the second
auto-compaction write .handoff.md, finish the current step and stop for a fresh
session. Use bounded card/TDD-referenced reads and rg; never scan/hash the estate.
{mail_instructions}
Implement only the criteria and run their required tests. Report actual commands,
results, limitations and rollback needs; never invent CI, tests or approval.
Write docs/evidence/agents/{card_id}/COMPLETION-EVIDENCE.md and include it in the
authorized candidate commit. Do not embed that report's own final commit hash;
capture final HEAD/tree externally below. Keep source and evidence custody.
After tests and the authorized commit, run this Bash handoff recipe from the
clean workspace. It verifies committed evidence and copies identical bytes to
shared storage before recording typed PASS_FOR_REVIEW. Do not complete your own
card, self-review, clean up source, release the claim or mutate the candidate
after handoff. The native independent review and completion gates act next.
For BLOCKED, preserve the workspace and write a private report under
~/.skcapstone/evidence/work/{card_id}/ describing the actual unmet contract.
Record a typed verdict, not only a verdict link: skcapstone coord verdict {card_id}
'BLOCKED blocked_on=<category> referent=<precise referent> <actual reason>'
--candidate <private report path> --commit <actual existing HEAD>
--tree <actual existing HEAD tree> --ref <actual existing refs/heads/branch>
--agent {owner}. Read the current owner/claim first as in the recipe below.
Obtain those Git identities from Git; no new commit is required or authorized
for BLOCKED. This report need not claim committed or passing implementation.
If any required identity/evidence is unavailable, report that and retain custody.
Stop after the typed verdict; the controller releases only the exact claim after
verified process death. Never clean up or release it yourself.

```bash
{recipe}```

CARD {card_id}; OWNER {owner}; CLAIM {claim_revision}
WORKSPACE {workspace}
AUTHORIZED BASE {base_revision}
TITLE: {title}
TASK TDD:
{description}
ACCEPTANCE CRITERIA:
{criteria}
"""
