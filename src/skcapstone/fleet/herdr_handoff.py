"""Durable local Herdr delivery evidence, never card ownership or acceptance."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
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

_DELIVERY_RECEIPT_SCHEMA = "skfleet.herdr-delivery-receipt/v1"
_DELIVERY_RECEIPT_FIELDS = {
    "schema",
    "parent_id",
    "helper_id",
    "helper_generation",
    "claim_revision",
    "host",
    "workspace_id",
    "tab_id",
    "pane_id",
    "agent_identity",
    "delivery_token_sha256",
    "delivery_state",
    "timestamps",
    "terminal_evidence",
}
_JOURNAL_FIELDS = {
    "schema",
    "packet",
    "packet_sha256",
    "contract",
    "context",
    "identity",
    "initial_seq",
    "observation",
    "state",
    "activity",
    "delivery_receipt",
    "pickup",
    "result",
}
_JOURNAL_REQUIRED = {
    "schema",
    "packet",
    "contract",
    "delivery_receipt",
}
_DELIVERY_STATES = {
    "reserved",
    "delivering",
    "delivered",
    "terminal",
    "cleaned",
    "failed",
    "held",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _receipt_identifier(value, name, maximum=128):
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value)
    ):
        raise HandoffError(f"invalid delivery receipt {name}")
    return value


def _delivery_receipt(value):
    if (
        not isinstance(value, dict)
        or set(value) != _DELIVERY_RECEIPT_FIELDS
        or value.get("schema") != _DELIVERY_RECEIPT_SCHEMA
    ):
        raise HandoffError("invalid delivery receipt fields or schema")
    for key in ("parent_id", "helper_id"):
        if not re.fullmatch(r"[0-9a-f]{8}", _text(value[key], key, 8)):
            raise HandoffError(f"invalid delivery receipt {key}")
    for key in (
        "helper_generation",
        "claim_revision",
        "host",
        "workspace_id",
        "tab_id",
        "pane_id",
    ):
        _receipt_identifier(value[key], key)
    if not re.fullmatch(r"[0-9a-f]{64}", value.get("delivery_token_sha256", "")):
        raise HandoffError("invalid delivery token digest")
    if value["delivery_state"] not in _DELIVERY_STATES:
        raise HandoffError("invalid delivery receipt state")
    timestamps = value["timestamps"]
    if (
        not isinstance(timestamps, dict)
        or not timestamps
        or not set(timestamps).issubset(_DELIVERY_STATES)
    ):
        raise HandoffError("invalid delivery receipt timestamps")
    for timestamp in timestamps.values():
        timestamp = _text(timestamp, "timestamp", 64)
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HandoffError("invalid delivery receipt timestamps") from exc
        if not timestamp.endswith("Z") or parsed.tzinfo != timezone.utc:
            raise HandoffError("invalid delivery receipt timestamps")
    identity = value["agent_identity"]
    if not isinstance(identity, dict) or set(identity) != {
        "agent_name",
        "agent_kind",
        "terminal_id",
        "agent_session",
    }:
        raise HandoffError("invalid delivery agent identity")
    for key in ("agent_kind", "terminal_id"):
        _text(identity[key], key, 128)
    for key in ("agent_name", "agent_session"):
        if identity[key] is not None:
            _text(identity[key], key, 128)
    terminal = value["terminal_evidence"]
    if terminal is not None and (
        not isinstance(terminal, dict)
        or set(terminal) != {"state", "state_change_seq", "process_group_alive"}
        or terminal["state"] != "done"
        or type(terminal["state_change_seq"]) is not int
        or terminal["process_group_alive"] is not False
    ):
        raise HandoffError("invalid terminal evidence")
    return value


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

    @contextmanager
    def _read_locked(self):
        """Share an existing journal lock without creating any store state."""
        try:
            fd = os.open(self.directory / ".lock", os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            yield
            return
        with os.fdopen(fd) as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise HandoffError("unsafe handoff lock")
            fcntl.flock(handle, fcntl.LOCK_SH)
            yield

    def _path(self, assignment):
        return self.directory / f"{_identifier(assignment)}.json"

    def _read(self, assignment):
        try:
            row = json.loads(_regular(self._path(assignment), 262144), object_pairs_hook=_pairs)
            if (
                not isinstance(row, dict)
                or not _JOURNAL_REQUIRED.issubset(row)
                or not set(row).issubset(_JOURNAL_FIELDS)
                or row["schema"] != 1
                or not isinstance(row["packet"], dict)
                or row["packet"].get("assignment_id") != assignment
            ):
                raise HandoffError("invalid assignment journal")
            if not isinstance(row["contract"], dict):
                raise HandoffError("invalid delivery journal contract")
            _delivery_receipt(row["delivery_receipt"])
            return row
        except (OSError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise HandoffError("assignment journal unavailable") from exc

    def delivery_records(self, helper_ids, limit=4096):
        """Return validated copies for exact helpers without changing the store."""
        if (
            not isinstance(helper_ids, (set, frozenset))
            or len(helper_ids) > 32
            or type(limit) is not int
            or not 1 <= limit <= 4096
        ):
            raise HandoffError("delivery query bound exceeded")
        for helper_id in helper_ids:
            if not re.fullmatch(r"[0-9a-f]{8}", _text(helper_id, "helper_id", 8)):
                raise HandoffError("invalid helper_id")
        if not self.directory.exists():
            return []
        if self.directory.resolve() != self.directory.absolute():
            raise HandoffError("unsafe handoff directory")

        def assignments():
            names = sorted(
                path.name for path in self.directory.iterdir() if path.suffix == ".json"
            )
            if len(names) > limit:
                raise HandoffError("delivery store row bound exceeded")
            return names

        with self._read_locked():
            names = assignments()
            rows = []
            for name in names:
                assignment = _identifier(name.removesuffix(".json"))
                row = self._read(assignment)
                if row["packet"].get("helper_id") in helper_ids:
                    rows.append(copy.deepcopy(row))
            if assignments() != names:
                raise HandoffError("delivery store changed during read")
        return rows

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

    def _sealed_contract(self, packet, contract):
        """Bind any matching automatic-crew request without inventing crew custody."""
        from .crew_store import CrewStore

        ledger = CrewStore(self.paths)
        matches = [
            row
            for row in ledger.list_requests()
            if row.get("helper_id") == packet["helper_id"]
            and row.get("canonical_request_id") == row.get("request_id")
        ]
        if not matches:
            return contract
        if len(matches) != 1:
            raise HandoffError("helper has ambiguous crew custody")
        request = matches[0]
        manifest = ledger.get_manifest(request["crew_id"])
        if (
            manifest is None
            or manifest.get("manifest_sha256") is None
            or manifest.get("packet", {}).get("parent_id") != contract["parent_id"]
            or manifest["packet"].get("parent_claim_revision") != contract["parent_claim_revision"]
            or manifest.get("parent_contract_sha256") != contract["parent_contract_sha256"]
            or request.get("helper_packet", {}).get("logical_route") != packet["route"]
        ):
            raise HandoffError("sealed crew manifest or route differs")
        sealed = dict(contract)
        sealed["crew_binding"] = {
            "crew_id": request["crew_id"],
            "request_id": request["request_id"],
            "manifest_sha256": manifest["manifest_sha256"],
            "provider_pair": manifest["packet"].get("provider_pair"),
            "producer_candidate": request.get("packet", {}).get("producer_candidate"),
        }
        return sealed

    def deliver(self, packet):
        """Submit once under local custody/freeze locks; ambiguity never resends."""
        packet = self._packet(packet)
        with (
            self._locked(),
            store.actuation_exclusion(self.paths),
            authority(self.home, self.actor, packet, delivery=True) as contract,
        ):
            contract = self._sealed_contract(packet, contract)
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
            token = secrets.token_urlsafe(32)
            reserved_at = _now()
            delivery_receipt = {
                "schema": _DELIVERY_RECEIPT_SCHEMA,
                "parent_id": contract["parent_id"],
                "helper_id": packet["helper_id"],
                "helper_generation": packet["claim_revision"],
                "claim_revision": packet["claim_revision"],
                "host": context["host"],
                "workspace_id": identity["workspace_id"],
                "tab_id": identity["tab_id"],
                "pane_id": identity["pane_id"],
                "agent_identity": {
                    key: identity[key]
                    for key in ("agent_name", "agent_kind", "terminal_id", "agent_session")
                },
                "delivery_token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "delivery_state": "reserved",
                "timestamps": {"reserved": reserved_at},
                "terminal_evidence": None,
            }
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
                "delivery_receipt": delivery_receipt,
            }
            prompt_data = {key: row[key] for key in ("packet", "packet_sha256", "contract")}
            prompt_data["delivery_token"] = token
            prompt = (
                "Execute only this exact helper assignment. Preserve all inherited restrictions. "
                "Use fleet handoff receipt for structured pickup/result evidence. "
                "No card completion, "
                "claim transfer, shared edits, push, deployment or provider changes.\n"
                + json.dumps(prompt_data, sort_keys=True)
            )
            if len(prompt.encode()) > 32768:
                raise HandoffError("projected helper prompt exceeds transport bound")
            row["delivery_receipt"]["delivery_state"] = "delivering"
            row["delivery_receipt"]["timestamps"]["delivering"] = _now()
            self._write(row)
            try:
                response = self.transport.prompt(packet["target"]["pane_id"], prompt)
                row["state"] = "delivery-unknown"
                if response.get("delivery") == "submitted":
                    self._destination(packet, row, response.get("agent", {}))
                    row["state"] = "submitted"
                    row["delivery_receipt"]["delivery_state"] = "delivered"
                    row["delivery_receipt"]["timestamps"]["delivered"] = _now()
            except Exception:
                row["state"] = "delivery-unknown"
                row["delivery_receipt"]["delivery_state"] = "held"
                row["delivery_receipt"]["timestamps"]["held"] = _now()
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
                contract = self._sealed_contract(row["packet"], contract)
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
                contract = self._sealed_contract(row["packet"], contract)
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

    def close_owned_delivery(self, assignment_id):
        """Close only this receipt's terminal pane, once, with no live process group."""
        with self._locked():
            row = self._read(assignment_id)
            with authority(self.home, self.actor, row["packet"]) as contract:
                contract = self._sealed_contract(row["packet"], contract)
                if row["contract"] != contract:
                    raise HandoffError("assignment authority or contract changed")
                if row.get("state") == "closed":
                    return row
                if row["delivery_receipt"]["delivery_state"] in {
                    "terminal",
                    "held",
                    "failed",
                }:
                    raise HandoffError("cleanup outcome is uncertain and cannot be replayed")
                self._observe(row, contract)
                observation = row["observation"]
                if observation["state"] != "done":
                    raise HandoffError("terminal worker state required for cleanup")
                probe = getattr(self.transport, "process_group_alive", None)
                if not callable(probe):
                    raise HandoffError("process group state unavailable")
                alive = probe(row["packet"]["target"]["pane_id"])
                if type(alive) is not bool:
                    raise HandoffError("process group state unavailable")
                if alive:
                    raise HandoffError("live process group prevents cleanup")
                row["delivery_receipt"]["terminal_evidence"] = {
                    "state": observation["state"],
                    "state_change_seq": observation["state_change_seq"],
                    "process_group_alive": False,
                }
                row["delivery_receipt"]["delivery_state"] = "terminal"
                row["delivery_receipt"]["timestamps"]["terminal"] = _now()
                self._write(row)
                closer = getattr(self.transport, "close", None)
                if not callable(closer):
                    raise HandoffError("Herdr cleanup unavailable")
                response = closer(row["packet"]["target"]["pane_id"])
                if not isinstance(response, dict) or response.get("closed") is not True:
                    row["delivery_receipt"]["delivery_state"] = "held"
                    row["delivery_receipt"]["timestamps"]["held"] = _now()
                    self._write(row)
                    raise HandoffError("Herdr cleanup not confirmed")
                returned = response.get("agent")
                if returned is not None:
                    self._destination(row["packet"], row, returned)
                row["delivery_receipt"]["delivery_state"] = "cleaned"
                row["delivery_receipt"]["timestamps"]["cleaned"] = _now()
                row["state"] = "closed"
                self._write(row)
                return row
