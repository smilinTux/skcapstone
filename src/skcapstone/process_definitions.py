"""Immutable, source-bound process definitions and instances.

Definitions are semantic derivatives of source material: every node and
requirement carries a source span and the source digest is retained.  The
registry never overwrites a source digest or a version, making replay and
contradictory variants explicit rather than silently replacing history.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProcessValidationError(ValueError):
    """A process definition cannot be safely interpreted."""


class SourceSpan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source_id: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    wording: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered(self) -> "SourceSpan":
        if self.end < self.start:
            raise ValueError("source span end precedes start")
        return self


class EvidenceRequirement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    description: str = Field(min_length=1)
    required: bool = True
    source: SourceSpan


class ProcessStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    wording: str = Field(min_length=1)
    source: SourceSpan
    capability: Optional[str] = None
    tool: Optional[str] = None
    deadline: Optional[str] = None
    required_evidence: tuple[str, ...] = ()


class ProcessBranch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    from_step: str
    to_step: str
    condition: str = Field(min_length=1)
    source: SourceSpan


class ProcessDefinition(BaseModel):
    """Versioned immutable process graph bound to exact source bytes."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: ClassVar[str] = "1"
    process_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    source_id: str
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_spans: tuple[SourceSpan, ...] = ()
    steps: tuple[ProcessStep, ...] = ()
    branches: tuple[ProcessBranch, ...] = ()
    required_evidence: tuple[EvidenceRequirement, ...] = ()
    capabilities: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    unresolved_values: tuple[str, ...] = ()
    variant_of: Optional[str] = None

    @model_validator(mode="after")
    def validate_graph(self) -> "ProcessDefinition":
        ids = {s.id for s in self.steps}
        if len(ids) != len(self.steps):
            raise ValueError("duplicate process step")
        if any(s.source.source_id != self.source_id or s.source.source_hash != self.source_hash for s in self.steps):
            raise ValueError("step source span is not bound to process source")
        if any(s.capability and s.capability not in self.capabilities for s in self.steps):
            raise ValueError("invalid capability")
        if any(s.tool and s.tool not in self.tools for s in self.steps):
            raise ValueError("invalid tool")
        if any(b.from_step not in ids or b.to_step not in ids for b in self.branches):
            raise ValueError("branch references unknown step")
        evidence = {e.key for e in self.required_evidence}
        if any(k not in evidence for s in self.steps for k in s.required_evidence):
            raise ValueError("step references unknown required evidence")
        if any(s.deadline is None and "deadline" in self.unresolved_values for s in self.steps):
            raise ValueError("unresolved deadline")
        graph = {i: [] for i in ids}
        for b in self.branches:
            graph[b.from_step].append(b.to_step)
        visiting, visited = set(), set()
        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("cyclic process graph")
            if node in visited:
                return
            visiting.add(node)
            for child in graph[node]: visit(child)
            visiting.remove(node); visited.add(node)
        for node in ids: visit(node)
        return self

    @property
    def content_hash(self) -> str:
        payload = self.model_dump_json(exclude_none=False, exclude_computed_fields=True)
        return hashlib.sha256(payload.encode()).hexdigest()


class ProcessInstance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    instance_id: str
    process_id: str
    definition_version: str
    definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    values: Mapping[str, Any] = Field(default_factory=dict)
    evidence: Mapping[str, Any] = Field(default_factory=dict)
    unresolved_values: tuple[str, ...] = ()


class ProcessRegistry:
    """Append-only registry enforcing source identity and variant visibility."""
    def __init__(self) -> None:
        self._definitions: dict[tuple[str, str], ProcessDefinition] = {}
        self._sources: dict[str, str] = {}

    def register_source(self, source_id: str, source_bytes: bytes) -> str:
        digest = hashlib.sha256(source_bytes).hexdigest()
        old = self._sources.get(source_id)
        if old and old != digest:
            raise ProcessValidationError("source mutation refused")
        self._sources[source_id] = digest
        return digest

    def register(self, definition: ProcessDefinition) -> ProcessDefinition:
        known = self._sources.get(definition.source_id)
        if known and known != definition.source_hash:
            raise ProcessValidationError("source mutation refused")
        key = (definition.process_id, definition.version)
        previous = self._definitions.get(key)
        if previous:
            if previous.content_hash != definition.content_hash:
                raise ProcessValidationError("contradictory variant for version")
            return previous
        self._definitions[key] = definition
        self._sources.setdefault(definition.source_id, definition.source_hash)
        return definition

    def get(self, process_id: str, version: str) -> ProcessDefinition:
        return self._definitions[(process_id, version)]

    def versions(self, process_id: str) -> tuple[str, ...]:
        return tuple(v for (p, v) in self._definitions if p == process_id)
