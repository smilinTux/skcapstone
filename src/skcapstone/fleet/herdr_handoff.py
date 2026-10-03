"""Durable local Herdr delivery evidence, never card ownership or acceptance."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path

from skcoord.card_store import explicit_creation_request_digest

from ..atomic_io import atomic_write_text
from . import store
from .herdr_handoff_validation import (
    _IDENTITY,
    _PACKET,
    _RECEIPT,
    _ROUTES,
    _TARGET,
    HandoffError,
    _bounded_copy,
    _git,
    _identifier,
    _pairs,
    _regular,
    _text,
    authority,
    load_json,
)

__all__ = ["HandoffError", "HandoffManager", "load_json"]


class HandoffManager:
    """Serialize local delivery and evidence while leaving card state unchanged."""

    def __init__(self, paths, coordination_home, actor, transport=None):
        if transport is None:
            from .herdr_transport import HerdrTransport

            transport = HerdrTransport()
        self.paths, self.home, self.actor = paths, Path(coordination_home), _text(actor, "actor")
        self.transport = transport
        self.directory = paths.root / "herdr-handoffs"

    @contextmanager
    def _locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.resolve() != self.directory.absolute():
            raise HandoffError("unsafe handoff directory")
        fd = os.open(self.directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise HandoffError("unsafe handoff lock")
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _path(self, assignment):
        return self.directory / f"{_identifier(assignment)}.json"

    def _read(self, assignment):
        try:
            row = json.loads(_regular(self._path(assignment), 262144), object_pairs_hook=_pairs)
            if row["schema"] != 1 or row["packet"]["assignment_id"] != assignment:
                raise HandoffError("invalid assignment journal")
            return row
        except (OSError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise HandoffError("assignment journal unavailable") from exc

    def _write(self, row):
        path = self._path(row["packet"]["assignment_id"])
        if path.is_symlink():
            raise HandoffError("symlink journal refused")
        atomic_write_text(path, json.dumps(row, sort_keys=True, indent=2) + "\n")
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _packet(self, packet):
        if not isinstance(packet, dict) or set(packet) != _PACKET:
            raise HandoffError("invalid assignment packet fields")
        packet = _bounded_copy(packet)
        _identifier(packet["assignment_id"])
        if not re.fullmatch(r"[0-9a-f]{8}", _text(packet["helper_id"], "helper_id")):
            raise HandoffError("invalid helper_id")
        _text(packet["claim_revision"], "claim_revision")
        _text(packet["expected_output"], "expected_output", 8192)
        target = packet["target"]
        if not isinstance(target, dict) or set(target) != _TARGET:
            raise HandoffError("invalid target fields")
        for key in _TARGET:
            _text(target[key], key)
        if packet["route"] not in _ROUTES:
            raise HandoffError("route must be an approved logical size route")
        workspace = Path(target["cwd"])
        if not workspace.is_absolute() or workspace.resolve(strict=True) != workspace:
            raise HandoffError("workspace must be an explicit canonical directory")
        return packet

    def _destination(self, packet, row=None, observation=None):
        context = self.transport.context()
        if (
            not isinstance(context, dict)
            or set(context) != {"host", "socket_path", "socket_dev", "socket_inode"}
            or any(type(context[key]) is not int for key in ("socket_dev", "socket_inode"))
        ):
            raise HandoffError("invalid host/socket context")
        _text(context["host"], "host")
        _text(context["socket_path"], "socket path")
        if observation is None:
            observation = self.transport.inspect(packet["target"]["pane_id"])
        if not isinstance(observation, dict):
            raise HandoffError("invalid Herdr observation")
        for key in _IDENTITY:
            _text(observation.get(key), key)
        if any(observation[key] != value for key, value in packet["target"].items()):
            raise HandoffError("Herdr destination identity changed")
        identity = {key: observation[key] for key in _IDENTITY}
        identity["agent_session"] = observation.get("agent_session")
        if row is not None and (row["context"] != context or row["identity"] != identity):
            raise HandoffError("Herdr host, socket or agent generation changed")
        if type(observation.get("state_change_seq")) is not int:
            raise HandoffError("Herdr activity sequence unavailable")
        return context, identity, observation

    def _workspace(self, packet, contract, *, initial=False):
        workspace = Path(packet["target"]["cwd"])
        if workspace.resolve(strict=True) != workspace:
            raise HandoffError("workspace identity changed")
        if (
            Path(_git(workspace, "rev-parse", "--show-toplevel")) != workspace
            or _git(workspace, "remote", "get-url", "origin") != contract["repository"]
        ):
            raise HandoffError("workspace source identity differs")
        head = _git(workspace, "rev-parse", "HEAD")
        _git(workspace, "merge-base", "--is-ancestor", contract["base_revision"], head)
        if initial and (
            head != contract["base_revision"] or _git(workspace, "status", "--porcelain=v1")
        ):
            raise HandoffError("initial workspace must be clean at the exact base")

    def deliver(self, packet):
        """Submit once under local custody/freeze locks; ambiguity never resends."""
        packet = self._packet(packet)
        with (
            self._locked(),
            store.actuation_exclusion(self.paths),
            authority(self.home, self.actor, packet, delivery=True) as contract,
        ):
            if self._path(packet["assignment_id"]).exists():
                row = self._read(packet["assignment_id"])
                if row["packet"] != packet or row["contract"] != contract:
                    raise HandoffError("assignment replay conflicts with exact content")
                self._destination(packet, row)
                return row
            for path in self.directory.glob("*.json"):
                prior = self._read(path.stem)["packet"]
                if (prior["helper_id"], prior["claim_revision"]) == (
                    packet["helper_id"],
                    packet["claim_revision"],
                ):
                    raise HandoffError("helper generation already has an assignment")
            gate = store.check_actuation_gate(self.paths)
            if not gate.allowed:
                raise HandoffError(f"handoff delivery refused: {gate.reason}")
            context, identity, observation = self._destination(packet)
            pinned_host = contract["helper_contract"]["coordinator_host"]
            if pinned_host is not None and pinned_host != context["host"]:
                raise HandoffError("explicit helper coordinator host differs")
            if observation["state"] not in {"idle", "done"}:
                raise HandoffError("Herdr agent is not available for a new assignment")
            self._workspace(packet, contract, initial=True)
            row = {
                "schema": 1,
                "packet": packet,
                "packet_sha256": explicit_creation_request_digest(packet),
                "contract": contract,
                "context": context,
                "identity": identity,
                "initial_seq": observation["state_change_seq"],
                "observation": observation,
                "state": "send-intent",
                "activity": False,
            }
            prompt_data = {key: row[key] for key in ("packet", "packet_sha256", "contract")}
            prompt = (
                "Execute only this exact helper assignment. Preserve all inherited restrictions. "
                "Use fleet handoff receipt for structured pickup/result evidence. "
                "No card completion, "
                "claim transfer, shared edits, push, deployment or provider changes.\n"
                + json.dumps(prompt_data, sort_keys=True)
            )
            if len(prompt.encode()) > 32768:
                raise HandoffError("projected helper prompt exceeds transport bound")
            self._write(row)
            try:
                response = self.transport.prompt(packet["target"]["pane_id"], prompt)
                row["state"] = "delivery-unknown"
                if response.get("delivery") == "submitted":
                    self._destination(packet, row, response.get("agent", {}))
                    row["state"] = "submitted"
            except Exception:
                row["state"] = "delivery-unknown"
            self._write(row)
            return row

    def _observe(self, row, contract):
        if row["contract"] != contract:
            raise HandoffError("assignment authority or contract changed")
        _, _, observation = self._destination(row["packet"], row)
        self._workspace(row["packet"], contract)
        row["observation"] = observation
        row["activity"] = row["activity"] or (
            observation["state_change_seq"] > row["initial_seq"]
            and observation["state"] in {"working", "blocked", "done"}
        )
        return row

    def observe(self, assignment_id):
        """Read and reconcile this destination without inferring ownership or resending."""
        with self._locked():
            row = self._read(assignment_id)
            with authority(self.home, self.actor, row["packet"]) as contract:
                self._observe(row, contract)
                self._write(row)
                return row

    def receipt(self, assignment_id, kind, payload):
        """Bind structured worker reports and actual artifact bytes to this assignment."""
        if kind not in {"pickup", "result"} or not isinstance(payload, dict):
            raise HandoffError("invalid receipt kind or payload")
        fields = _RECEIPT | ({"artifacts", "tests"} if kind == "result" else set())
        if set(payload) != fields:
            raise HandoffError("invalid structured receipt fields")
        payload = _bounded_copy(payload)
        with self._locked():
            row = self._read(assignment_id)
            with authority(self.home, self.actor, row["packet"]) as contract:
                self._observe(row, contract)
                expected = {
                    key: row["packet"][key]
                    for key in ("assignment_id", "helper_id", "claim_revision")
                }
                expected.update(
                    packet_sha256=row["packet_sha256"],
                    base_revision=contract["base_revision"],
                    cwd=row["identity"]["cwd"],
                )
                if any(payload[key] != value for key, value in expected.items()):
                    raise HandoffError("receipt does not match exact assignment")
                if not row["activity"]:
                    raise HandoffError("no post-submission activity corroborates receipt")
                if kind in row and row[kind] != payload:
                    raise HandoffError("receipt conflicts with existing exact custody")
                if kind == "result":
                    if "pickup" not in row:
                        raise HandoffError("result requires structured pickup first")
                    artifacts, tests = payload["artifacts"], payload["tests"]
                    if (
                        not isinstance(artifacts, list)
                        or not 1 <= len(artifacts) <= 32
                        or not isinstance(tests, list)
                        or not 1 <= len(tests) <= 32
                    ):
                        raise HandoffError("bounded artifacts and test reports required")
                    for artifact in artifacts:
                        if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
                            raise HandoffError("invalid artifact")
                        relative = Path(_text(artifact["path"], "artifact path"))
                        if (
                            relative.is_absolute()
                            or str(relative) != artifact["path"]
                            or any(p in {"..", ".git"} for p in relative.parts)
                        ):
                            raise HandoffError("artifact must be inside the workspace")
                        data = _regular(Path(row["identity"]["cwd"]) / relative, 16 * 1024 * 1024)
                        if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
                            raise HandoffError("artifact bytes do not match receipt")
                    for test in tests:
                        if not isinstance(test, dict) or set(test) != {"command", "outcome"}:
                            raise HandoffError("invalid reported test")
                        _text(test["command"], "test command")
                        _text(test["outcome"], "test outcome")
                row[kind] = payload
                row["state"] = (
                    "delivered" if kind == "result" or "result" in row else "acknowledged"
                )
                self._write(row)
                return row
