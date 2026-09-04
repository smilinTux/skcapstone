"""Fleet worker beat: writer, reader, and classifier (card ad0c3bfd / A).

Beats are STATE: a small self-expiring file per worker, one writer,
Syncthing-replicated, only the latest value matters. Dispositions are EVENTS:
sent via skmail, append-only, auditable. This module handles the state half.

The invariant (verbatim from the protocol review): no lease state is derived
from beat evidence alone; beats only corroborate preconditioned claim events.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

# Shared with heartbeat.py: one allowlist, not two.
BEAT_OWNER_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Disposition vocabulary (card C will use these; wrapper uses RUNNING only)
DISPOSITIONS = frozenset(
    {
        "RUNNING",
        "WAITING_DEPENDENCY",
        "BLOCKED_NEEDS_HUMAN",
        "DEGRADED_RETRYING",
    }
)

DEFAULT_BEAT_INTERVAL_S = 600  # 10 minutes
SHADOW_ALERT_TTL_S = 900  # alert only, never actuate (measured p95 292s)
ACTUATION_FLOOR_S = 3600  # minimum before any claim-affecting action
STARTUP_GRACE_S = 120  # wrapper may take this long to first beat


def validate_beat_owner(owner: str) -> str:
    """Reject any beat owner outside [a-z0-9-]. Fail fast, never sanitize."""
    cleaned = (owner or "").strip().lower()
    if not cleaned:
        raise ValueError("beat owner must be non-empty")
    if not BEAT_OWNER_RE.fullmatch(cleaned):
        raise ValueError("beat owner %r outside [a-z0-9-]; rejected, not sanitized" % owner)
    return cleaned


@dataclass(frozen=True)
class Beat:
    """One beat record, parsed from disk."""

    owner: str
    card_id: str = ""
    claim_revision: str = ""
    emitter: str = "wrapper"  # wrapper | agent
    disposition: str = "RUNNING"
    elapsed_s: int = 0
    beat_at: float = 0.0  # epoch seconds, writer's clock
    sequence: int = 0  # monotonic per writer
    progress_token: str = ""  # agent-only, opaque to monitor

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.beat_at)


def write_beat(
    beats_dir: Path,
    owner: str,
    card_id: str = "",
    claim_revision: str = "",
    emitter: str = "wrapper",
    disposition: str = "RUNNING",
    elapsed_s: int = 0,
    sequence: int = 0,
    progress_token: str = "",
) -> Beat:
    """Write one beat atomically (temp + rename). Never raises on beat content.

    A wrapper beat may never set progress_token; enforced here.
    """
    owner = validate_beat_owner(owner)
    if emitter == "wrapper" and progress_token:
        raise ValueError("wrapper beats must not carry progress_token")
    if disposition not in DISPOSITIONS:
        raise ValueError("disposition %r not in vocabulary" % disposition)

    beat = Beat(
        owner=owner,
        card_id=card_id,
        claim_revision=claim_revision,
        emitter=emitter,
        disposition=disposition,
        elapsed_s=elapsed_s,
        beat_at=time.time(),
        sequence=sequence,
        progress_token=progress_token,
    )

    beats_dir.mkdir(parents=True, exist_ok=True)
    path = beats_dir / f"{owner}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            {
                "owner": beat.owner,
                "card_id": beat.card_id,
                "claim_revision": beat.claim_revision,
                "emitter": beat.emitter,
                "disposition": beat.disposition,
                "elapsed_s": beat.elapsed_s,
                "beat_at": beat.beat_at,
                "sequence": beat.sequence,
                "progress_token": beat.progress_token,
            }
        ),
        encoding="utf-8",
    )
    tmp.rename(path)  # atomic on same filesystem
    return beat


def write_agent_beat(
    beats_dir: Path,
    owner: str,
    card_id: str = "",
    claim_revision: str = "",
    disposition: str = "RUNNING",
    elapsed_s: int = 0,
    sequence: int = 0,
    progress_token: str = "",
    *,
    skmail_recipient: str = "",
) -> Beat:
    """Write an agent beat. Non-RUNNING dispositions also emit one skmail.

    This is the agent-side entry point (Card C). The wrapper cannot call
    this: emitter is fixed to "agent" and progress_token is allowed.
    """
    import subprocess

    beat = write_beat(
        beats_dir,
        owner,
        card_id=card_id,
        claim_revision=claim_revision,
        emitter="agent",
        disposition=disposition,
        elapsed_s=elapsed_s,
        sequence=sequence,
        progress_token=progress_token,
    )
    if disposition != "RUNNING" and skmail_recipient:
        # A block is an event with history, not a state to be overwritten
        try:
            subprocess.run(
                [
                    "skmail",
                    "send",
                    owner,
                    skmail_recipient,
                    "normal",
                    "beat disposition",
                    "%s %s" % (disposition, card_id),
                ],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass  # mail failure never fails the beat
    return beat


def read_beats(beats_dir: Path) -> list[Beat]:
    """Read every parseable beat. Malformed files are skipped, never raise."""
    out: list[Beat] = []
    if not beats_dir.is_dir():
        return out
    for path in beats_dir.glob("*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
            out.append(
                Beat(
                    owner=str(raw.get("owner", path.stem)),
                    card_id=str(raw.get("card_id", "")),
                    claim_revision=str(raw.get("claim_revision", "")),
                    emitter=str(raw.get("emitter", "wrapper")),
                    disposition=str(raw.get("disposition", "RUNNING")),
                    elapsed_s=int(raw.get("elapsed_s", 0)),
                    beat_at=float(raw.get("beat_at", 0)),
                    sequence=int(raw.get("sequence", 0)),
                    progress_token=str(raw.get("progress_token", "")),
                )
            )
        except (json.JSONDecodeError, ValueError, OSError):
            continue  # malformed beat: skip, never hide other workers
    return out


@dataclass(frozen=True)
class BeatThresholds:
    """Tunable thresholds. Defaults from measured Syncthing p95 (review)."""

    shadow_alert_s: float = SHADOW_ALERT_TTL_S
    actuation_floor_s: float = ACTUATION_FLOOR_S
    startup_grace_s: float = STARTUP_GRACE_S


@dataclass(frozen=True)
class BeatClassification:
    """The result of classifying one worker's beats."""

    state: str  # LIVE | STALLED | BLOCKED | DEAD | NEVER_STARTED | UNKNOWN
    evidence: str  # agent_beat | wrapper_beat | none
    age_s: float  # seconds since last beat (0 if never)
    disposition: str = ""  # from the beat, if any
    note: str = ""  # human-readable context


def classify(
    beats: list[Beat],
    owner: str,
    now: float | None = None,
    thresholds: BeatThresholds | None = None,
) -> BeatClassification:
    """Classify one worker from its beat(s). Pure function, no filesystem.

    Survivorship rule: a missing sequence number is unknown, never evidence.
    Invariant: no lease state is derived from beat evidence alone.
    """
    _now = now if now is not None else time.time()
    _th = thresholds or BeatThresholds()

    # Prefer agent beats over wrapper beats (higher information content)
    agent_beats = [b for b in beats if b.owner == owner and b.emitter == "agent"]
    wrapper_beats = [b for b in beats if b.owner == owner and b.emitter == "wrapper"]
    best = agent_beats[-1] if agent_beats else (wrapper_beats[-1] if wrapper_beats else None)
    evidence = "agent_beat" if agent_beats else ("wrapper_beat" if wrapper_beats else "none")

    if best is None:
        return BeatClassification(
            state="NEVER_STARTED",
            evidence="none",
            age_s=0,
            note="no beat file for this owner",
        )

    age = max(0.0, _now - best.beat_at)

    # Clock skew: a beat in the future is not negative age
    if best.beat_at > _now:
        return BeatClassification(
            state="UNKNOWN",
            evidence=evidence,
            age_s=0,
            disposition=best.disposition,
            note="beat_at is in the future (clock skew); age is unreliable",
        )

    # Within startup grace and never progressed
    if best.elapsed_s == 0 and age < _th.startup_grace_s:
        return BeatClassification(
            state="UNKNOWN",
            evidence=evidence,
            age_s=age,
            note="within startup grace, first beat may not have landed",
        )

    if age > _th.actuation_floor_s:
        return BeatClassification(
            state="DEAD",
            evidence=evidence,
            age_s=age,
            disposition=best.disposition,
            note="no beat for %.0fs (above actuation floor)" % age,
        )

    if best.disposition != "RUNNING":
        return BeatClassification(
            state="BLOCKED",
            evidence=evidence,
            age_s=age,
            disposition=best.disposition,
            note="worker reports %s" % best.disposition,
        )

    if age > _th.shadow_alert_s:
        return BeatClassification(
            state="STALLED",
            evidence=evidence,
            age_s=age,
            note="no beat for %.0fs (above shadow alert threshold)" % age,
        )

    return BeatClassification(
        state="LIVE",
        evidence=evidence,
        age_s=age,
        disposition=best.disposition,
    )
