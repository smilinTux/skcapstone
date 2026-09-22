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

A second, narrower instance of the same defect survived inside the review
branch itself. `model` was resolved correctly via `_lane_model(...)` for
EVERY card, then for a governed review card only, immediately reset back to
`_bucket` a few lines later. The comment guarding that reset argued
`eligible_review_routes`/`choose_review_route` had already made a
better-informed selection than the lane's model resolution, so overriding
back to the bucket preserved that selection. That reasoning does not hold:
those two functions choose a `capacity_domain` for admission and occupancy
bookkeeping, never the model string sent to the gateway. Overriding `model`
to `_bucket` there did exactly what the fix above stops it doing everywhere
else: discarded the operator's configured reviewer model and sent the
shared `sk-s` pool alias instead.

Measured 2026-08-31: SKFLEET_MODEL_S=sk-codex-mid for the Seraph seat was
confirmed loaded into the running service (systemctl --user show at the
14:25:10 CDT restart), and governed review workers launched afterward still
sent "model":"sk-s" on every turn because of this second override. sk-s
round-robins across backends including chiap08-qwen38, the only erroring
backend on the gateway that day (80 errors of 3,943 requests, every other
backend zero). A review turn landing there returned an empty completion, the
reviewer recorded no verdict, and the wrapper exited 75 no_card_mutation;
cards accumulated 17 claims and 18 releases this way.

A tempting "fix" for that reset is reading `model` back off `_selected_route`
instead (`model=str(_selected_route["model_or_bucket"])`), so the request
always names the exact route `choose_review_route` reserved capacity for.
That was tried in review here and reverted: it is the pattern the
2026-09-18 producer-dispatch fix (95c04b06) deliberately removed and pinned
against in `test_skfleet_logical_routes.py`
(`test_launch_never_replaces_logical_route_with_selected_member`) and
`test_skfleet_pool_v2_authority.py`
(`test_worker_runtime_contract_is_unchanged`). `_selected_route` only exists
to pick a `capacity_domain` for LOCAL oversubscription bookkeeping against
the live queue snapshot; it is not the gateway's authority on what a model
request will actually do. `resolve_and_preflight`, called on the resolved
`model` a few lines below, is that authority: it independently probes the
exact model about to be requested against the live catalog before dispatch,
and fails the card closed if that model is not currently advertised or
healthy. Coupling `model` to `_selected_route` would make two same-size
concurrent review cards send two DIFFERENT models depending on which route
each happened to reserve, silently defeating the operator's per-size
configuration for exactly the cards that raced each other for capacity,
which is the opposite of what this fix is for.
"""

from __future__ import annotations

import ast
import os
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "fleet" / "skfleet-rotate.py"

CONSTANT_NAMES = {
    "_GLM_SIZE_RE",
    "_KIMI_SIZE_RE",
    "_LOGICAL_ROUTES",
    "_SIZE_MODEL_DEFAULTS",
    "_SIZE_MODELS",
    "_GLM_LEVEL_DEFAULTS",
    "_GLM_LEVELS",
}
FUNCTION_NAMES = {
    "_size_class_for",
    "_size_model_for",
    "_glm_model_for",
    "_kimi_model_for",
    "_lane_model",
}


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


def _load_real_size_model_chain(monkeypatch, env: dict[str, str]):
    """Exec the REAL `_SIZE_MODELS`/`_size_model_for`/`_lane_model` chain, no
    stubs, with `env` as the only fleet env vars set. This is the same chain
    the dispatcher's `model=_lane_model(_LANE,core) or _bucket` line runs, so
    it is what proves an operator's SKFLEET_MODEL_<size> actually reaches a
    governed review card's dispatch and not just the function in isolation.
    """
    for key in list(os.environ):
        if key.startswith("SKFLEET_MODEL_") or key.startswith("SKFLEET_CODEX_MODEL_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    tree = ast.parse(_source())
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTION_NAMES:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if names & CONSTANT_NAMES:
                nodes.append(node)
    module = ast.Module(body=nodes, type_ignores=[])
    ns: dict = {"os": os, "re": __import__("re")}
    exec(compile(module, str(SRC), "exec"), ns)
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


def _governed_branch_window() -> str:
    """The launch-path governed review branch, from its `_review_seat=`
    assignment through the `_route_identity` it builds a bit further down.

    rindex, not index: the identifier appears earlier in the candidate scan
    too, and only the occurrence on the LAUNCH path is the one that runs
    before route preflight reads `model`.
    """
    source = _source()
    seat = source.rindex("_review_seat=_fresh_review_seat")
    return source[seat : seat + 7000]


def test_a_governed_review_card_keeps_the_resolved_model_not_the_bucket():
    """Review dispatch must send the SAME resolved model producer dispatch
    sends, not the bare bucket, and must not read a substitute model back off
    `_selected_route` either (see the module docstring for why that
    alternative was tried and reverted): `model` must simply be left alone
    between its resolution via `_lane_model(...)` and the `_route_identity`
    literal that records it.
    """
    window = _governed_branch_window()
    assert "model=_bucket" not in window, (
        "a governed review card must not be reset back onto the bare bucket; "
        "it should keep the model _lane_model(...) already resolved"
    )
    assert 'model=str(_selected_route["model_or_bucket"])' not in window, (
        "a governed review card must not read its dispatched model back off "
        "_selected_route either; that desyncs concurrent same-size cards "
        "onto different models depending only on which route each reserved"
    )
    assert "keeps the BARE BUCKET" not in _source(), (
        "the misleading rationale for the bucket reset must not still be in "
        "the source next to code that no longer does it"
    )


def test_the_governed_review_route_identity_keeps_the_chosen_capacity_domain():
    """Removing the model override must not touch capacity-domain selection:
    the governed branch's `_route_identity` still carries whichever
    `capacity_domain` `choose_review_route` picked from the review-specific
    `_routes` (built by `eligible_review_routes` against the reviewer seat,
    the producer identity and per-route occupancy), and `model_or_bucket`
    still carries the resolved `model`, not `_bucket`.
    """
    window = _governed_branch_window()
    routes_idx = window.index('_routes=[route for route in _capacity["routes"]')
    selected_idx = window.index("_selected_route=choose_review_route(_routes,")
    identity_idx = window.index('"capacity_domains":[str(_selected_route["capacity_domain"])]')
    model_idx = window.index('"model_or_bucket":model,')
    assert routes_idx < selected_idx < identity_idx, (
        "the governed branch must still pick its capacity domain from the "
        "review-specific eligible_review_routes/choose_review_route pair"
    )
    assert identity_idx < model_idx or model_idx < identity_idx + 200, (
        "capacity_domains and model_or_bucket should be set together in the "
        "same _route_identity literal"
    )


def test_a_producer_card_route_identity_is_unaffected():
    """The non-governed (`else`) branch must be untouched by the review-branch
    fix: it already resolved `model` via `_lane_model(...)` before the
    if/else and never had the bucket-reset bug, so its `_route_identity`
    literal must be byte-identical in shape to the governed one for the two
    fields that matter.
    """
    source = _source()
    else_idx = source.rindex(
        '\n    else:\n        admitted,health_reason=_health_for(_LANE["name"],model)'
    )
    window = source[else_idx : else_idx + 900]
    assert '"capacity_domains":[str(_selected_route["capacity_domain"])],' in window
    assert '"model_or_bucket":model,' in window
    assert "model=_bucket" not in window
    assert 'model=str(_selected_route["model_or_bucket"])' not in window


def test_a_governed_review_card_resolves_the_operator_configured_model(monkeypatch):
    """End-to-end through the REAL (unstubbed) constant/function chain: an
    operator's SKFLEET_MODEL_S drop-in must reach the model a governed [S]
    review card dispatches with. `tests/test_skfleet_seraph_selector_e2e.py`
    proves this through the actual dispatcher subprocess, not just this
    isolated chain.
    """
    lane_model = _load_real_size_model_chain(monkeypatch, {"SKFLEET_MODEL_S": "sk-codex-mid"})
    core = {"title": "[S][REVIEW] some governed review card"}
    assert lane_model({"name": "codex", "model": "sk-s"}, core) == "sk-codex-mid"


def test_a_governed_review_card_honors_the_deprecated_env_spelling(monkeypatch):
    """SKFLEET_CODEX_MODEL_<size> is the deprecated spelling, read when the
    new name is unset, and SKFLEET_MODEL_<size> wins when both are set.
    """
    lane_model = _load_real_size_model_chain(
        monkeypatch, {"SKFLEET_CODEX_MODEL_S": "sk-codex-fast"}
    )
    core = {"title": "[S][REVIEW] some governed review card"}
    assert lane_model({"name": "codex", "model": "sk-s"}, core) == "sk-codex-fast"

    lane_model = _load_real_size_model_chain(
        monkeypatch,
        {"SKFLEET_MODEL_S": "sk-codex-mid", "SKFLEET_CODEX_MODEL_S": "sk-codex-fast"},
    )
    assert lane_model({"name": "codex", "model": "sk-s"}, core) == "sk-codex-mid"


def test_a_governed_review_card_with_no_model_configured_still_resolves_sanely(monkeypatch):
    """With no SKFLEET_MODEL_S/SKFLEET_CODEX_MODEL_S set at all, a governed
    review card must still resolve to a non-empty, sane default (the bucket
    itself) rather than crashing or dispatching an empty model string.
    """
    lane_model = _load_real_size_model_chain(monkeypatch, {})
    core = {"title": "[S][REVIEW] some governed review card"}
    resolved = lane_model({"name": "codex", "model": "sk-s"}, core)
    assert resolved == "sk-s"
    assert resolved
