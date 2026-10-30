"""skcapstone delegates harness hook wiring to skmemory's installer."""

from __future__ import annotations

import sys
import types

from skcapstone import register
from skcapstone.codex_setup import ensure_codex_setup, render_loader_script


def test_loader_uses_skmemory_hook_entrypoint():
    script = render_loader_script()
    assert 'skmemory hook session-start --harness generic --agent "$AGENT"' in script
    assert "skmemory ritual --full ||" not in script
    assert "sed -n '1,220p'" not in script  # no raw whisper.md dump (it is in the ritual)


def test_old_loader_is_upgraded(tmp_path, monkeypatch):
    codex_home = tmp_path / ".codex"
    loader = codex_home / "bin" / "load-sk-agent-context.sh"
    loader.parent.mkdir(parents=True)
    old = render_loader_script().replace(
        "skmemory hook session-start --harness generic", "skmemory ritual --full"
    )
    loader.write_text(old, encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    ensure_codex_setup(agent_name="lumina")
    assert "skmemory hook session-start" in loader.read_text(encoding="utf-8")
    agents = (codex_home / "AGENTS.md").read_text(encoding="utf-8")
    assert "skmemory hook session-start --harness generic" in agents


def test_register_harness_hooks_uses_skmemory_api(monkeypatch):
    calls = []
    fake = types.ModuleType("skmemory.hook_install")
    fake.install = lambda target, dry_run=False: (
        calls.append((target, dry_run)) or {"claude": {"action": "unchanged"}}
    )
    monkeypatch.setitem(sys.modules, "skmemory.hook_install", fake)
    res = register.register_harness_hooks(["claude-code", "cursor", "pi", "codex"], dry_run=True)
    assert calls == [("claude,codex,pi", True)]
    assert res["harnesses"]["claude"]["action"] == "unchanged"


def test_register_harness_hooks_skips_without_harness():
    assert register.register_harness_hooks(["cursor", "vscode"])["action"] == "skip"
