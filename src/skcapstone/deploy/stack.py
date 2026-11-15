"""Public stacks a cluster manifest includes, and the roles nodes take from them.

A *stack* is a public repository with ``stack.yaml`` at a release tag on its
default branch. It brings tools (the :class:`~.manifest.ToolSpec` shape),
services (:class:`~.manifest.ServiceSpec`, in restart order) and roles that
group them::

    stack: sidekick
    version: 0.1.0
    config_schema: 1
    tools:
      hermes: {repo: smilinTux/hermes-skworld, ref: v1.4.0,
               setup: ["@STACK:sidekick@/bin/setup-hermes"]}
      skcall: {repo: smilinTux/skcall, ref: v0.3.0}
    services:                       # restart order
      skcall.service: {smoke: "@STACK:sidekick@/bin/check skcall"}
      hermes-gateway.service: {}
    roles:
      agent: {tools: [hermes, skcall], services: [skcall.service, hermes-gateway.service]}

A cluster manifest names it under ``stacks:`` and gives nodes roles from it
(``nodes.<node>.roles: {sidekick: [agent]}``). :func:`merge_stacks` folds the
stack into the manifest:

* every stack tool and service joins the manifest, and one the cluster (or
  another stack) also defines is refused, naming both;
* the stack repository itself is the tool ``<stack>-release``;
* a node gets a stack's tools and services only through its roles, plus
  ``<stack>-release``. ``tools: all`` and ``services: all`` keep meaning every
  part the CLUSTER defines, so a stack never lands on a node by accident;
* ``@STACK:<name>@`` in smoke commands and setup argv becomes that stack's
  ``<name>-release`` export.

``nodes.<node>.stacks.<stack>`` pins one node to another ref (a canary), so the
merged manifest is per node: see ``resolve_manifest(..., node=...)``.
"""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, model_validator

from .manifest import Manifest, ServiceSpec, StackRef, ToolSpec
from .tools import (
    STACK_RELEASE_SUFFIX,
    Runner,
    ToolError,
    _err,
    _git,
    _text,
    default_root,
    expand_stack_refs,
    ref_on_branch,
    sync_mirror,
)

#: The file a stack repository carries at its root.
STACK_FILE = "stack.yaml"
_CLUSTER = "the cluster manifest"


class RoleSpec(BaseModel):
    """A named set of a stack's tools and services a node can take."""

    tools: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)


class StackFile(BaseModel):
    """``stack.yaml``: what one stack release brings."""

    stack: str
    version: str
    config_schema: int
    tools: dict[str, ToolSpec] = Field(default_factory=dict)
    services: dict[str, ServiceSpec] = Field(default_factory=dict)
    roles: dict[str, RoleSpec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _roles_resolve(self) -> "StackFile":
        for role, spec in self.roles.items():
            missing = [t for t in spec.tools if t not in self.tools]
            if missing:
                raise ValueError(f"role {role} names unknown tool(s): {missing}")
            missing = [s for s in spec.services if s not in self.services]
            if missing:
                raise ValueError(f"role {role} names unknown service(s): {missing}")
        return self


def load_stack(text: str, where: str = STACK_FILE) -> StackFile:
    """Parse and validate the text of a ``stack.yaml``.

    Raises:
        ValueError: It is not a valid stack file.
    """
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{where} is not a mapping")
    return StackFile.model_validate(data)


def fetch_stack(
    name: str,
    ref: StackRef,
    at: str,
    mirrors: Path,
    fetch: bool = True,
    runner: Runner = subprocess.run,
) -> StackFile:
    """Read ``stack.yaml`` of stack *name* at *at*, which must be on its main.

    The repository is kept as a bare mirror at ``mirrors/<name>`` (fetched
    first unless *fetch* is off), the same way tool exports keep theirs.

    Raises:
        ToolError: The mirror is missing or could not be fetched, *at* does not
            resolve or is not on the stack's default branch, or the stack file
            is missing or invalid.
    """
    label = f"stack {name}"
    url = ref.git_url()
    mirror = mirrors / name
    if fetch:
        sync_mirror(label, url, mirror, runner)
    elif not mirror.is_dir():
        raise ToolError(f"{label}: {mirror} not mirrored yet")
    sha = ref_on_branch(label, url, at, ref.branch, mirror, runner, what="stacks")
    proc = _git(["show", f"{sha}:{STACK_FILE}"], runner, cwd=mirror, text=True)
    if proc.returncode != 0:
        raise ToolError(f"{label}: no {STACK_FILE} at {at} ({sha[:12]}): {_err(proc)}")
    try:
        stack = load_stack(_text(proc), f"{label} {STACK_FILE} at {at}")
    except ValueError as exc:
        raise ToolError(f"{label}: {STACK_FILE} at {at} is invalid: {exc}") from exc
    if stack.stack != name:
        raise ToolError(f"{label}: {STACK_FILE} at {at} says it is stack {stack.stack}")
    return stack


def _expand(text: str, stacks, root: str, where: str) -> str:
    try:
        return expand_stack_refs(text, stacks, root, where)
    except ToolError as exc:
        raise ValueError(str(exc)) from exc


def merge_stacks(
    manifest_data: dict,
    files: dict[str, StackFile],
    tools_root: Optional[Path] = None,
) -> Manifest:
    """The manifest with each stack's tools, services and roles folded in.

    Args:
        manifest_data: The cluster manifest as parsed YAML (not modified).
            ``stacks.<name>.ref`` is the ref the stack files were read at.
        files: The stack file of every stack the manifest names.
        tools_root: Where tool exports live (``@STACK:<name>@`` expands under
            it); ``~/.local/share/sk-tools`` by default.

    Raises:
        ValueError: A tool or service is defined twice, a node names a role
            its stack does not have, or ``@STACK:<name>@`` names no stack.
    """
    data = copy.deepcopy(manifest_data)
    base = Manifest.model_validate(data)
    missing = [s for s in base.stacks if s not in files]
    if missing:
        raise ValueError(f"no stack file for stack(s): {missing}")
    root = str(tools_root or default_root())
    tools: dict = dict(data.get("tools") or {})
    services: dict = dict(data.get("services") or {})
    cluster_tools, cluster_services = list(tools), list(services)
    tool_from = {t: _CLUSTER for t in tools}
    service_from = {s: _CLUSTER for s in services}
    for name, stack in files.items():
        sref = base.stacks[name]
        source = f"stack {name} ({stack.version})"
        parts = [(name + STACK_RELEASE_SUFFIX, None)] + list(stack.tools.items())
        for tool, spec in parts:
            if tool in tool_from:
                raise ValueError(f"tool {tool} is defined by {tool_from[tool]} and by {source}")
            tool_from[tool] = source
            if spec is None:
                tools[tool] = {
                    "url": sref.git_url(),
                    "ref": sref.ref,
                    "branch": sref.branch,
                    "reason": f"{source}: the stack repository itself",
                }
            else:
                tools[tool] = spec.model_dump(exclude_unset=True)
        for unit, svc in stack.services.items():
            if unit in service_from:
                raise ValueError(
                    f"service {unit} is defined by {service_from[unit]} and by {source}"
                )
            service_from[unit] = source
            services[unit] = svc.model_dump(exclude_unset=True)
    names = set(files)
    for tool, spec in tools.items():
        if spec.get("setup"):
            spec["setup"] = [_expand(a, names, root, f"tool {tool} setup") for a in spec["setup"]]
        if spec.get("setup_env"):
            spec["setup_env"] = {
                k: _expand(v, names, root, f"tool {tool} setup_env")
                for k, v in spec["setup_env"].items()
            }
    for unit, spec in services.items():
        if spec and spec.get("smoke"):
            spec["smoke"] = _expand(spec["smoke"], names, root, f"service {unit} smoke")
    data["smoke"] = [_expand(c, names, root, "smoke") for c in data.get("smoke") or []]
    data["tools"], data["services"] = tools, services

    nodes = data.get("nodes")
    if nodes is None:
        nodes = data["nodes"] = {"default": {}}
    for node, prof in nodes.items():
        prof = nodes[node] = dict(prof or {})
        want_tools = prof.get("tools", "all")
        want_tools = list(cluster_tools if want_tools == "all" else want_tools)
        want_services = prof.get("services", "all")
        want_services = list(cluster_services if want_services == "all" else want_services)
        for name, roles in (prof.get("roles") or {}).items():
            stack = files[name]
            role_tools = {name + STACK_RELEASE_SUFFIX}
            role_services: set[str] = set()
            for role in roles:
                if role not in stack.roles:
                    raise ValueError(
                        f"node {node}: role {role} is not in stack {name} ({stack.version});"
                        f" it has {sorted(stack.roles)}"
                    )
                role_tools.update(stack.roles[role].tools)
                role_services.update(stack.roles[role].services)
            order = [name + STACK_RELEASE_SUFFIX, *stack.tools]
            want_tools += [t for t in order if t in role_tools and t not in want_tools]
            want_services += [
                s for s in stack.services if s in role_services and s not in want_services
            ]
        prof["tools"], prof["services"] = want_tools, want_services
    return Manifest.model_validate(data)
