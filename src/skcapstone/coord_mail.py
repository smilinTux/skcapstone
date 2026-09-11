"""Agent-to-agent mailbox over the Syncthing coordination folder.

STORAGE: one file per WRITER PER HOST, ``coordination/skmail.d/<agent>@<host>.jsonl``.

That naming is the whole design, not a style choice. ``~/.skcapstone`` is a
Syncthing folder, and a single shared ``skmail.jsonl`` produced a real
sync-conflict on 2026-08-25 that silently swallowed a message: two hosts both
appended, Syncthing kept one version and moved the other aside. ``flock`` does
not help, because it is a single-host lock and the conflict is between hosts.
One writer per file means Syncthing never has two versions to reconcile. This
is the same pattern the CardStore already uses at
``cards/<id>/events/<writer>@<host>.jsonl``, which is why that store has never
conflicted.

``flock`` is still taken, because two agents on the SAME host can share one
writer file.

ESTATE ISOLATION: mailboxes never cross estates. ``lumina@noroc2027`` and
``lumina@chiap08`` are deliberately separate mailboxes with separate read
cursors, because nor and chi are different fleets on different Syncthing
shares. Reaching another estate's mail is an ad-hoc SSH operation, not a
federation feature.

Ported from the original 180-line bash implementation, keeping the on-disk
format byte-compatible so existing mailboxes stay readable.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

from .estate import (
    NODE_ENV,
    environment_d_dir,
    estate_id,
    local_host,
    node_name,
    xdg_config_home,
)
from .lifecycle_seats import LIFECYCLE_SEATS

VALID_PRIORITIES = ("urgent", "normal", "fyi")

#: Subdirectories every coordination plane needs. ``bootstrap`` creates these.
COORD_SUBDIRS = ("skmail.d", "card_events", "locks", "tasks", "agents", "reviews")

#: The bounded per-cycle unit ``bootstrap`` installs for the Niobe seat.
#: Five of the six lifecycle seats ship one. Niobe shipped only ``-live``,
#: which launches real agent runs, so an estate had no way to stage Niobe
#: without turning dispatch on. ``seat_cycle_entrypoint --seat niobe`` runs
#: the same bounded presence beat the other seats run, which is the safe
#: staging step.
NIOBE_CYCLE_UNITS = ("skfleet-niobe.service", "skfleet-niobe.timer")

#: Host-local file that persists this machine's fleet node name. It lives
#: under XDG config, NEVER in the synced coordination tree: every node has a
#: different answer, and a synced copy would hand them all the same one.
NODE_ENV_FILENAME = "skfleet-node.conf"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mailbox_dir(home: Path) -> Path:
    """Directory holding one JSONL per writer."""
    return Path(home) / "coordination" / "skmail.d"


def writer_file(home: Path, sender: str, host: str | None = None) -> Path:
    """The single file this writer appends to, on this host.

    Lowercased sender so ``Jarvis``/``jarvis``/``JARVIS`` share one mailbox,
    matching the bash implementation's case-insensitive recipient rule.
    """
    host = host or socket.gethostname()
    return mailbox_dir(home) / f"{sender.lower()}@{host}.jsonl"


def sender_fqid(home: Path, sender: str) -> str | None:
    """The sender's fully-qualified id, so a message says which estate it is from.

    Two estates run the same agent NAMES. `jarvis@chef.skworld` on the nor
    fleet is a different being, with a different identity, memory and operator,
    from Casey's jarvis on chi. The mailboxes are already separated
    structurally (a different Syncthing folder per estate, which is what
    actually keeps them apart), but the record itself carried only a bare
    `from: "jarvis"`. A message quoted, forwarded, or read outside its own
    folder therefore had nothing on it to disambiguate against, and the only
    thing standing between the two was whoever was reading remembering the
    difference.

    Best effort by design: a node with no identity file still sends, with the
    field left None rather than the send failing. Absent is honest; a guessed
    realm would be worse than none.
    """
    try:
        data = json.loads(
            (Path(home) / "agents" / sender.lower() / "identity" / "identity.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return None
    return data.get("fqid") or None


def send(
    home: Path, sender: str, to: str, priority: str, re: str, body: str, host: str | None = None
) -> dict:
    """Append one message to this writer's own file. Never writes a shared file.

    ``to`` may be ``all``. Raises ValueError on an unknown priority rather than
    silently downgrading it: ``urgent`` means "stop what you are doing", so a
    typo must not quietly become a normal message.
    """
    if priority not in VALID_PRIORITIES:
        raise ValueError(f"priority must be one of {'|'.join(VALID_PRIORITIES)}, got {priority!r}")
    if not sender or not to:
        raise ValueError("sender and recipient are both required")

    path = writer_file(home, sender, host)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": _now(),
        "from": sender,
        "to": to,
        "priority": priority,
        "re": re,
        "body": body,
        "host": host or socket.gethostname(),
        # Which estate this came from. None when the node has no identity
        # file; always present as a key so an unqualified sender is
        # explicit rather than indistinguishable from an older record.
        "from_fqid": sender_fqid(home, sender),
    }
    with open(path, "a", encoding="utf-8") as fh:
        # Same-host concurrency only; cross-host is solved by the filename.
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return rec


def _read_all(home: Path) -> list[dict]:
    """Every message across every writer file, oldest first.

    A malformed line is skipped rather than fatal: these files are appended by
    several processes and a partial line can exist mid-write.
    """
    out: list[dict] = []
    d = mailbox_dir(home)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.jsonl")):
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    out.sort(key=lambda r: str(r.get("ts", "")))
    return out


def _addressed_to(rec: dict, me: str) -> bool:
    to = str(rec.get("to", "")).lower()
    me = me.lower()
    # `to` may be a comma list, and `all` reaches everyone.
    return to == "all" or me in [t.strip() for t in to.split(",")]


def _cursor_path(home: Path, me: str) -> Path:
    return Path(home) / "coordination" / f".skmail-cursor.{me.lower()}"


def read(home: Path, me: str) -> list[dict]:
    """Unread messages addressed to ``me``, oldest first. Does not advance the cursor."""
    cur = ""
    cp = _cursor_path(home, me)
    if cp.exists():
        cur = cp.read_text(encoding="utf-8").strip()
    return [r for r in _read_all(home) if _addressed_to(r, me) and str(r.get("ts", "")) > cur]


def ack(home: Path, me: str) -> int:
    """Mark everything currently visible to ``me`` as read. Returns the count."""
    pending = read(home, me)
    if not pending:
        return 0
    cp = _cursor_path(home, me)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(str(pending[-1].get("ts", "")), encoding="utf-8")
    return len(pending)


def tail(home: Path, n: int = 10) -> list[dict]:
    """Recent traffic between any peers, newest last."""
    return _read_all(home)[-n:]


def _canonical(value: object) -> str:
    """Serialize one record the way every other coordination writer does."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"


def seat_control_plane_document(host: str, estate: str) -> dict:
    """Build the estate's six-seat control record for one elected host.

    Args:
        host: The host this estate elects to run the six lifecycle seats.
        estate: The estate identifier, used only to label the revision so a
            record read out of context says which estate it came from.

    Returns:
        A schema 1 control record pinning all six seats to ``host``.
    """
    return {
        "schema_version": 1,
        "revision": f"{estate}-lifecycle-six-seat-v1",
        "active_host": host,
        "seats": {seat: [host] for seat in sorted(LIFECYCLE_SEATS)},
    }


def _ensure_seat_control_plane(home: Path, host: str, estate: str) -> str | None:
    """Create ``coordination/seat-control-plane.json`` when it is absent.

    Without this file EVERY lifecycle seat refuses to run, because
    ``seat_cycle_entrypoint`` loads it before it will do anything at all.
    Nothing in the codebase created it, so a fresh estate had six seats that
    all failed closed until an operator wrote the file by hand.

    Create-or-skip on purpose: an existing record is the estate's own
    election and must never be silently repointed at whichever host happened
    to run bootstrap last.

    Args:
        home: The SKCapstone home (the synced estate tree).
        host: The host to elect when the record is absent.
        estate: The estate identifier for the revision label.

    Returns:
        The path relative to ``home`` when created, else ``None``.
    """
    path = home / "coordination" / "seat-control-plane.json"
    if path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical(seat_control_plane_document(host, estate)), encoding="utf-8")
    return str(path.relative_to(home))


def observation_feed_seed(home: Path, host: str, estate: str) -> dict:
    """Build a valid, self-hashed, EMPTY Link observation feed.

    The producer identity names bootstrap, not the GitHub producer, because
    that is what actually wrote it. Claiming otherwise would put a
    fabricated provenance behind a hash whose entire point is provenance.

    Zero records is the honest content: bootstrap observed no pull requests,
    and it has no connector with which to observe any.

    Args:
        home: The SKCapstone home, recorded as the producer workspace.
        host: The host that produced the seed.
        estate: The estate identifier for the source revision label.

    Returns:
        A document that ``link_observation_feed.load_observation_feed``
        accepts, yielding an empty but well-formed feed.
    """
    from .link_cycle import _digest
    from .link_observation_feed import SCHEMA, _canonical_payload

    payload = {
        "source_revision": f"{estate}-bootstrap-seed",
        "observed_at": _now(),
        "producer": {
            "identity": "coord-bootstrap",
            "host": host,
            "session": "bootstrap",
            "workspace": str(home),
        },
        "records": [],
        "reviewer_candidates": [],
    }
    return {"schema": SCHEMA, **payload, "evidence_sha256": _digest(_canonical_payload(payload))}


def _ensure_observation_feed(home: Path, host: str, estate: str) -> str | None:
    """Create ``coordination/link-observations.json`` when it is absent.

    Without it the Link seat returns ``observation_feed_missing`` on every
    cycle. Seeding an empty feed does not invent observations: it gives Link
    a well-formed input so its first cycle reports "nothing to recommend"
    instead of an input error, and so the failure it reports fifteen minutes
    later is the true one, ``observation_feed_stale``, which points at the
    producer timer nobody enabled rather than at a missing file.

    Args:
        home: The SKCapstone home (the synced estate tree).
        host: The host writing the seed.
        estate: The estate identifier for the source revision label.

    Returns:
        The path relative to ``home`` when created, else ``None``.
    """
    path = home / "coordination" / "link-observations.json"
    if path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical(observation_feed_seed(home, host, estate)), encoding="utf-8")
    return str(path.relative_to(home))


def _default_unit_dir() -> Path:
    """The systemd user unit directory, resolved through XDG.

    Returns:
        ``$XDG_CONFIG_HOME/systemd/user``, which is host-local by
        definition. Resolved here rather than imported from
        ``skcapstone.systemd`` because that module pins ``~/.config``
        directly and would ignore an operator's ``$XDG_CONFIG_HOME``.
    """
    return xdg_config_home() / "systemd" / "user"


def _ensure_niobe_cycle_units(unit_dir: Path) -> list[str]:
    """Install the bounded Niobe cycle units into the host-local unit dir.

    Args:
        unit_dir: The systemd user unit directory, which is host-local XDG
            config and never part of the synced estate tree.

    Returns:
        Absolute paths of the units created, empty when both already exist
        or when the packaged copies are not readable (an installed wheel
        always carries them; a stripped install honestly carries none).
    """
    created: list[str] = []
    for unit in NIOBE_CYCLE_UNITS:
        target = unit_dir / unit
        if target.exists():
            continue
        try:
            body = files("skcapstone").joinpath(f"data/systemd/{unit}").read_text(encoding="utf-8")
        except (OSError, ModuleNotFoundError):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        created.append(str(target))
    return created


def _ensure_node_env(env_dir: Path, node: str) -> str | None:
    """Persist ``SKFLEET_NODE`` where every shell on THIS host can see it.

    The value was previously set only inside a systemd unit, so it existed
    for the timer-driven seats and for nobody else: every interactive
    ``skcapstone fleet ...`` call failed with "no such node object". The fix
    is not to set it in more units, it is to put it in the host-local
    environment, which is exactly what ``environment.d`` is for.

    This file must never move into the synced tree. Every node has a
    different node name, so one synced copy would make every node in the
    estate answer with the first one's name.

    Args:
        env_dir: The ``environment.d`` directory under XDG config.
        node: The derived node name to persist.

    Returns:
        The absolute path when created, else ``None``.
    """
    path = env_dir / NODE_ENV_FILENAME
    if path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Host-local: this machine's fleet node name. Derived from the host\n"
        "# name by `skcapstone coord bootstrap`. Never copy this file into\n"
        "# ~/.skcapstone, which is shared with every node in the estate.\n"
        f"{NODE_ENV}={node}\n",
        encoding="utf-8",
    )
    return str(path)


def bootstrap(
    home: Path,
    agent: str | None = None,
    *,
    host: str | None = None,
    estate: str | None = None,
    unit_dir: Path | None = None,
    env_dir: Path | None = None,
) -> dict:
    """Create the coordination skeleton. Idempotent: creates only what is absent.

    Nothing in coordination.py ever created these directories, which is why a
    new node silently has no mailbox until someone makes one by hand. This is
    that step, made repeatable.

    A second estate stood up from scratch showed that the mailbox was only
    part of what nobody creates. Four more things had to be made by hand
    before a single lifecycle seat would run, and this function now makes
    all of them, still create-or-skip so re-running it never overwrites a
    decision the estate has already recorded:

    * ``coordination/seat-control-plane.json``, without which every seat
      refuses to run,
    * ``coordination/link-observations.json``, without which the Link seat
      reports ``observation_feed_missing`` forever,
    * the bounded ``skfleet-niobe`` cycle units, the safe staging step Niobe
      never had,
    * ``SKFLEET_NODE`` in the host-local ``environment.d``, without which
      every interactive ``skcapstone fleet`` call fails with "no such node
      object".

    The first two go in the synced estate tree, the last two are host-local
    and go under XDG. That split is the whole point: see
    :mod:`skcapstone.estate`.

    Args:
        home: The SKCapstone home (the synced estate tree).
        agent: Also create this agent's own mailbox file.
        host: Override the local host name. Derived from the machine when
            ``None``; no host name is ever hardcoded.
        estate: Override the estate identifier. Resolved from
            ``cluster.json`` or the local host when ``None``.
        unit_dir: Override the systemd user unit directory (host-local XDG
            config). Mainly for tests.
        env_dir: Override the ``environment.d`` directory (host-local XDG
            config). Mainly for tests.

    Returns:
        A dict with ``home``, ``created``, ``already_present``, ``mailbox``,
        ``host``, ``estate`` and ``node``. Paths inside ``home`` are
        reported relative to it; host-local paths outside the estate tree
        are reported absolutely, so the report itself shows which side of
        the split each item landed on.
    """
    home = Path(home)
    local = local_host(host)
    estate_name = estate_id(home, estate=estate, host=host)
    node = node_name(host)
    created: list[str] = []
    for sub in COORD_SUBDIRS:
        p = home / "coordination" / sub
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created.append(str(p.relative_to(home)))
    # "evidence/decisions" is created explicitly, not just its parent: the
    # decision log is the durable record a human gate is discharged into,
    # and a bootstrap that leaves it absent means the first writer has to
    # mkdir it by hand, which is exactly the manual step this removes.
    for top in ("cards", "evidence", "evidence/decisions"):
        p = home / top
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created.append(top)
    # Touch this agent's own mailbox so `read` works before the first `send`
    # and so the file exists for Syncthing to propagate.
    mailbox = None
    if agent:
        wf = writer_file(home, agent)
        if not wf.exists():
            wf.parent.mkdir(parents=True, exist_ok=True)
            wf.touch(mode=0o600)
            created.append(str(wf.relative_to(home)))
        mailbox = str(wf)

    # Estate-wide truth, written into the synced tree.
    for made in (
        _ensure_seat_control_plane(home, local, estate_name),
        _ensure_observation_feed(home, local, estate_name),
    ):
        if made:
            created.append(made)

    # Host-local truth, written under XDG and never into the synced tree.
    created.extend(_ensure_niobe_cycle_units(unit_dir or _default_unit_dir()))
    made = _ensure_node_env(env_dir or environment_d_dir(), node)
    if made:
        created.append(made)

    return {
        "home": str(home),
        "created": created,
        "mailbox": mailbox,
        "host": local,
        "estate": estate_name,
        "node": node,
        "already_present": [
            s for s in COORD_SUBDIRS if str(Path("coordination") / s) not in created
        ],
    }
