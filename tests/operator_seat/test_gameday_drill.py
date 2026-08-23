"""CR-9.1 AC1 gameday drill (dry): a telegram-bridge wedge self-heals correctly.

Injects a controlled, reversible telegram-bridge wedge (a stale poll heartbeat
while the daemon is up, the ConnectTimeout silent-wedge signature), then proves,
in OBSERVE/dry mode (execute stays off, nothing is physically restarted):

  1. the real probe path DETECTS it (BridgeAlive fires False),
  2. the brief flags it firing,
  3. the self-heal ACTION is correctly computed (restart-telegram-bridge, auto,
     mapping through actuator.honor to `systemctl --user restart
     skchat-telegram-<agent>.service` WITHOUT executing it),
  4. the ITIL change record is correct (normal, auto-normal, no human required),
  5. the Telegram report carries the firing + the fix.

This is the deterministic encoding of the live gameday drill run for the flip.
"""

from __future__ import annotations

import subprocess
import time

from skcapstone.fleet.paths import FleetPaths
from skcapstone.operator_seat import (
    act_dispatch,
    brief,
    fleet_adapter,
    itil_intent,
    notify,
    plan,
    skchat_adapter,
)


def _paths(tmp_path):
    return FleetPaths(root=tmp_path / "fleet")


def _inject_bridge_wedge(monkeypatch, tmp_path):
    """The controlled fault: daemon UP + poll heartbeat older than the threshold."""

    # Daemon health probe reports UP (so the wedge detector engages, not defers).
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true, "dataplane_auth": true, "webrtc_signaling": "ok"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    # A heartbeat file whose mtime is 20 minutes old (> the 600s wedge threshold).
    hb = tmp_path / "telegram_poll.ts"
    hb.write_text("stale")
    old = time.time() - 1200
    import os

    os.utime(hb, (old, old))
    monkeypatch.setenv("SKCHAT_BRIDGE_HEARTBEAT", str(hb))
    monkeypatch.setenv("SKCOMMS_OUTBOX_DIR", str(tmp_path / "empty-outbox"))


def test_gameday_wedge_detected_and_self_heal_computed_dry(monkeypatch, tmp_path):
    _inject_bridge_wedge(monkeypatch, tmp_path)

    # 1. DETECT: the real probe path fires BridgeAlive False.
    obs = skchat_adapter.skchat_observe()
    by_type = {c["type"]: c for c in obs["conditions"]}
    assert by_type["BridgeAlive"]["status"] == "False"
    assert by_type["BridgeAlive"]["object"] == "telegram-bridge"
    # Nothing else spuriously fired (daemon up, auth on, outbox empty, calling ok).
    assert by_type["DaemonReady"]["status"] == "True"

    # 2. BRIEF: it shows as firing (skchat conds are health-type: fire on False).
    the_brief = brief.build_brief(
        {"skchat": obs["conditions"]}, set(fleet_adapter.PROBLEM_WHEN_TRUE)
    )
    assert the_brief["quiet"] is False
    firing = {(c["app"], c["type"], c.get("object")) for c in the_brief["firing"]}
    assert ("skchat", "BridgeAlive", "telegram-bridge") in firing

    # 3. SELF-HEAL ACTION computed + classified auto (what the brain proposes).
    proposal = {
        "action": "restart-telegram-bridge",
        "object": "telegram-bridge",
        "change_class": "normal",
        "rationale": "bridge poll heartbeat is stale while the daemon is up (silent wedge)",
        "ts": "drill",
    }
    planned = plan.plan_actions([proposal], act_dispatch.merged_explain())
    assert planned[0]["disposition"] == "auto"
    assert planned[0]["classification"]["change_class"] == "normal"

    # 4a. The action maps through actuator.honor to the per-agent bridge unit,
    #     captured (dry) not executed.
    calls = []

    def capture(cmd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    apply_fn = act_dispatch.build_apply_fn(_paths(tmp_path), "drill", runner=capture, itil=None)
    result = apply_fn(proposal, planned[0]["classification"])
    assert result["adapter"] == "skchat"
    assert result["actuation"]["performed"] is True
    unit = skchat_adapter._unit_for("restart-telegram-bridge")
    assert unit.startswith("skchat-telegram-") and unit.endswith(".service")
    assert any(c == ["systemctl", "--user", "restart", unit] for c in calls)

    # 4b. The ITIL change record is correct: normal + auto-normal, no human gate.
    record = itil_intent.build_change_record(
        {"name": "restart-telegram-bridge"},
        planned[0]["classification"],
        dry_run="true",
        rollback_plan="revert via controller reconcile",
    )
    assert record["change_class"] == "normal"
    assert "auto-normal" in record["tags"]
    assert record["requires_human"] is False

    # 5. The Telegram report carries the firing condition and the fix.
    sent = []
    report = brief_report(the_brief, [proposal])
    ok = notify.notify_report(report, sender=lambda t: sent.append(t) or True)
    assert ok is True
    assert "BridgeAlive" in sent[0]
    assert "restart-telegram-bridge" in sent[0]


def brief_report(the_brief, proposals):
    """A human report over the firing brief and the proposed fixes."""
    firing_lines = "\n".join(
        f"  {c['app']}: {c['type']}={c['status']}" for c in the_brief["firing"]
    )
    prop_lines = "\n".join(
        f"  {p['change_class']}, {p['action']}, {p['rationale']}" for p in proposals
    )
    return f"firing conditions:\n{firing_lines}\nproposals:\n{prop_lines}"
