"""The release manifest: which released build of each package a node runs.

The manifest is one YAML file kept in a git repository of its own. Its git
history is the deploy history, so rolling back a release set means checking
out the previous manifest commit. This module only parses and validates it;
it never decides what is released, it reads what a merged PR said.

Example::

    schema: 1
    python: "3.12"
    packages:
      skcapstone: {source: pypi, version: "0.15.402", repo: smilinTux/skcapstone}
      skbrain:    {source: pypi, version: "1.0.1", repo: smilinTux/skbrain,
                   tag: "skbrain-v{version}"}
      skcore:     {source: git, repo: smilinTux/skcore, ref: v0.1.0, branch: master}
    requirements:
      - "uvicorn==0.30.6"
    constraints:            # pins that only apply if something pulls them in
      - "numpy==2.3.4"
    lock: []                # optional: the exact third-party set, installed --no-deps
    services:
      skchat-daemon.service:
        packages: [skchat-sovereign, skcomms]
        smoke: "skchat --version"
    smoke:
      - "skcapstone --version"
    nodes:
      default: {packages: all}
      some-host: {packages: [skcapstone, skmemory]}
    allow_exec_in_tree: []
    carry_bin: []           # hand-placed bin/ files copied from the live venv
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class PackageSpec(BaseModel):
    """One package pinned to a released build.

    Attributes:
        source: ``pypi`` installs ``name==version`` from PyPI; ``git`` installs
            the package straight from a tag (private repositories);
            ``editable`` is a DECLARED, temporary exception (a tree running
            unmerged work that must not be dropped). It is still reported as
            drift, and needs ``path`` and ``reason``.
        version: The released version (required for ``pypi``).
        repo: ``owner/name`` of the GitHub repository the release comes from.
        tag: Tag template the release was cut from; ``{version}`` is expanded.
            Pre-flight checks that this tag is on the default branch.
        ref: The tag to install from (``git`` source only).
        branch: The repository's default branch (``main`` unless stated).
        subdirectory: Package directory inside the repository, if not the root.
        imports: Modules that must import after install. Defaults to the
            distribution name with dashes turned into underscores.
    """

    source: Literal["pypi", "git", "editable"] = "pypi"
    version: Optional[str] = None
    repo: str
    tag: str = "v{version}"
    ref: Optional[str] = None
    branch: str = "main"
    subdirectory: Optional[str] = None
    imports: list[str] = Field(default_factory=list)
    path: Optional[str] = None
    reason: Optional[str] = None

    @model_validator(mode="after")
    def _source_fields(self) -> "PackageSpec":
        if self.source == "pypi" and not self.version:
            raise ValueError("a pypi package needs a version")
        if self.source == "git" and not self.ref:
            raise ValueError("a git package needs a ref (a tag on the default branch)")
        if self.source == "editable" and not (self.path and self.reason):
            raise ValueError("an editable exception needs a path and a reason")
        return self

    def release_tag(self) -> str:
        """The tag this release must exist as on the default branch."""
        if self.source == "git":
            return str(self.ref)
        return self.tag.format(version=self.version)

    def requirement(self, name: str) -> str:
        """The pip requirement line that installs exactly this release."""
        if self.source == "pypi":
            return f"{name}=={self.version}"
        if self.source == "editable":
            return f"-e {Path(str(self.path)).expanduser()}"
        url = f"git+https://github.com/{self.repo}@{self.ref}"
        if self.subdirectory:
            url += f"#subdirectory={self.subdirectory}"
        return f"{name} @ {url}"


class ServiceSpec(BaseModel):
    """A user unit and the packages whose change means it must restart."""

    packages: list[str] = Field(default_factory=list)
    smoke: Optional[str] = None


class NodeProfile(BaseModel):
    """Which packages and services a node gets. ``all`` means every one.

    ``lock`` and ``carry_bin`` override the manifest-wide ones: two nodes
    rarely have the same third-party set or the same hand-placed shims.
    """

    packages: Union[Literal["all"], list[str]] = "all"
    services: Union[Literal["all"], list[str]] = "all"
    lock: Optional[list[str]] = None
    carry_bin: Optional[list[str]] = None


class Manifest(BaseModel):
    """The whole release set."""

    schema_: int = Field(1, alias="schema")
    python: str = "3.12"
    packages: dict[str, PackageSpec]
    requirements: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    lock: list[str] = Field(default_factory=list)
    services: dict[str, ServiceSpec] = Field(default_factory=dict)
    smoke: list[str] = Field(default_factory=list)
    nodes: dict[str, NodeProfile] = Field(default_factory=lambda: {"default": NodeProfile()})
    allow_exec_in_tree: list[str] = Field(default_factory=list)
    carry_bin: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @field_validator("schema_")
    @classmethod
    def _known_schema(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"unsupported manifest schema {value}")
        return value

    @model_validator(mode="after")
    def _references_resolve(self) -> "Manifest":
        known = set(self.packages)
        for unit, spec in self.services.items():
            missing = [p for p in spec.packages if p not in known]
            if missing:
                raise ValueError(f"service {unit} names unknown package(s): {missing}")
        for node, prof in self.nodes.items():
            if prof.packages != "all":
                missing = [p for p in prof.packages if p not in known]
                if missing:
                    raise ValueError(f"node {node} names unknown package(s): {missing}")
        return self

    def profile(self, node: Optional[str] = None) -> NodeProfile:
        """The profile for *node* (this host by default), else ``default``."""
        node = node or socket.gethostname()
        return self.nodes.get(node) or self.nodes.get("default") or NodeProfile()

    def packages_for(self, node: Optional[str] = None) -> dict[str, PackageSpec]:
        """The packages this node runs."""
        prof = self.profile(node)
        if prof.packages == "all":
            return dict(self.packages)
        return {n: self.packages[n] for n in prof.packages}

    def services_for(self, node: Optional[str] = None) -> dict[str, ServiceSpec]:
        """The services this node may restart."""
        prof = self.profile(node)
        if prof.services == "all":
            return dict(self.services)
        return {n: self.services[n] for n in prof.services if n in self.services}

    def requirement_lines(self, node: Optional[str] = None) -> list[str]:
        """Every pip requirement the node's venv is built from, SK pins first.

        With a ``lock`` the third-party set is exactly the lock (installed
        ``--no-deps``), so only the SK packages differ from what was locked.
        """
        lines = [spec.requirement(name) for name, spec in self.packages_for(node).items()]
        return lines + list(self.lock_for(node) or self.requirements)

    def lock_for(self, node: Optional[str] = None) -> list[str]:
        """The third-party lock for *node* (its own, else the manifest's)."""
        prof = self.profile(node)
        return list(prof.lock if prof.lock is not None else self.lock)

    def carry_bin_for(self, node: Optional[str] = None) -> list[str]:
        """The hand-placed bin/ files to carry on *node*."""
        prof = self.profile(node)
        return list(prof.carry_bin if prof.carry_bin is not None else self.carry_bin)


def load_manifest(path: Path) -> Manifest:
    """Parse and validate a manifest file.

    Args:
        path: The manifest YAML file.

    Returns:
        The validated manifest.

    Raises:
        ValueError: The file is not a valid manifest.
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a mapping")
    return Manifest.model_validate(data)
