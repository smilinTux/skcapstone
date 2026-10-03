"""Validate committed native source-review proposals without trusting model claims."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from .source_bundle import _INSPECT_SETUP, SourceBundleError, _inspect

SCHEMA = "skfleet.source-review-decision/v1"


class ReviewEvidenceError(ValueError):
    """Review or original source evidence is absent, changed, or unsafe."""


_REVIEW_PROGRAM = _INSPECT_SETUP + r"""
# Existing sandbox argument slots carry review card, parent, source head/tree,
# and exact reviewer identity; the evidence commit is discovered independently.
parent, owner = base, ref
review_head=git('rev-parse','HEAD^{commit}').decode().strip()
review_tree=git('rev-parse','HEAD^{tree}').decode().strip()
assert review_head != head
assert git('rev-parse',head+'^{tree}').decode().strip()==tree
git('merge-base','--is-ancestor',head,review_head)
prefix='docs/evidence/agents/'+card+'/'
names={prefix+'COMPLETION-EVIDENCE.md',prefix+'REVIEW-DECISION.json'}
assert set(git('diff','--name-only',head,review_head).decode().splitlines())==names
def read(name):
    path=root
    for part in name.split('/'):
        path=path/part
        assert not path.is_symlink()
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as stream:
        info=os.fstat(stream.fileno())
        assert stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and not info.st_mode&0o022
        raw=stream.read(262145)
    assert 0<len(raw)<=262144
    assert git('cat-file','blob',review_head+':'+name)==raw
    return raw
report=read(prefix+'COMPLETION-EVIDENCE.md')
raw=read(prefix+'REVIEW-DECISION.json')
def unique(pairs):
    value={}
    for key,item in pairs:
        assert key not in value
        value[key]=item
    return value
proposal=json.loads(raw,object_pairs_hook=unique)
assert isinstance(proposal,dict)
assert set(proposal)=={'schema','card','parent_card','source_head','source_tree',
                       'reviewer_identity','verdict','report_sha256'}
assert proposal['schema']=='skfleet.source-review-decision/v1'
assert proposal['card']==card and proposal['parent_card']==parent
assert proposal['source_head']==head and proposal['source_tree']==tree
assert proposal['reviewer_identity']==owner
assert proposal['verdict'] in ('PASS','FAIL','BLOCKED')
assert proposal['report_sha256']==hashlib.sha256(report).hexdigest()
assert not git('status','--porcelain','--untracked-files=all')
assert git('rev-parse','HEAD').decode().strip()==review_head
print(json.dumps({'proposal':proposal,'review_head':review_head,'review_tree':review_tree,
    'report_sha256':hashlib.sha256(report).hexdigest(),
    'decision_sha256':hashlib.sha256(raw).hexdigest(),
    'report_b64':base64.b64encode(report).decode(),
    'decision_b64':base64.b64encode(raw).decode()}))
"""


def inspect_proposal(
    workspace: Path,
    *,
    card: str,
    parent_card: str,
    source_head: str,
    source_tree: str,
    reviewer_identity: str,
) -> dict:
    """Inspect exact committed review bytes in the existing no-network Git sandbox."""
    if (
        not all(re.fullmatch(r"[0-9a-f]{8}", value) for value in (card, parent_card))
        or card == parent_card
        or not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (source_head, source_tree))
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", reviewer_identity)
    ):
        raise ReviewEvidenceError("review proposal identity invalid")
    try:
        result = _inspect(
            workspace,
            _REVIEW_PROGRAM,
            card,
            parent_card,
            source_head,
            source_tree,
            reviewer_identity,
        )
        # The only artifact bytes returned by trusted inspection are the two
        # bounded committed files; they will be preserved outside worker source.
        for key in ("report", "decision"):
            raw = base64.b64decode(result[key + "_b64"], validate=True)
            if hashlib.sha256(raw).hexdigest() != result[key + "_sha256"]:
                raise ReviewEvidenceError("review inspection digest changed")
        return result
    except (OSError, ValueError, KeyError, TypeError, SourceBundleError) as exc:
        raise ReviewEvidenceError("committed review proposal refused") from exc
