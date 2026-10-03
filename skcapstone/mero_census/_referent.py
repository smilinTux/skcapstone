"""The BLOCKED-contract referent validator of the Mero blocker census.

Card 8fa7d8eb moved ``_referent_defect`` verbatim from the single-module
layout of card 2516480b. A BLOCKED verdict must carry, as its own typed
fields, exactly one allowed ``blocked_on`` value plus a referent shape that
exists. This module is the only judge of that shape.
"""

from __future__ import annotations

import re
from typing import Callable

from ._constants import _BLOCKED_ON_VALUES

__all__ = ["_referent_defect"]


def _referent_defect(event: dict, resolve_card: Callable[[str], object]) -> str | None:
    """Why a blocker event's typed referent violates the BLOCKED contract.

    Returns None when the event carries a well-formed referent, per the four
    allowed ``blocked_on`` values and their referent shapes:
    dependency -> card:<id>, human -> approval:<what>, capability -> ac:<n>|free,
    card -> ac:<n>.
    """
    blocked = event.get("blocked_on")
    if isinstance(blocked, dict):
        value = str(blocked.get("value") or blocked.get("type") or "").strip().lower()
        referent = str(blocked.get("referent") or "").strip()
    elif isinstance(blocked, str) and blocked.strip():
        text = blocked.strip()
        match = re.search(r"\b(dependency|human|capability|card)\b", text, re.IGNORECASE)
        value = match.group(1).lower() if match else ""
        ref_match = re.search(r"referent[\"']?\s*[=:]?\s*[\"']?([^\s,;\"']+)", text, re.IGNORECASE)
        referent = ref_match.group(1).strip("\"'") if ref_match else ""
    else:
        # No blocked_on at all: the malformed-blocker census reads the
        # verdict prose before declaring the typed fields absent.
        text = str(event.get("verdict") or event.get("reason") or "")
        match = re.search(r"\b(dependency|human|capability|card)\b", text, re.IGNORECASE)
        value = match.group(1).lower() if match else ""
        ref_match = re.search(r"referent[\"']?\s*[=:]?\s*[\"']?([^\s,;\"']+)", text, re.IGNORECASE)
        referent = ref_match.group(1).strip("\"'") if ref_match else ""
        if not value and not referent:
            return "missing_or_unknown_blocked_on_value"
    if value not in _BLOCKED_ON_VALUES:
        return "missing_or_unknown_blocked_on_value"
    if not referent:
        return "missing_referent"
    if value == "dependency":
        # A dependency referent must be card:<hex id>. Do not liberalise with
        # a strip-nonhex pass: "notacard" would otherwise normalize to hex
        # "acad" and look like a plausible id.
        id_match = re.fullmatch(r"(?:card:)?([0-9a-f]{8,32})", referent, re.IGNORECASE)
        if id_match is None:
            return "dependency_referent_not_a_card_id"
        target = id_match.group(1).lower()
        if resolve_card(target) is None:
            return "dependency_referent_unresolvable"
    elif value == "human":
        if not referent:
            return "human_referent_missing"
    elif value == "capability":
        if not (re.fullmatch(r"ac:\d+", referent, re.IGNORECASE) or referent.lower() == "free"):
            return "capability_referent_not_ac_or_free"
    else:  # card
        if not re.fullmatch(r"ac:\d+", referent, re.IGNORECASE):
            return "card_referent_not_ac"
    return None
