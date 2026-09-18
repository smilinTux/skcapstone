"""The dispatcher must send each lane's OWN model, not the bare size bucket.

Regression, measured on chi 2026-09-18. `_lane_model` resolves a card to the
model its lane actually uses (glm-4.7 for glm, the SKFLEET_MODEL_/
SKFLEET_CODEX_MODEL_ override for codex, kimi-for-coding or k3 for kimi). It
existed and was called by NOTHING, so every lane shipped the bare bucket
`sk-s`/`sk-m`/`sk-l`/`sk-xl` as its model.

This never errored, which is exactly why it survived: the bare bucket IS a
valid gateway route, and it resolves to the LOCAL QWEN38 FALLBACK. A card
dispatched to the codex lane asked for `sk-m`, was answered by qwen38, and came
back with perfectly good work. The subscription backends were simply never
asked for anything.

Measured at the gateway over the 60 minutes to 04:16, 1811 requests:

    chiap08-qwen  sk-m               727   40.1%
    chiap08-qwen  sk-m-secret        408   22.5%
    codex         sk-codex-mid         4    0.2%
    zai           glm-4.7              4    0.2%

A codex target of 30 could never be met, because codex was never asked.

Probed directly at the gateway the same day, which is what proves the models
themselves were fine:

    sk-codex-mid  -> served_by gpt-5.6-luna     (the codex backend)
    glm-4.7       -> served_by glm-5.3-flash    (the zai backend)
    sk-m          -> served_by qwen3.8-27b      (the fallback)
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "fleet" / "skfleet-rotate.py"


def _source() -> str:
    return SRC.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(_source(), node) or ""
    raise AssertionError(f"{name} not found in the dispatcher")


def _load_lane_model():
    """Exec just `_lane_model` with its three resolvers stubbed."""
    ns: dict = {
        "_glm_model_for": lambda core: "glm-4.7",
        "_size_model_for": lambda core: "sk-codex-mid",
        "_kimi_model_for": lambda core: "kimi-for-coding",
    }
    exec(_function_source("_lane_model"), ns)
    return ns["_lane_model"]


def test_each_lane_resolves_its_own_model_not_the_bucket():
    lane_model = _load_lane_model()
    core = {"title": "[M] a card"}
    assert lane_model({"name": "glm", "model": "sk-m"}, core) == "glm-4.7"
    assert lane_model({"name": "codex", "model": "sk-m"}, core) == "sk-codex-mid"
    assert lane_model({"name": "kimi", "model": "sk-m"}, core) == "kimi-for-coding"


def test_an_unknown_lane_still_falls_back_to_its_configured_model():
    lane_model = _load_lane_model()
    assert lane_model({"name": "qwen", "model": "sk-m"}, {"title": "[M] x"}) == "sk-m"


def test_the_launch_site_actually_calls_lane_model():
    """The guard that matters. `_lane_model` was correct and unreachable for
    its whole life; a test of the function alone would have passed the entire
    time the fleet was routing everything to qwen38.
    """
    src = _source()
    assert "_lane_model(_LANE,core)" in src.replace(
        " ", ""
    ), "the launch site must resolve the model through _lane_model"
    assert "_lane_model(_LANE,fresh_claimability" in src.replace(
        " ", ""
    ), "the post-race recheck must resolve the model through _lane_model too"


def test_the_route_identity_keeps_the_bucket_as_the_logical_route():
    """The bucket is the card's route identity and must not become the model,
    or preflight, health and evidence all start naming a backend instead of a
    capability bucket."""
    src = _source().replace(" ", "")
    assert '"logical_route":_bucket,' in src
    assert '"logical_route":model,' not in src
    assert '"model_or_bucket":model,' in src


def test_the_unsized_card_skip_is_preserved():
    """A card with no size marker must still be skipped, not silently given a
    lane default: a silent downgrade hides lost capability behind weaker work.
    """
    src = _source().replace(" ", "")
    assert "if_bucketisNone:" in src
    assert "SKIPPED_LOGICAL_ROUTE|" in _source()
    assert "SKIPPED_LOGICAL_ROUTE_RACE|" in _source()


def test_a_governed_review_card_keeps_the_bare_bucket_as_its_model():
    """Review dispatch must NOT get the lane's generic model.

    The review path already chose a concrete route through
    eligible_review_routes and choose_review_route against the advertised
    snapshot, using the reviewer seat, the producer identity and the per-route
    occupancy. Substituting the lane model afterwards would discard a selection
    made with strictly more information, and route preflight would then refuse
    the card outright because the lane model is not what the review snapshot
    offered. Producer dispatch has no such selection, which is the only path
    this change is allowed to alter.
    """
    source = _source()
    # rindex, not index: the identifier appears earlier in the candidate scan
    # too, and only the occurrence on the LAUNCH path is the one that runs
    # before route preflight reads `model`.
    seat = source.rindex("_review_seat=governed_review_seat(")
    window = source[seat : seat + 1400]
    assert "model=_bucket" in window, (
        "the governed-review branch must reset model to the bucket before "
        "route preflight reads it"
    )
