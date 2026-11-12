"""Keep a card's PR link on the same forge as its repository.

skgit is the canonical forge (Chef, 2026-10-13). A card whose repository is a
skgit URL but whose ``pr`` link is a github.com URL can never be accepted: the
review publisher binds the PR repository to ``repository`` and refuses with
``card_pr_repository_mismatch`` (38119435, 2026-10-13). Refuse that pairing at
the link write path instead of discovering it at acceptance. Cards whose
repository is on GitHub are untouched.
"""

from __future__ import annotations

from urllib.parse import urlsplit

SKGIT_HOST = "skgit.skstack01.douno.it"
GITHUB_HOST = "github.com"


def _host(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return (urlsplit(value.strip()).hostname or "").lower()


def pr_forge_conflict(repository: object, pr: object) -> str | None:
    """Return a refusal message when a GitHub PR is paired with a skgit repository."""
    if _host(repository) == SKGIT_HOST and _host(pr) == GITHUB_HOST:
        return (
            f"pr link {str(pr).strip()} is a GitHub URL but the card repository "
            f"{str(repository).strip()} is on skgit, which is canonical; link the "
            "skgit pull request URL instead (card_pr_repository_mismatch)"
        )
    return None


def check_link_write(card: object | None, key: str, value: str) -> None:
    """Raise ValueError if writing ``key=value`` would leave a forge mismatch."""
    if card is None or key not in {"pr", "repository"}:
        return
    links = dict(getattr(card, "links", None) or {})
    meta = dict(getattr(card, "meta", None) or {})
    links[key] = value
    repositories = {links.get("repository"), meta.get("repository")}
    prs = {links.get("pr"), meta.get("pr")}
    for repository in repositories:
        for pr in prs:
            message = pr_forge_conflict(repository, pr)
            if message:
                raise ValueError(message)
