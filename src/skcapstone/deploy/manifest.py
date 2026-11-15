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
    resolve: strict         # refuse a set that does not resolve (default: report)
    services:
      skchat-daemon.service:
        packages: [skchat-sovereign, skcomms]
        smoke: "skchat --version"
    smoke:
      - "skcapstone --version"
    nodes:
      default: {packages: all}
      some-host: {packages: [skcapstone, skmemory]}
      agent-host: {packages: [], tools: [], services: [],
                   roles: {sidekick: [agent]}, stacks: {sidekick: v0.1.1-rc.1}}
    allow_exec_in_tree: []
    carry_bin: []           # hand-placed bin/ files copied from the live venv
    stacks:                 # public stack releases (stack.yaml at a tag), see .stack
      sidekick: {repo: smilinTux/sidekick, ref: v0.1.0}
    tools:                  # scripts units run, exported from a commit on main
      skchat-scripts:
        package: skchat-sovereign      # repo (and default ref) from the package
        ref: 4f588c8                   # optional: a commit on the default branch
        checkout: ~/clawd/skcapstone-repos/skchat   # the tree units used to run
        paths: [scripts]               # optional: export only these paths
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator


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


class ToolSpec(BaseModel):
    """Non-packaged files units execute (scripts, a small server), released.

    The tool is exported with ``git archive`` from ``ref`` (a tag or commit
    that must be on the default branch) into
    ``~/.local/share/sk-tools/<name>/<sha>`` and published as ``current``.
    Every user unit that names a path under ``checkout`` (restricted to
    ``paths`` when given) gets a managed drop-in pointing it at the export
    instead, so no unit runs from a working tree.

    Attributes:
        package: Take ``repo``, ``branch`` and the default ``ref`` from this
            manifest package (its release tag).
        repo: ``owner/name`` on GitHub, when there is no package.
        url: Any git URL (a private forge); overrides ``repo``.
        ref: Tag or commit to export. Defaults to the package release tag.
        branch: The default branch the ref must be on.
        checkout: Where the working tree sits on nodes (``~`` allowed). A list
            when nodes keep it in different places.
        paths: Repository paths to export and rewrite; empty means all.
        reason: Why this is a tool export and not a package entry point.
        setup: Optional argv run once inside the finished export, at its final
            path, before it is published (``~`` expanded). For a tool that
            provisions itself (dependencies, a generated launcher) from its
            own tree. A failure removes the export and publishes nothing.
        setup_env: Extra environment for ``setup`` (``~`` expanded).
        setup_timeout: Seconds ``setup`` may run.
    """

    package: Optional[str] = None
    repo: Optional[str] = None
    url: Optional[str] = None
    ref: Optional[str] = None
    branch: Optional[str] = None
    checkout: Union[str, list[str]] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    reason: Optional[str] = None
    setup: list[str] = Field(default_factory=list)
    setup_env: dict[str, str] = Field(default_factory=dict)
    setup_timeout: int = 1800

    @model_validator(mode="after")
    def _has_source(self) -> "ToolSpec":
        if not (self.package or self.repo or self.url):
            raise ValueError("a tool needs a package, repo or url")
        if not self.package and not self.ref:
            raise ValueError("a tool without a package needs a ref")
        if any(p.startswith("/") or ".." in Path(p).parts for p in self.paths):
            raise ValueError("tool paths are relative to the repository root")
        return self

    @property
    def checkouts(self) -> list[str]:
        """Every checkout location, as a list (empty for a tool with none)."""
        if isinstance(self.checkout, str):
            return [self.checkout] if self.checkout else []
        return list(self.checkout)


class ToolConfigSpec(BaseModel):
    """A per-cluster config repository for a tool, pinned per node.

    The framework is the tool (public, one pinned release for every node);
    the config is a private repository per cluster holding one directory per
    instance. Both are exported side by side and the instance's units are
    rendered from the config, so a node runs exactly
    ``(framework ref, config ref)`` and nothing from a working tree.

    The export holds only ``<instance>/``, at
    ``~/.local/share/sk-tools/<tool>-config/<sha12>/<instance>``. Every file
    in ``<instance>/systemd/`` (``*.service``, ``*.timer``) is installed as a
    user unit of the same name, with ``@TOOL@`` replaced by the tool's
    ``current`` export and ``@CONFIG@`` by ``<tool>-config/current/<instance>``.
    Those units are owned by the deploy: their hand-written drop-ins are moved
    aside, so the rendered file is the whole effective unit.

    Attributes:
        repo: ``owner/name`` on GitHub.
        url: Any git URL (a private forge); overrides ``repo``.
        ref: Commit or tag to export; must be on ``branch``.
        branch: The default branch.
        instance: The instance directory to export and render, relative to the
            repo root (``node1`` or ``render/chiap04``); no ``..``.
    """

    repo: Optional[str] = None
    url: Optional[str] = None
    ref: str
    branch: str = "main"
    instance: str

    @model_validator(mode="after")
    def _has_source(self) -> "ToolConfigSpec":
        if not (self.repo or self.url):
            raise ValueError("a tool config needs a repo or url")
        parts = Path(self.instance).parts
        if (
            not self.instance
            or self.instance.startswith("/")
            or not parts
            or any(p in ("", ".", "..") for p in parts)
        ):
            raise ValueError(
                "instance is one directory inside the config repo (no '..', not absolute)"
            )
        return self

    def source(self) -> tuple[str, str, str]:
        """``(git url, ref, default branch)``."""
        return self.url or f"https://github.com/{self.repo}.git", self.ref, self.branch


class ServiceSpec(BaseModel):
    """A user unit and the packages whose change means it must restart."""

    packages: list[str] = Field(default_factory=list)
    smoke: Optional[str] = None


class StackRef(BaseModel):
    """A public stack release a cluster manifest includes (``stacks.<name>``).

    The stack repository holds ``stack.yaml`` (tools, services and roles, see
    :mod:`.stack`) at a tag on its default branch. Nodes take parts of it only
    through ``nodes.<node>.roles``.

    Attributes:
        repo: ``owner/name`` on GitHub (cloned over HTTPS).
        url: Any git URL; overrides ``repo``.
        ref: The release tag (or a commit) on ``branch``.
        branch: The stack repository's default branch.
    """

    repo: Optional[str] = None
    url: Optional[str] = None
    ref: str
    branch: str = "main"

    @model_validator(mode="after")
    def _has_source(self) -> "StackRef":
        if not (self.repo or self.url):
            raise ValueError("a stack needs a repo or url")
        return self

    def git_url(self) -> str:
        """The URL git clones the stack repository from."""
        return self.url or f"https://github.com/{self.repo}.git"


class NodeProfile(BaseModel):
    """Which packages and services a node gets. ``all`` means every one.

    ``lock`` and ``carry_bin`` override the manifest-wide ones: two nodes
    rarely have the same third-party set or the same hand-placed shims.

    ``roles`` (stack name to role names) adds those roles' tools and services
    from the stack to the node; it is the only way a node gets a stack's
    parts. ``stacks`` (stack name to ref) pins this node alone to another
    ref of a stack, for a canary.
    """

    packages: Union[Literal["all"], list[str]] = "all"
    services: Union[Literal["all"], list[str]] = "all"
    lock: Optional[list[str]] = None
    carry_bin: Optional[list[str]] = None
    tools: Union[Literal["all"], list[str]] = "all"
    configs: dict[str, ToolConfigSpec] = Field(default_factory=dict)
    roles: dict[str, list[str]] = Field(default_factory=dict)
    stacks: dict[str, str] = Field(default_factory=dict)


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
    tools: dict[str, ToolSpec] = Field(default_factory=dict)
    stacks: dict[str, StackRef] = Field(default_factory=dict)
    #: ``strict``: ``deploy plan`` and ``deploy apply`` refuse a node set whose
    #: pinned PyPI packages and lock do not resolve together (the conflict
    #: ``pip check`` would report after the build). ``report``: they print the
    #: conflict and go on; for a manifest whose lock still carries known
    #: conflicts (verify then refuses only NEW ``pip check`` problems).
    resolve: Literal["strict", "report"] = "report"

    model_config = {"populate_by_name": True}

    @field_validator("schema_")
    @classmethod
    def _known_schema(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"unsupported manifest schema {value}")
        return value

    @model_validator(mode="after")
    def _references_resolve(self, info: ValidationInfo) -> "Manifest":
        # Before stacks are merged a node's role tools are not in its list yet:
        # its configs are checked once they are (the merged manifest is
        # validated again without this context).
        stacks_pending = bool((info.context or {}).get("stacks_pending"))
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
            if prof.tools != "all":
                missing = [t for t in prof.tools if t not in self.tools]
                if missing:
                    stack_tools = set((info.context or {}).get("stack_tools") or ())
                    stack_tools |= {s + "-release" for s in self.stacks}
                    hint = (
                        " (stack tools come only through roles)"
                        if any(t in stack_tools for t in missing)
                        else ""
                    )
                    raise ValueError(f"node {node} names unknown tool(s): {missing}{hint}")
            node_tools = set(self.tools) if prof.tools == "all" else set(prof.tools)
            stray = [t for t in prof.configs if t not in node_tools]
            if stray and not (stacks_pending and prof.roles):
                raise ValueError(f"node {node} has a config for tool(s) it does not run: {stray}")
            for what, names in (("roles", prof.roles), ("stacks", prof.stacks)):
                missing = [s for s in names if s not in self.stacks]
                if missing:
                    raise ValueError(f"node {node} {what} name unknown stack(s): {missing}")
        for name, tool in self.tools.items():
            if tool.package and tool.package not in known:
                raise ValueError(f"tool {name} names unknown package {tool.package}")
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

    def tools_for(self, node: Optional[str] = None) -> dict[str, ToolSpec]:
        """The tool exports this node gets."""
        prof = self.profile(node)
        if prof.tools == "all":
            return dict(self.tools)
        return {n: self.tools[n] for n in prof.tools}

    def configs_for(self, node: Optional[str] = None) -> dict[str, ToolConfigSpec]:
        """The per-cluster config repos this node exports, keyed by tool."""
        return dict(self.profile(node).configs)

    def tool_source(self, name: str) -> tuple[str, str, str]:
        """``(git url, ref, default branch)`` for tool *name*."""
        tool = self.tools[name]
        pkg = self.packages.get(tool.package) if tool.package else None
        repo = tool.repo or (pkg.repo if pkg else None)
        url = tool.url or f"https://github.com/{repo}.git"
        ref = tool.ref or (pkg.release_tag() if pkg else "")
        branch = tool.branch or (pkg.branch if pkg else "main")
        return url, ref, branch


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
