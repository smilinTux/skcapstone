"""Pure drift diff: a node's live .stignore against its sync folder's ruleset.

Epic 3bbf39ea, card 20a1d4d3 (second half). Sibling of profile_doctor.py and
built to the same contract: one frozen result, no side effects, REPORT ONLY.
There is no actuation verb here and no way to reach one, so a bug produces a
wrong FINDING, never a wrong CHANGE.

Why this check has to exist at all
----------------------------------
``~/.skcapstone/.stignore`` opens with ``*.key``, ``*.pem`` and
``**/private.*``. Those three lines are the ONLY reason the control node can
hold eleven ``agents/*/capauth/identity/private.asc`` files while a peer in
the same ``sendreceive`` folder holds zero: Syncthing does not scan or
announce an ignored file, so the source never offers it to anyone. The
control is strong where it is enforced. What was weak is custody of the RULE.

``syncthing_setup._write_stignore`` can no longer strip a rule (it unions,
never overwrites), but that only closes ONE way to lose the ruleset. A hand
edit, a restore from an old backup, a fresh agent home built by something
that never ran the installer: all of those still produce a node inside a
sovereign folder with nothing stopping it from announcing private keys. This
module is what notices.

Why the ruleset is keyed by FOLDER ID, not by role
--------------------------------------------------
``spec.syncFolders`` on the Profile kind is ROLE-keyed, so a ruleset living
there would be per-role, and two roles joining one folder could disagree
about what must never leave a node. The no-secrets invariant would become
per-node, which is the exact drift this card exists to stop. Every node in a
given folder needs a byte-identical answer, so the ruleset belongs to the
FOLDER definition and ``syncFolders`` merely REFERENCES it by id.

A ``syncfolder`` fleet object may extend a built-in ruleset. It may only ADD
patterns, never drop one, for the same reason ``_write_stignore`` may only
add: forgetting to ignore something leaks it, and a declarative object is
exactly as capable of a typo as a hand edit is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

#: Rules whose absence means secret material can be announced to peers.
#: Missing any of these is an `error`: it is not "this node is mid-install",
#: it is "this node has nothing stopping it from replicating a private key".
_SOVEREIGN_SECRET_RULES = (
    "*.key",
    "*.pem",
    "**/private.*",
)

#: Rules that cover narrower secret and credential material. Missing one is a
#: `warn`, not an `error`: each protects a specific subsystem that may simply
#: not be installed on this node, so grading it `error` would train people to
#: ignore the report, which is how the first ruleset went missing unnoticed.
_SOVEREIGN_CREDENTIAL_RULES = (
    "**/telegram.session",
    "skcomms/cot-pki/*.key",
    "skcomms/cot-pki/devices",
    "skcomms/cot-pki/packages",
    "capauth/security/tokens",
)


@dataclass(frozen=True)
class SyncFolderRuleset:
    """What every node joining one Syncthing folder must ignore.

    Attributes:
        folder_id: The Syncthing folder id, e.g. ``skcapstone-sync``. This is
            the key, deliberately: it is the only identifier every member of
            the folder agrees on.
        root: Folder root as a path string, ``~`` allowed. The check is
            skipped when this path is absent, because a node that does not
            hold the folder cannot leak through it.
        required: Patterns whose absence grades `error`.
        recommended: Patterns whose absence grades `warn`.
    """

    folder_id: str
    root: str
    required: tuple[str, ...] = ()
    recommended: tuple[str, ...] = ()


#: Built-in folder definitions. These are the floor, not the ceiling: a
#: `syncfolder` fleet object of the same name may add to them.
DEFAULT_RULESETS: dict[str, SyncFolderRuleset] = {
    "skcapstone-sync": SyncFolderRuleset(
        folder_id="skcapstone-sync",
        root="~/.skcapstone",
        required=_SOVEREIGN_SECRET_RULES,
        recommended=_SOVEREIGN_CREDENTIAL_RULES,
    ),
}


def pattern_lines(text: str) -> list[str]:
    """The actual ignore patterns in a .stignore, without comments or blanks.

    Args:
        text: Raw .stignore contents.

    Returns:
        Stripped pattern lines in file order.
    """
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("//"):
            out.append(line)
    return out


def ruleset_from_spec(folder_id: str, spec: dict | None) -> SyncFolderRuleset:
    """Resolve a folder's ruleset: the built-in floor, plus any object extras.

    A ``syncfolder`` object can add patterns and set the root. It cannot
    remove a built-in pattern, so no declared object can ever weaken the
    no-secrets invariant below what the code ships with.

    Args:
        folder_id: Syncthing folder id.
        spec: ``spec`` block of a ``syncfolder`` object, or None.

    Returns:
        The merged ruleset. Unknown folder ids with no spec resolve to an
        empty ruleset rooted nowhere, which the checker skips.
    """
    base = DEFAULT_RULESETS.get(folder_id) or SyncFolderRuleset(folder_id=folder_id, root="")
    if not spec:
        return base

    def _union(existing: tuple[str, ...], key: str) -> tuple[str, ...]:
        extra = [p for p in (spec.get(key) or []) if isinstance(p, str)]
        return existing + tuple(p for p in extra if p not in existing)

    return SyncFolderRuleset(
        folder_id=folder_id,
        root=spec.get("root") or base.root,
        required=_union(base.required, "requiredIgnores"),
        recommended=_union(base.recommended, "recommendedIgnores"),
    )


@dataclass(frozen=True)
class StignoreReport:
    """One folder's ruleset drift on one node.

    Every list is sorted, so two reports over unchanged inputs compare equal
    and can be diffed across runs.
    """

    folder_id: str
    root: str
    #: False when the folder root holds no readable .stignore at all.
    present: bool = False
    missing_required: list[str] = field(default_factory=list)
    missing_recommended: list[str] = field(default_factory=list)

    @property
    def severity(self) -> str:
        """`error`, `warn` or `ok`, worst finding wins."""
        if not self.present or self.missing_required:
            return "error"
        if self.missing_recommended:
            return "warn"
        return "ok"

    @property
    def clean(self) -> bool:
        """True when the file exists and every required pattern is present."""
        return self.severity == "ok"

    def as_dict(self) -> dict:
        """Machine-readable form, severity included."""
        return {
            "folder": self.folder_id,
            "root": self.root,
            "present": self.present,
            "missing_required": list(self.missing_required),
            "missing_recommended": list(self.missing_recommended),
            "severity": self.severity,
        }

    def findings(self) -> list[tuple[str, str, str]]:
        """Flat (grade, category, name) rows, sorted, for table rendering."""
        rows: list[tuple[str, str, str]] = []
        if not self.present:
            rows.append(("error", "no_stignore", self.root or self.folder_id))
        for name in self.missing_required:
            rows.append(("error", "missing_required_ignore", name))
        for name in self.missing_recommended:
            rows.append(("warn", "missing_recommended_ignore", name))
        return rows


def check_text(ruleset: SyncFolderRuleset, text: str) -> StignoreReport:
    """Diff one .stignore's contents against a folder ruleset.

    Args:
        ruleset: The folder's required and recommended patterns.
        text: Raw .stignore contents.

    Returns:
        A StignoreReport. Matching is exact on the stripped pattern line: a
        near-miss like ``*.keys`` is a miss, because Syncthing would treat it
        as one too and a fuzzy match here would report safety that does not
        exist.
    """
    have = set(pattern_lines(text))
    return StignoreReport(
        folder_id=ruleset.folder_id,
        root=ruleset.root,
        present=True,
        missing_required=sorted(p for p in ruleset.required if p not in have),
        missing_recommended=sorted(p for p in ruleset.recommended if p not in have),
    )


def check_folder(ruleset: SyncFolderRuleset, root: Path | None = None) -> StignoreReport | None:
    """Check the .stignore at a folder root on this host.

    Args:
        ruleset: The folder's ruleset.
        root: Override for the folder root, for tests. Defaults to the
            ruleset's own root with ``~`` expanded.

    Returns:
        A StignoreReport, or None when this node does not hold the folder at
        all. None and a clean report are DIFFERENT answers and must not be
        conflated: "the folder is not here" is not "the folder is safe".

    An unreadable .stignore reports as absent. That is the safe direction:
    rules we cannot read are rules we cannot vouch for.
    """
    if root is None:
        if not ruleset.root:
            return None
        root = Path(ruleset.root).expanduser()
    if not root.is_dir():
        return None

    path = root / ".stignore"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return StignoreReport(folder_id=ruleset.folder_id, root=str(root), present=False)
    report = check_text(ruleset, text)
    return StignoreReport(
        folder_id=report.folder_id,
        root=str(root),
        present=True,
        missing_required=report.missing_required,
        missing_recommended=report.missing_recommended,
    )
