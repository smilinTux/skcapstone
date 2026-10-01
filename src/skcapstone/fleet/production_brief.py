"""Concise instructions for claimed production source workers."""

from __future__ import annotations

import json
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


def _private_evidence_stage() -> str:
    """Reuse the reviewed descriptor-relative private staging recipe."""
    # Directory descriptors keep staging inside the opened, non-symlink path.
    return """import os,stat,sys,uuid
from pathlib import Path
card,head,evidence=sys.argv[1:]
flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
fd=os.open(os.environ['HOME'],flags)
try:
    for part in ('.skcapstone','evidence','work',card):
        try: os.mkdir(part,0o700,dir_fd=fd)
        except FileExistsError: pass
        child=os.open(part,flags,dir_fd=fd)
        os.close(fd)
        fd=child
    info=os.fstat(fd)
    if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
        raise ValueError('card evidence directory must be owned and mode 0700')
    name='completion-'+head+'.'+uuid.uuid4().hex+'.md'
    output=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
    with os.fdopen(output,'wb') as stream:
        stream.write(Path(evidence).read_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    path=Path(os.environ['HOME'])/'.skcapstone/evidence/work'/card/name
    if path.stat().st_ino!=os.stat(name,dir_fd=fd,follow_symlinks=False).st_ino:
        raise ValueError('card evidence directory changed')
    print(path)
finally:
    os.close(fd)
"""


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
candidate=$(python3 -c {shlex.quote(_private_evidence_stage())} {card_id} "$head" "$evidence")
cmp -- "$evidence" "$candidate"
ls -- "$candidate"
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


def production_source_review_brief(
    *,
    card_id: str,
    owner: str,
    claim_revision: str,
    workspace: str,
    source_head: str,
    core: dict,
    labels: Sequence[str],
) -> str:
    """Render only the existing native source-only applicability handoff."""
    from ..review_admission import reviewer_candidate_reasons
    from ..review_verdict import _has_pr_or_ci_binding
    from .source_bundle import _binding

    _identity(card_id, owner, claim_revision)
    if "source-only" not in {str(label).lower() for label in labels}:
        raise ValueError("review is not source-only")
    if any(
        _has_pr_or_ci_binding(k) and v is not None
        for container in ("links", "meta")
        for k, v in (core.get(container) or {}).items()
    ):
        raise ValueError("source-only review has hosted PR or CI bindings")
    parent = _binding(core, "link_source_card") or ""
    producer = _binding(core, "producer_identity") or ""
    tree = _binding(core, "candidate_tree") or ""
    digest = _binding(core, "candidate_evidence_sha256") or ""
    if (
        core.get("id") != card_id
        or not re.fullmatch(r"[0-9a-f]{8}", parent)
        or parent == card_id
        or not re.fullmatch(r"[0-9a-f]{40}", source_head)
        or _binding(core, "link_head_revision") != source_head
        or not re.fullmatch(r"[0-9a-f]{40}", tree)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or not producer
        or reviewer_candidate_reasons(owner, producer=producer)
    ):
        raise ValueError("invalid exact source review binding or reviewer")
    expected = dict(
        schema="skfleet.source-review-decision/v1",
        card=card_id,
        parent_card=parent,
        source_head=source_head,
        source_tree=tree,
        reviewer_identity=owner,
    )
    expected_arg = shlex.quote(json.dumps(expected, sort_keys=True))
    bindings_arg = shlex.quote(
        json.dumps(
            {
                "link_source_card": parent,
                "link_head_revision": source_head,
                "candidate_tree": tree,
                "producer_identity": producer,
                "candidate_evidence_sha256": digest,
            },
            sort_keys=True,
        )
    )
    prepare = """import hashlib,json,os,subprocess,sys
from pathlib import Path
d=json.loads(sys.argv[1])
def git(*args): return subprocess.check_output(['git',*args],text=True).strip()
if git('rev-parse','HEAD')!=d['source_head']:
    raise ValueError('review must start at exact candidate')
d['source_head']=git('rev-parse',d['source_head']+'^{commit}')
if git('rev-parse',d['source_head']+'^{tree}')!=d['source_tree']:
    raise ValueError('source tree changed')
d['source_tree']=git('rev-parse',d['source_head']+'^{tree}')
d['verdict']=os.environ['REVIEW_VERDICT']
v=d['verdict']
blocked=(v.startswith('BLOCKED ') and 'blocked_on=' in v and 'referent=' in v)
if v not in ('PASS','FAIL') and not blocked: raise ValueError('invalid review verdict')
p=Path('docs/evidence/agents')/d['card']/'REVIEW-DECISION.json'
p.parent.mkdir(parents=True,exist_ok=True)
if not p.parent.resolve().is_relative_to(Path.cwd()):
    raise ValueError('evidence path escapes workspace')
report=p.with_name('COMPLETION-EVIDENCE.md')
if not report.is_file() or report.is_symlink(): raise ValueError('review report invalid')
d['report_sha256']=hashlib.sha256(report.read_bytes()).hexdigest()
fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
with os.fdopen(fd,'w') as f: json.dump(d,f,indent=2)
"""
    verify = """import hashlib,json,sys
from pathlib import Path
expected=json.loads(sys.argv[1]);d=json.load(open(sys.argv[2]))
if (set(d)!=set(expected)|{'verdict','report_sha256'}
    or any(d.get(k)!=v for k,v in expected.items())):
    raise ValueError('review decision binding changed')
if d['report_sha256']!=hashlib.sha256(Path(sys.argv[3]).read_bytes()).hexdigest():
    raise ValueError('review report digest changed')
v=d['verdict']
blocked=(isinstance(v,str) and v.startswith('BLOCKED ')
         and 'blocked_on=' in v and 'referent=' in v)
if v not in ('PASS','FAIL') and not blocked: raise ValueError('invalid review verdict')
print(v)
"""
    check = """import json,sys
c=json.load(sys.stdin);e=json.loads(sys.argv[1]);claim=sys.argv[2]
if (c.get('id')!=e['card'] or c.get('owner')!=e['reviewer_identity']
    or c.get('meta',{}).get('_claim_revision')!=claim or c.get('archived')
    or c.get('status') not in ('ready','doing','review')
    or c.get('meta',{}).get('claim_conflicts') or 'source-only' not in c.get('labels',[])):
    raise ValueError('review ownership changed')
for key,wanted in json.loads(sys.argv[3]).items():
    values={str(c.get(part,{}).get(key)) for part in ('meta','links')
            if c.get(part,{}).get(key) is not None}
    if values!={wanted}: raise ValueError('review source changed')
"""
    receipt = """import json,sys
card,head,reviewer,digest=sys.argv[1:]
print(json.dumps(dict(type='source-only-applicability',card_id=card,source_head=head,reviewer=reviewer,evidence_digest=digest,governed_pr_ci=False),sort_keys=True))
"""
    revision = """from pathlib import Path
import sys
from skcapstone.seraph_review_cardstore import LiveCardStoreGateway
print(LiveCardStoreGateway(Path.home()/'.skcapstone').read_card(sys.argv[1]).revision)
"""
    transition = """import hashlib,json,sys
print(hashlib.sha256(json.dumps(sys.argv[1:],separators=(',',':')).encode()).hexdigest())
"""
    readback = """import json,re,sys
d=json.load(sys.stdin)
if d.get('card_id')!=sys.argv[1] or not re.fullmatch('[0-9a-f]{64}',d.get('source_revision','')):
    raise ValueError('guarded link readback invalid')
print(d['source_revision'])
"""
    actor = shlex.quote(owner)
    criteria = "\n".join(str(value) for value in core.get("acceptance_criteria") or [])
    return f"""PRODUCTION SOURCE-ONLY INDEPENDENT REVIEW
Use only card {card_id}, owner {owner}, claim {claim_revision}, workspace {workspace}.
Read the exact card TDD and workspace AGENTS.md. Use skcapstone coord for all
board writes. Never mutate CardStore files. This card is a local source-only
review, not hosted PR/CI approval: use the existing applicability contract below.
Do not add PR, hosted_checks or ci_check_* links. Never invent SUCCESS, tests or
approval. Run required tests and report exact commands/results, including failures.
Inspect the bounded candidate diff and necessary surrounding context; expand reads
to resolve findings. Batch independent reads. Identify every card-required test
before running them once; combine overlapping selections only when all required
coverage is preserved. Rerun only for changed code, failures or unresolved findings.
All card-required tests remain mandatory. The controller separately runs the
authoritative acceptance tests; review test results do not replace its receipt.
Do not send unsolicited mail. Do not complete the card or release its claim.
Do not push, merge, deploy, change runtime/configuration or alter producer source.
Keep this isolated workspace; never reset or switch back to the source after the
review evidence commit. Create a named reviewer branch at the imported candidate.
Local evidence commits are allowed only when explicitly authorized by this card.
Source {parent}: verify candidate {source_head}, tree {tree} and producer report
docs/evidence/agents/{parent}/COMPLETION-EVIDENCE.md sha256 {digest}.
Run ls after every write; run git rev-parse HEAD after every authorized commit.
Stop on inconsistent tool output. After the second compaction write .handoff.md,
finish the current step and stop. Retain all source, evidence and claim custody.

After reviewing, write docs/evidence/agents/{card_id}/COMPLETION-EVIDENCE.md with
actual findings, tests, limitations and a PASS, FAIL or precise BLOCKED outcome.
Set REVIEW_VERDICT to that actual decision, then generate the decision metadata
from Git below. Do not type hashes manually. Never put evidence_commit or any
hash of the containing commit inside a committed file: that self-reference cannot
be satisfied. Capture the resulting evidence commit separately after committing.

```bash
set -euo pipefail
: "${{REVIEW_VERDICT:?Set the actual independent PASS, FAIL or structured BLOCKED decision}}"
export REVIEW_VERDICT
producer_report=docs/evidence/agents/{parent}/COMPLETION-EVIDENCE.md
test -f "$producer_report"; test ! -L "$producer_report"
test "$(sha256sum "$producer_report" | cut -d ' ' -f1)" = {digest}
cmp -- "$producer_report" <(git cat-file blob '{source_head}':"$producer_report")
python3 -c {shlex.quote(prepare)} {expected_arg}
ls docs/evidence/agents/{card_id}/REVIEW-DECISION.json
```

Commit only the report and decision on the named reviewer branch, when the card
authorizes that local commit. Afterward run this handoff from the clean workspace:

```bash
set -euo pipefail
test -d .git; test ! -L .git
test "$(git rev-parse --show-toplevel)" = "$PWD"
test -z "$(git status --porcelain)"
branch=$(git symbolic-ref --short HEAD)
test "$branch" != main; test "$branch" != master
source_head=$(git rev-parse '{source_head}^{{commit}}')
test "$source_head" = {source_head}
test "$(git rev-parse "$source_head^{{tree}}")" = {tree}
head=$(git rev-parse HEAD)
test "$head" != "$source_head"
git merge-base --is-ancestor "$source_head" "$head"
printf '%s\\n' "$head"
evidence=docs/evidence/agents/{card_id}/COMPLETION-EVIDENCE.md
decision=docs/evidence/agents/{card_id}/REVIEW-DECISION.json
for path in "$evidence" "$decision"; do
    test -s "$path"; test -f "$path"; test ! -L "$path"
    cmp -- "$path" <(git cat-file blob "HEAD:$path")
done
changed=$(git diff --name-only "$source_head" HEAD | sort)
test "$changed" = "$(printf '%s\\n' "$evidence" "$decision" | sort)"
verdict=$(python3 -c {shlex.quote(verify)} {expected_arg} "$decision" "$evidence")
revision=$(python3 -c {shlex.quote(revision)} {card_id})
skcapstone coord show {card_id} --json |
    python3 -c {shlex.quote(check)} {expected_arg} {claim_revision} {bindings_arg}
candidate=$(python3 -c {shlex.quote(_private_evidence_stage())} {card_id} "$head" "$evidence")
cmp -- "$evidence" "$candidate"
ls -- "$candidate"
digest=$(sha256sum "$candidate" | cut -d ' ' -f1)
guarded_link() {{
    transition=$(python3 -c {shlex.quote(transition)} \\
        {card_id} {claim_revision} "$revision" "$1" "$2")
    reply=$(skcapstone coord link {card_id} "$1" "$2" --agent {actor} \\
        --expected-source-revision "$revision" --expected-claim-revision {claim_revision} \\
        --transition-id "$transition" --json)
    revision=$(printf '%s' "$reply" | python3 -c {shlex.quote(readback)} {card_id})
}}
guarded_link evidence "$candidate"
guarded_link review_evidence_sha256 "$digest"
guarded_link verdict "$verdict"
if test "$verdict" = PASS; then
    receipt=$(python3 -c {shlex.quote(receipt)} {card_id} "$source_head" {actor} "$digest")
    guarded_link applicability_receipt "$receipt"
fi
```

Stop after handoff. The controller verifies evidence independently and handles
completion. This recipe does not prove review truth or replace deterministic
external acceptance checks. Authority guarded-link support is required; missing
guards or any rejected write stops handoff, with no legacy fallback.

TITLE: {core.get('title', '')}
TASK TDD: {core.get('description', '')}
ACCEPTANCE CRITERIA:
{criteria}
"""
