"""Drift-guard: each subapp's generated SKWorld module manifest must agree with
that subapp's operator adapter in skcapstone.

The manifest ``operator`` block and the adapter live in DIFFERENT repos (skchat /
skharness ship the manifest; skcapstone ships the adapter) and were kept in sync
by hand. This test makes any drift impossible to miss: if a manifest declares a
condition or proposed action the adapter does not emit (or vice versa), the
assertion fails loudly and names the app.

The manifest builders are imported from their sibling repos via ``importorskip``
so a bare CI env without the sibling package SKIPS rather than errors (mirrors the
app-repo parity tests).
"""

from __future__ import annotations

import pytest

from skcapstone.operator_seat import skchat_adapter, skcode_adapter


def _proposed_standard_actions(actions: list[dict]) -> list[str]:
    """The standard-AND-reversible action names, matching how
    ``registration.derive_operatorapp_spec`` computes proposedStandardActions."""
    return [a["name"] for a in actions if a.get("standard") and a.get("reversible")]


# (test id, importorskip module, builder attr, adapter module)
_CASES = [
    ("skchat", "skchat.skworld_manifest", "skchat_module_manifest", skchat_adapter),
    ("skcode", "skharness.manifest", "skcode_module_manifest", skcode_adapter),
]


@pytest.mark.parametrize(
    "app, module_name, builder_attr, adapter",
    _CASES,
    ids=[c[0] for c in _CASES],
)
def test_manifest_operator_block_matches_adapter(app, module_name, builder_attr, adapter):
    """The manifest operator block must exactly match the adapter it mirrors."""
    module = pytest.importorskip(
        module_name, reason=f"{module_name} (sibling repo) not installed"
    )
    build = getattr(module, builder_attr)
    operator = build("http://x/")["operator"]

    # Key drift check: conditions must be the adapter's CONDITIONS, exact + ordered.
    assert operator["conditions"] == adapter.CONDITIONS, (
        f"{app}: manifest operator conditions drifted from the adapter. "
        f"manifest={operator['conditions']} adapter={adapter.CONDITIONS}"
    )

    # The manifest must propose exactly the adapter's standard+reversible actions.
    expected_actions = _proposed_standard_actions(adapter._ACTIONS)
    assert operator["proposedStandardActions"] == expected_actions, (
        f"{app}: manifest proposedStandardActions drifted from the adapter's "
        f"standard+reversible actions. manifest={operator['proposedStandardActions']} "
        f"adapter={expected_actions}"
    )

    # Contract sanity: version is the int 1, cli + repos are non-empty.
    assert operator["contractVersion"] == 1
    assert isinstance(operator["contractVersion"], int)
    assert operator["cli"], f"{app}: manifest operator cli is empty"
    assert operator["repos"], f"{app}: manifest operator repos is empty"
