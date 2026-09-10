"""Fleet worker brief recipient routing tests for card bf7317af."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

ROTATE = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"


def functions():
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    wanted = {"_worker_mail_routing", "_worker_mail_instructions"}
    nodes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    }
    namespace = {"os": os, "re": __import__("re")}
    for name in wanted:
        exec(compile(ast.Module([nodes[name]], []), str(ROTATE), "exec"), namespace)
    return namespace


def test_default_routing_preserves_named_recipients_without_broadcast() -> None:
    route = functions()["_worker_mail_routing"]
    assert route({}) == ("jarvis", "lumina")
    assert "all" not in route({})


def test_exclusions_are_case_insensitive_and_removed_from_brief() -> None:
    values = functions()
    recipients = values["_worker_mail_routing"]({"SKFLEET_EXCLUDED_MAIL_RECIPIENTS": " LuMiNa "})
    brief = values["_worker_mail_instructions"](recipients)
    assert recipients == ("jarvis",)
    assert "lumina" not in brief.lower()
    assert "broadcast to all" in brief


def test_empty_config_falls_back_to_jarvis_not_all() -> None:
    route = functions()["_worker_mail_routing"]
    assert route({"SKFLEET_MAIL_RECIPIENTS": ""}) == ("jarvis",)


def test_empty_allowed_set_fails_closed() -> None:
    route = functions()["_worker_mail_routing"]
    with pytest.raises(ValueError, match="no allowed recipient"):
        route(
            {
                "SKFLEET_MAIL_RECIPIENTS": "jarvis,all",
                "SKFLEET_EXCLUDED_MAIL_RECIPIENTS": "JARVIS",
            }
        )
