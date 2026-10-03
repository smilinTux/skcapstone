"""Report models and private-material classes for the sync policy audit.

Every class of private material is declared three ways at once: the globs
that find it already sitting under a folder root, the probe path evaluated
against the root's .stignore for the could-land question, and the exact
ignore line that would cover the probe. Keeping the three side by side in
one frozen record is what makes the remediation output trustworthy: the
line printed is the line whose absence was just proven.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

Severity = Literal["error", "warn", "info", "ok"]


class _Frozen(BaseModel):
    """Immutable base for audit report values."""

    model_config = ConfigDict(frozen=True)


class MaterialClass(_Frozen):
    """One class of private material the audit hunts for.

    Attributes:
        name: Stable category name used in findings.
        globs: Syncthing-syntax patterns that identify present material.
        probes: (probe path, covering ignore line) pairs. The probe is a
            synthetic relative path evaluated against the folder root's
            .stignore; the line is exactly what remediation would add.
    """

    name: str
    globs: tuple[str, ...]
    probes: tuple[tuple[str, str], ...]


#: The private-material classes from the card, in report order. The probe
#: paths live under a ``policy-probe`` directory that cannot collide with
#: real content, except the keyring and token-store probes, which must sit
#: at their real locations because location IS the risk.
MATERIAL_CLASSES: tuple[MaterialClass, ...] = (
    MaterialClass(
        name="private_key",
        globs=("**/*.key", "**/*.pem", "**/private.*"),
        probes=(
            ("policy-probe/agent.key", "*.key"),
            ("policy-probe/agent.pem", "*.pem"),
            ("policy-probe/identity/private.asc", "**/private.*"),
        ),
    ),
    MaterialClass(
        name="revocation_cert",
        globs=("**/*.rev", "**/revocation*"),
        probes=(
            ("policy-probe/revoke.rev", "*.rev"),
            ("policy-probe/revocation.crt", "**/revocation*"),
        ),
    ),
    MaterialClass(
        name="passphrase",
        globs=("**/passphrase*", "**/*.pass"),
        probes=(
            ("policy-probe/passphrase.txt", "**/passphrase*"),
            ("policy-probe/secret.pass", "*.pass"),
        ),
    ),
    MaterialClass(
        name="secret_keyring",
        globs=(".gnupg/secring.gpg", ".gnupg/private-keys-v1.d/*", "**/*.kbx"),
        probes=(
            (".gnupg/secring.gpg", ".gnupg/secring.gpg"),
            (".gnupg/private-keys-v1.d/probe.key", ".gnupg/private-keys-v1.d"),
            ("policy-probe/ring.kbx", "**/*.kbx"),
        ),
    ),
    MaterialClass(
        name="token_store",
        globs=("security/tokens/*", "capauth/security/tokens/*"),
        probes=(
            ("security/tokens/probe.token", "security/tokens"),
            ("capauth/security/tokens/probe.token", "capauth/security/tokens"),
        ),
    ),
)


class Finding(_Frozen):
    """One audit observation, worst-first sortable.

    Attributes:
        severity: error fails the audit; warn and info never do.
        category: Stable machine-readable finding kind.
        path: File or directory the finding is about, when one exists.
        detail: Human-readable explanation.
        remediation: Exact .stignore lines that would resolve the finding.
    """

    severity: Severity
    category: str
    path: str = ""
    detail: str = ""
    remediation: tuple[str, ...] = ()


class FolderReport(_Frozen):
    """Audit result for one discovered Syncthing folder root."""

    folder_id: str
    path: str
    present_on_host: bool
    stignore_present: bool
    findings: tuple[Finding, ...] = ()

    @computed_field
    @property
    def severity(self) -> Severity:
        """Worst finding severity; a held folder with no findings is ok."""
        return _worst(self.findings) if self.findings else "ok"

    @computed_field
    @property
    def remediation_lines(self) -> tuple[str, ...]:
        """Ordered, deduplicated ignore lines this folder would gain."""
        out: list[str] = []
        for finding in self.findings:
            for line in finding.remediation:
                if line not in out:
                    out.append(line)
        return tuple(out)


class SyncPolicyReport(_Frozen):
    """The whole audit: every folder plus host-level findings."""

    folders: tuple[FolderReport, ...] = ()
    findings: tuple[Finding, ...] = ()
    dry_run: bool = True
    applied: tuple[str, ...] = Field(default_factory=tuple)

    @computed_field
    @property
    def severity(self) -> Severity:
        """Worst severity across folders and host findings."""
        return _worst(
            tuple(self.findings)
            + tuple(finding for folder in self.folders for finding in folder.findings)
        )

    @computed_field
    @property
    def ok(self) -> bool:
        """True only when nothing error-grade was found anywhere."""
        return self.severity != "error"


def _worst(findings: tuple[Finding, ...]) -> Severity:
    """The worst severity in a finding tuple, error first."""
    order: tuple[Severity, ...] = ("error", "warn", "info")
    for level in order:
        if any(finding.severity == level for finding in findings):
            return level
    return "ok"
