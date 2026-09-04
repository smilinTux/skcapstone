# SKWorld install-and-converge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make skos the only installer, fleet objects the only desired-state, and Atlas the read-only reconciler that reports the delta, then use that chain to install skfleet-rotate, six correct gateway backends, and thirteen operator-signed agent identities on the `chef` estate.

**Architecture:** A skos capability pack (signed `skworld.module.json`) declares fleet objects. A new unit materializer turns a fleet Service object into a real systemd user unit, closing the one gap in the chain. The existing `fleet/converge.py` then owns that unit's lifecycle, and Atlas reports the delta between declared (skos registry), desired (fleet store) and actual (systemd).

**Tech Stack:** Python 3.12, click/typer CLIs, pytest, systemd user units, Node.js (skgateway `src/discovery/*.mjs`), capauth/GPG rsa4096, Syncthing-replicated `~/.skcapstone`.

**Spec:** `docs/superpowers/specs/2026-09-04-skworld-install-converge-design.md`

## Global Constraints

Every task's requirements implicitly include this section.

- **No em dashes or en dashes** in any output: code, comments, commit messages, docs, card text. Regular hyphens are fine. This is an absolute estate rule.
- **Commit BEFORE the long verification, never after.** Commit as soon as the code is written and a fast check passes; the message may say "verification pending". Then run the full suite and amend or add a follow-up. Never gate a commit on a background job.
- **Never work on the default branch.** Branch `fix/...` or `feat/...` first.
- **Commit attribution names the agent that actually did the work.** Never add a `Co-Authored-By` footer you cannot evidence.
- **Read state back through a different path than the one that wrote it.** A CLI printing success is not evidence. Five SK tools exit 0 and persist nothing.
- **`coord link` verdict recording is silently lossy.** It prints success with an empty value. Read verdicts back from `card_events`, never from the CLI line.
- **A 401 or 400 from a deny-by-default service is often the control working.** Read the body before calling anything broken.
- **`capauth init` exits 1 while still producing a valid keypair.** Check the result, not the exit code.
- **Never omit `--home` on `capauth`.** The default profile is inherited otherwise, and the identity becomes worthless.
- Python formatting: Black, CI pin 26.5.1. Lint: ruff (pinned).
- Every branch, loop, parser, or security path leaves exactly one runnable check behind. No frameworks, no fixtures beyond what exists.
- Estate paths: agent homes `~/.skcapstone/agents/<name>/`, fleet store under `~/.skcapstone/`, SK venv `~/.skenv/`.
- Operator identity: `chef`, fqid `<name>@chef.skworld.io`, GPG `BD7EEECA23D90A594400751CFDB582D9CB7272A6`.

---

## Phase 0: close the actuation gap

The spec's §3.2 flagged CronJob-to-systemd rendering as unverified. It has now been verified and **the gap is real**:

- `fleet/actuation.py` exposes only `systemd_state`, `systemd_start`, `systemd_restart`, `systemd_logs` (plus docker equivalents). There is no unit-file creation, no `enable`, no `daemon-reload`.
- `fleet/cron.py` docstring states "skscheduler wiring for CronJob is a later operational card". CronJob objects do not actuate.
- Unit files are currently created by per-repo imperative `install.sh` scripts shelled out from `fleet/install_backends.py`.

Phase 0 closes this with the smallest correct piece: materialize a fleet Service object into a systemd user unit. CronJob actuation is explicitly NOT built; `skfleet-rotate` is declared as a Service plus a timer, which is what chi actually runs (`Type=oneshot` service driven by `skfleet-rotate.timer`).

### Task 0.1: Unit materializer, pure rendering

**Files:**
- Create: `src/skcapstone/fleet/materialize.py`
- Test: `tests/test_fleet_materialize.py`

**Interfaces:**
- Consumes: `fleet.services.normalize_service_spec(spec) -> dict` (existing).
- Produces:
  - `render_service_unit(name: str, spec: dict) -> str`
  - `render_timer_unit(name: str, schedule: str) -> str`
  - `UnitRenderError(ValueError)`

Pure functions only. No filesystem, no subprocess. Task 0.2 owns side effects.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from skcapstone.fleet.materialize import (
    UnitRenderError,
    render_service_unit,
    render_timer_unit,
)


def test_render_service_unit_is_oneshot_with_verbatim_execstart():
    spec = {
        "unit": "skfleet-rotate.service",
        "runtime": "systemd-user",
        "exec": "%h/.skenv/bin/python3 %h/.local/bin/skfleet-rotate.py --go",
        "type": "oneshot",
        "environment": {"SKFLEET_TARGET": "3"},
        "description": "SKWorld fleet rotation",
    }
    text = render_service_unit("skfleet-rotate", spec)
    # Specifiers are systemd's to expand at start time, not ours at write time.
    assert "%h/.skenv/bin/python3 %h/.local/bin/skfleet-rotate.py --go" in text
    assert "Type=oneshot" in text
    assert "Environment=SKFLEET_TARGET=3" in text
    assert "Description=SKWorld fleet rotation" in text
    # A rendered base unit must never carry a bare reset; that is drop-in grammar.
    assert "\nExecStart=\n" not in text


def test_render_service_unit_refuses_missing_exec():
    with pytest.raises(UnitRenderError):
        render_service_unit("x", {"unit": "x.service", "runtime": "systemd-user"})


def test_render_service_unit_refuses_non_systemd_runtime():
    spec = {"unit": "x", "runtime": "docker", "exec": "/bin/true"}
    with pytest.raises(UnitRenderError):
        render_service_unit("x", spec)


def test_render_timer_unit_maps_interval_schedule():
    text = render_timer_unit("skfleet-rotate", "5m")
    assert "OnUnitActiveSec=5m" in text
    assert "Unit=skfleet-rotate.service" in text
    assert "WantedBy=timers.target" in text


def test_render_timer_unit_maps_named_schedule():
    assert "OnCalendar=hourly" in render_timer_unit("j", "@hourly")


def test_render_timer_unit_refuses_bad_schedule():
    with pytest.raises(UnitRenderError):
        render_timer_unit("j", "every-so-often")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/clawd/skcapstone-repos/skcapstone && python -m pytest tests/test_fleet_materialize.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'skcapstone.fleet.materialize'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Render a fleet Service object into systemd user unit text (pure).

The fleet actuation surface (``fleet/actuation.py``) can start and restart a
unit but cannot create one, and CronJob objects do not actuate at all
(``fleet/cron.py``: "skscheduler wiring for CronJob is a later operational
card"). Unit files have therefore come from per-repo imperative install.sh
scripts, which is the reason a documented fleet setup can be live on four
hosts and absent on the fifth.

This module renders. ``fleet/materialize_apply.py`` writes and reloads. The
split exists so the grammar is testable without touching the box.

Specifiers are copied VERBATIM. ``%h`` and friends belong to systemd at start
time, not to us at write time; resolving them here bakes one node's absolute
paths into an object meant to be placed on any node.
"""

from __future__ import annotations

import re

_SCHEDULE_NAMED = {"@hourly": "hourly", "@daily": "daily", "@weekly": "weekly"}
_SCHEDULE_INTERVAL = re.compile(r"^[1-9][0-9]*[mh]$")


class UnitRenderError(ValueError):
    """A Service spec cannot be rendered into a systemd unit."""


def render_service_unit(name: str, spec: dict) -> str:
    """Render the base .service text for a fleet Service object."""
    runtime = spec.get("runtime", "systemd-user")
    if runtime != "systemd-user":
        raise UnitRenderError(f"only systemd-user renders to a unit, got {runtime!r}")
    execstart = spec.get("exec")
    if not isinstance(execstart, str) or not execstart.strip():
        raise UnitRenderError(f"spec.exec is required for {name!r}")
    description = spec.get("description") or f"SKWorld fleet service {name}"
    stype = spec.get("type", "simple")
    lines = [
        "# Managed by skcapstone fleet materialize. Edits are overwritten.",
        f"# Fleet object: service/{name}",
        "",
        "[Unit]",
        f"Description={description}",
        "",
        "[Service]",
        f"Type={stype}",
    ]
    for key in sorted(spec.get("environment", {})):
        lines.append(f"Environment={key}={spec['environment'][key]}")
    lines.append(f"ExecStart={execstart}")
    if stype != "oneshot":
        lines.append(f"Restart={spec.get('restartPolicy', 'on-failure')}")
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def render_timer_unit(name: str, schedule: str) -> str:
    """Render the .timer text that drives ``<name>.service``."""
    if schedule in _SCHEDULE_NAMED:
        trigger = f"OnCalendar={_SCHEDULE_NAMED[schedule]}"
    elif _SCHEDULE_INTERVAL.match(schedule or ""):
        trigger = f"OnUnitActiveSec={schedule}"
    else:
        raise UnitRenderError(f"invalid schedule: {schedule!r}")
    return "\n".join(
        [
            "# Managed by skcapstone fleet materialize. Edits are overwritten.",
            f"# Fleet object: service/{name}",
            "",
            "[Unit]",
            f"Description=Run {name}",
            "",
            "[Timer]",
            trigger,
            "Persistent=true",
            f"Unit={name}.service",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fleet_materialize.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/fleet/materialize.py tests/test_fleet_materialize.py
git commit -m "feat(fleet): render a Service object into systemd unit text

Fleet actuation could start and restart a unit but never create one, and
CronJob objects do not actuate at all, so unit files came from per-repo
install.sh scripts. That is why the fleet setup is live on four chi hosts
and absent on noroc2027.

Pure rendering only; apply lands next. Specifiers are copied verbatim so a
rendered object stays placeable on any node.

Agent: <your agent name>"
```

### Task 0.2: Unit materializer, apply and reload

**Files:**
- Create: `src/skcapstone/fleet/materialize_apply.py`
- Test: `tests/test_fleet_materialize_apply.py`

**Interfaces:**
- Consumes: `render_service_unit`, `render_timer_unit`, `UnitRenderError` from Task 0.1.
- Produces:
  - `apply_unit(name: str, spec: dict, *, unit_dir: Path, runner: Runner, dry_run: bool = False) -> list[str]` returning the paths written (empty when unchanged).
  - `remove_unit(name: str, *, unit_dir: Path, runner: Runner) -> list[str]`

Idempotent: a second apply with an unchanged spec writes nothing and returns `[]`. This is what makes "install fresh" and "reconverge an existing node" the same command.

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path

from skcapstone.fleet.materialize_apply import apply_unit, remove_unit


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()


SPEC = {
    "unit": "demo.service",
    "runtime": "systemd-user",
    "exec": "%h/.skenv/bin/python3 -c pass",
    "type": "oneshot",
    "schedule": "5m",
}


def test_apply_writes_service_and_timer_then_reloads(tmp_path):
    r = FakeRunner()
    written = apply_unit("demo", SPEC, unit_dir=tmp_path, runner=r)
    assert sorted(Path(p).name for p in written) == ["demo.service", "demo.timer"]
    assert (tmp_path / "demo.service").read_text().startswith("# Managed by")
    assert "daemon-reload" in " ".join(" ".join(c) for c in r.calls)


def test_apply_is_idempotent(tmp_path):
    apply_unit("demo", SPEC, unit_dir=tmp_path, runner=FakeRunner())
    r2 = FakeRunner()
    assert apply_unit("demo", SPEC, unit_dir=tmp_path, runner=r2) == []
    # Nothing changed, so nothing is reloaded.
    assert r2.calls == []


def test_apply_rewrites_when_spec_changes(tmp_path):
    apply_unit("demo", SPEC, unit_dir=tmp_path, runner=FakeRunner())
    changed = dict(SPEC, exec="%h/.skenv/bin/python3 -c other")
    assert apply_unit("demo", changed, unit_dir=tmp_path, runner=FakeRunner())


def test_apply_dry_run_writes_nothing(tmp_path):
    r = FakeRunner()
    planned = apply_unit("demo", SPEC, unit_dir=tmp_path, runner=r, dry_run=True)
    assert planned
    assert not list(tmp_path.iterdir())
    assert r.calls == []


def test_apply_without_schedule_writes_only_service(tmp_path):
    spec = {k: v for k, v in SPEC.items() if k != "schedule"}
    written = apply_unit("demo", spec, unit_dir=tmp_path, runner=FakeRunner())
    assert [Path(p).name for p in written] == ["demo.service"]


def test_remove_unit_deletes_and_reloads(tmp_path):
    apply_unit("demo", SPEC, unit_dir=tmp_path, runner=FakeRunner())
    r = FakeRunner()
    removed = remove_unit("demo", unit_dir=tmp_path, runner=r)
    assert sorted(Path(p).name for p in removed) == ["demo.service", "demo.timer"]
    assert not list(tmp_path.iterdir())
    joined = " ".join(" ".join(c) for c in r.calls)
    assert "disable" in joined and "daemon-reload" in joined
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fleet_materialize_apply.py -v`
Expected: FAIL, `ModuleNotFoundError: skcapstone.fleet.materialize_apply`

- [ ] **Step 3: Write minimal implementation**

Write `src/skcapstone/fleet/materialize_apply.py`. Requirements, all pinned by the tests above:

1. `apply_unit` renders via Task 0.1, compares byte-for-byte against the existing file, and returns `[]` when every target is already identical. Only reload when something actually changed.
2. A `schedule` key in the spec produces the paired `.timer`; its absence produces only the `.service`.
3. `dry_run=True` returns the paths it would write and touches neither disk nor `runner`.
4. After a real write: `systemctl --user daemon-reload`, then `systemctl --user enable --now <name>.timer` when a timer exists, else `enable <name>.service`.
5. `remove_unit` disables first, then unlinks, then reloads. Disable before unlink, or systemd keeps a dangling symlink in `default.target.wants`.
6. `unit_dir` defaults to `Path.home() / ".config/systemd/user"` but is always injectable, which is what makes the tests hermetic.
7. Write atomically: write to `<path>.tmp` in the same directory, then `os.replace`. A half-written unit file that systemd reads during reload is a live failure.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fleet_materialize_apply.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/fleet/materialize_apply.py tests/test_fleet_materialize_apply.py
git commit -m "feat(fleet): apply a rendered Service object to systemd, idempotently

Writes atomically, reloads only when bytes actually changed, and disables
before unlink so no dangling wants symlink survives a removal.

Idempotence is the point: install-fresh and reconverge are one command.

Agent: <your agent name>"
```

### Task 0.3: Wire materialize into `skcapstone fleet install`

**Files:**
- Modify: `src/skcapstone/fleet/cli.py:455` (`install_cmd`)
- Test: `tests/test_fleet_install_materialize.py`

**Interfaces:**
- Consumes: `apply_unit`, `remove_unit` from Task 0.2.
- Produces: `skcapstone fleet install --check` reports units that would be written; `--apply` writes them.

`--check` stays the default, per the existing command contract. A node must be able to show its drift without changing anything.

- [ ] **Step 1: Write the failing test**

```python
from click.testing import CliRunner

from skcapstone.fleet.cli import fleet


def test_install_check_reports_missing_unit_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    result = CliRunner().invoke(fleet, ["install", "--check"])
    assert result.exit_code == 0
    assert "unit" in result.output.lower()


def test_install_apply_is_reported_as_applied(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    result = CliRunner().invoke(fleet, ["install", "--apply"])
    assert result.exit_code == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fleet_install_materialize.py -v`
Expected: FAIL, the check output does not mention units.

- [ ] **Step 3: Implement**

In `install_cmd`, after the existing backend diff, load this node's Service objects from the fleet store, call `apply_unit(..., dry_run=not apply)` for each, and print one line per unit: `write <name>.service`, `write <name>.timer`, or `ok <name> (unchanged)`. Preserve the existing exit-code contract exactly.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_fleet_install_materialize.py tests/test_fleet_materialize.py tests/test_fleet_materialize_apply.py -v`
Expected: all pass

- [ ] **Step 5: Commit, then run the full suite**

```bash
git add -A && git commit -m "feat(fleet): materialize units during fleet install (verification pending)

Agent: <your agent name>"
python -m pytest tests/ -x -q     # then amend or follow up with any fix
```

- [ ] **Step 6: Prove the chain end to end on this node**

This is the card the whole plan rests on. Evidence, not assertion:

```bash
# 1. Declare a throwaway Service object, apply it.
skcapstone fleet apply --file /tmp/demo-service.json
skcapstone fleet install --apply

# 2. Read the result back through systemd, NOT through the CLI that wrote it.
systemctl --user cat skfleet-demo.service
systemctl --user list-timers --all | grep skfleet-demo

# 3. Remove it and confirm systemd agrees it is gone.
skcapstone fleet apply --file /tmp/demo-service-deleted.json
skcapstone fleet install --apply
systemctl --user cat skfleet-demo.service   # must now fail
```

Record the exact output on the card. If step 2 does not show the unit, Phase 0 is not done and no later phase may start.

---

## Phase 1: skgateway, six correct backends

Config lives at `~/clawd/skcapstone-repos/skgateway/config/skgateway.yaml`. The service listens on `:18780`. Restart via its systemd unit; note that SIGHUP does not rebuild the router (see `reference_skgateway_sighup_no_router_rebuild`), so restart rather than reload.

### Task 1.1: Provision the OpenRouter credential

**Files:**
- Create: `~/.config/environment.d/openrouter.conf` (mode 0600)
- Modify: `config/skgateway.yaml` (the `openrouter` backend stanza)

The key currently lives at `~/api-keys/openrouter.env`, which is not env-file format: line 1 is `key: lumina`, line 2 is the bare key. Verified working 2026-09-04: 427 models, 26 free-tier.

- [ ] **Step 1: Normalize into a real env file**

```bash
umask 077
mkdir -p ~/.config/environment.d
printf 'OPENROUTER_API_KEY=%s\n' \
  "$(sed -n '2p' ~/api-keys/openrouter.env | tr -d '[:space:]')" \
  > ~/.config/environment.d/openrouter.conf
chmod 600 ~/.config/environment.d/openrouter.conf
```

- [ ] **Step 2: Verify the key is live and count the free tier**

```bash
set -a; . ~/.config/environment.d/openrouter.conf; set +a
curl -s -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  https://openrouter.ai/api/v1/models | python3 -c "
import json,sys
m=json.load(sys.stdin)['data']
free=[x['id'] for x in m if str(x.get('pricing',{}).get('prompt','1')) in ('0','0.0','-1')]
print('total',len(m),'free',len(free))
assert len(m)>100, 'catalog too small, key may be rejected'
"
```

Expected: `total 427 free 26` or close. A 401 here means the key, not the plumbing.

- [ ] **Step 3: Flip the stanza on**

In `config/skgateway.yaml`, set `openrouter.enabled: true`, keep `api_key_env: OPENROUTER_API_KEY`, `discovery: free`, `priority: 4`.

- [ ] **Step 4: Restart and confirm the models are advertised**

```bash
systemctl --user restart skgateway
sleep 5
curl -s localhost:18780/v1/models | python3 -c "
import json,sys
ids=[m['id'] for m in json.load(sys.stdin)['data']]
print(len(ids),'models'); print([i for i in ids if ':free' in i][:5])
"
```

- [ ] **Step 5: Commit** (config repo, branch first, secret NEVER committed)

```bash
cd ~/clawd/skcapstone-repos/skgateway
git checkout -b feat/openrouter-enable
git add config/skgateway.yaml
git diff --cached | grep -i 'sk-or-v1' && { echo "SECRET IN DIFF, ABORT"; exit 1; }
git commit -m "feat(config): enable the OpenRouter free-tier backend

Key is provisioned via OPENROUTER_API_KEY from environment.d, never in the
config tree. Verified live: 427 models, 26 free-tier.

Agent: <your agent name>"
```

### Task 1.2: Repair the three broken backend endpoints

**Files:** Modify `config/skgateway.yaml`.

Three defects, each independently verifiable:

1. `chiap08-qwen38.url` is the literal unsubstituted string `http://TAILNET_HOST:11439/v1`. Replace with the real tailnet host.
2. `ollama` points at `192.168.0.100:11434`, which has been dead 47 hours and is really the ornith host. Rename the backend to `ornith`, point it at the ornith endpoint, and declare `ornith-1.5-9b`.
3. `chiap01-qwen38` at `http://chiap01:18810/v1` needs confirming, not assuming.

- [ ] **Step 1: Probe all three before editing anything**

```bash
for u in http://chiap08:11439/v1 http://chiap01:18810/v1 http://192.168.0.100:11434/v1; do
  echo "== $u"; curl -s --max-time 6 "$u/models" | head -c 300; echo
done
```

Record which answer. Do not configure a backend that did not answer; declaring a dead backend is exactly what failed skoperator before.

- [ ] **Step 2: Edit the stanzas** to match what actually answered, `models:` listing only ids the endpoint really serves.

- [ ] **Step 3: Restart and verify each backend is observed**

```bash
systemctl --user restart skgateway; sleep 8
curl -s localhost:18780/health | python3 -c "
import json,sys
b=json.load(sys.stdin)['backends']
for k,v in b.items(): print(f\"{k:20} status={v['status']:10} observed={v['observed']}\")
"
```

- [ ] **Step 4: Commit.**

### Task 1.3: Make health actually observed

Every backend currently reports `observed:false`. Per `docs/fleet/lane-admission-health.md`, a lane admits work only when its capacity domain was observed `up` or `degraded`, was not quarantined, and reports positive queue capacity. **With `observed:false` everywhere, this node admits zero cards even once the rotator is installed.** This task is a hard prerequisite for Phase 2.

- [ ] **Step 1: Find why the probe never marks observed.** Start at `src/discovery/probe.mjs` and the health sweep scheduler. Determine whether the sweep runs at all on this host, or runs and fails to persist.
- [ ] **Step 2: Write one regression check** that fails while `observed` stays false after a completed sweep.
- [ ] **Step 3: Fix the smallest cause.** Do not add a second publisher; the snapshot contract says no separate publisher, service, or timer is required.
- [ ] **Step 4: Verify through `/health` and `/queue`,** and confirm `~/.skcapstone/evidence/fleet-lane-health.json` is written and under 120 seconds old.
- [ ] **Step 5: Commit.**

### Task 1.4: Capability battery on nvidia and openrouter

The mechanism exists: `src/discovery/capability-assessment.mjs` exposes `runCapabilityAssessment`, and `probe.mjs` already rides the battery on the health sweep so it costs no extra request. `classify.mjs` refuses to infer capability from a model name, which is correct and must not be weakened.

- [ ] **Step 1: Enable the battery** for the `nvidia` and `openrouter` backends.
- [ ] **Step 2: Verify `measured_capabilities` is populated** on the lifecycle records for both, including tool support.
- [ ] **Step 3: Confirm the 59 NVIDIA "unknown capability" models resolve** to measured values, or record precisely why a given model cannot be assessed.
- [ ] **Step 4: Commit.**

### Task 1.5: Capability-derived buckets

- [ ] **Step 1: Make bucket membership a function of `measured_capabilities`,** not a hand-maintained list. A model enters the tool-capable bucket only on measured evidence.
- [ ] **Step 2: Write the regression test:** a model with no measured tool support must never land in a tool-capable bucket, even if its name suggests otherwise.
- [ ] **Step 3: Commit.**

### Task 1.6: Lane order and capacity domains

Declare the lane order `qwen -> ornith -> nim -> openrouter -> claude`, `claude` last because it is the only paid capacity.

- [ ] **Step 1: Configure the capacity domains** so each lane maps to its backends.
- [ ] **Step 2: Preserve the claude route exactly:** skgateway `:18780` to claude-code-api `:18782`. Direct `api.anthropic.com` 400s the subscription token for third-party apps. Add a regression test pinning this route.
- [ ] **Step 3: Verify a real completion on each lane,** cheapest first, and record the model that answered.
- [ ] **Step 4: Commit.**

---

## Phase 2: skos packs

`skos` pack anatomy, from `src/skos/packs/model.py`: a signed `skworld.module.json` whose `install` facet carries steps of six kinds, executed in the fixed order `sql_migration -> db_roles -> content_repo -> seed -> fleet_objects -> doctor`. Reference pack: `src/skos/packs/skbrain/`, which carries the manifest plus two fleet CronJob specs. The provisioner is already idempotent, resumable and all-or-nothing.

### Task 2.1: Lay `SK_DATA_ROOT` on noroc2027

- [ ] **Step 1:** Set `SK_DATA_ROOT` in `~/.config/environment.d/skos.conf`.
- [ ] **Step 2:** `skos init`, then `skos install --profile personal`.
- [ ] **Step 3:** Verify by reading the tree back with `skos path memory` and `ls`, not by trusting the installer's exit code.
- [ ] **Step 4:** Commit any config change.

### Task 2.2: The `skfleet` pack

Declares `skfleet-rotate` as a Service object with a `5m` schedule, materialized by Phase 0.

Lane targets for this estate, respecting that there is no z.ai and no Kimi here:

```
SKFLEET_TARGET=4
SKFLEET_QWEN_TARGET=2
SKFLEET_QWEN_MODEL=qwen3.8-27b-huihui-abliterated-q4_k_m
SKFLEET_GLM_TARGET=0
SKFLEET_MAX_LAUNCH=4
```

`SKFLEET_GLM_TARGET=0` is deliberate and must be commented as such in the pack: the GLM lane exists in the rotator but this estate has no z.ai connection, so its target is zero rather than the stanza being deleted.

- [ ] **Step 1:** Author `skworld.module.json` with a `fleet_objects` step.
- [ ] **Step 2:** `skos install skfleet`, then `skcapstone fleet install --apply`.
- [ ] **Step 3:** Verify through systemd directly: `systemctl --user list-timers | grep skfleet-rotate`.
- [ ] **Step 4:** Run one rotation manually and read the lane-health evidence file.
- [ ] **Step 5:** Commit.

### Task 2.3: The `skgateway` pack

- [ ] Declare skgateway as a Service object so its unit is materialized rather than hand-installed. Verify, then commit.

### Task 2.4: The `seats` pack

- [ ] Declare the per-seat runtime objects. Placement only; identities are Phase 3. Verify, then commit.

### Task 2.5: Replicate to node-41

- [ ] Lay `SK_DATA_ROOT` on node-41, install the same packs, and confirm `skcapstone fleet install --check` reports zero drift on both nodes.

### Task 2.6: Retire one imperative install path

- [ ] Pick exactly one `install.sh` now covered by a pack, delete it, and prove the pack still produces the same unit. One is the point: it proves the migration is real without a big-bang rewrite.

---

## Phase 3: thirteen identities

Ceremony is `docs/fleet/standing-up-a-seat.md`, with `chef` substituted for `casey` throughout. Names must match `^[a-z][a-z0-9-]{0,31}$`.

Seats (ADR-0006, ratified for this estate): `niobe` dispatcher, `tank` release and install, `seraph` verification only, `link` integrator and arbitration, `mero` overseer, `atlas` operations (frozen, provisioned last).

Role agents: `veritas`, `implementer`, `tester`, `cli-reviewer`, `dependency-worker`, `fleet-review-closer`, `fleet-lifecycle-reconciler`.

`jarvis` receives no seat on this estate.

### Task 3.1: Mint the six seats

- [ ] **Step 1:** For each seat, generate the passphrase and run `capauth --home <D> init` per the spec §4.3 block. Never omit `--home`.
- [ ] **Step 2:** After each, derive the fingerprint from `identity/public.asc` via a throwaway GPG homedir, never from `profile.json`.
- [ ] **Step 3:** Assert all thirteen fingerprints are pairwise distinct. A collision means stop and start that seat over.
- [ ] **Step 4:** Commit the non-secret profile records. Passphrases stay in `~/capauth-pass/`, mode 0600, never committed.

### Task 3.2: Operator-sign every identity

- [ ] **Step 1:** Assemble a temporary operator home holding both key halves in a `700` tmpdir, sign, then destroy it.
- [ ] **Step 2:** Two checks per identity, both required: the signature must verify against the operator public key, and must **fail** against a tampered payload. A verify that only ever returns True proves nothing.
- [ ] **Step 3:** Write `operator`, `operator_fingerprint`, `operator_signed_at` and `fqid` into each `profile.json`, storing the signature at `identity/operator-attestation.json`.

### Task 3.3: Mint the seven role agents

- [ ] Same ceremony, full capauth rather than lightweight profiles. Rationale is in spec §4.2: a `capauth_managed: false` profile cannot sign, so `veritas` could not prove it is Veritas, and separation of duties that cannot be attributed is a naming convention rather than a control.

### Task 3.4: Register seat boundaries

- [ ] **Step 1:** Configure enforcement so `seraph` is rejected on merge, dispatch and actuation; `mero` on every mutation; `link` on fleet claims and launches.
- [ ] **Step 2:** Write one negative test per boundary. A boundary with no failing case is undefended.
- [ ] **Step 3:** Commit.

---

## Phase 4: drift purge

Read `reference_coord_dir_syncthing_fleet` first: `~/.skcapstone` is ONE Syncthing folder, so a destructive coordination operation needs every node rolled, or a deleted object returns from another replica.

### Task 4.1: Purge junk fleet placements

- [ ] Remove `job/d`, `job/demo`, `job/t-1`, `job/keep` and the other placements that map to no real workload. Confirm each corresponds to nothing before deleting. Verify `skcapstone fleet placements` afterward.

### Task 4.2: Retire node-ollama, archive scratch agent homes

- [ ] **Step 1:** `node-ollama` has been Dead 171605 seconds while still advertising `gpu=true`. Either revive it or drain and remove it, and drop the stale label either way.
- [ ] **Step 2:** Archive the roughly twenty scratch homes (`sovereign-test`, `opus-swarm-varus-urls`, `sonnet-worker-1`, `sonnet-worker-2`, and similar) to a dated tarball before deleting. Preserve archives as real files.
- [ ] **Step 3:** Never delete a home holding key material without checking `identity/` first.

### Task 4.3: Resolve the four failed units

- [ ] `push-pending`, `skchat-app-web`, `skchat-health-probe`, `skcot-cert-renew`. For each: fix it, or disable it with a recorded reason. A permanently failed unit that nobody intends to fix is noise that hides the next real failure.

---

## Phase 5: Atlas, read-only, last

Atlas gains **no new authority**. The delta report is pure observation and sits inside the existing Atlas Constitution, which is what reconciles the transition card's "ATLAS remains frozen and last" with standing Atlas up as the ops seat.

### Task 5.1: The three-registry reconciler

**Files:**
- Create: `src/skcapstone/atlas/reconcile.py`
- Test: `tests/test_atlas_reconcile.py`

**Interfaces:**
- Produces: `reconcile(paths, node) -> ReconcileReport` with `declared`, `desired`, `actual`, and `deltas` as typed lists.

Reads three registries: the skos capability registry (declared), fleet objects (desired), systemd units (actual). Writes nothing, actuates nothing.

- [ ] **Step 1:** Write the failing test covering the four delta classes: declared-but-not-desired, desired-but-not-actual, actual-but-not-desired (the `job/demo` class), and full agreement.
- [ ] **Step 2:** Implement as pure functions over injected readers, so the test needs no live systemd.
- [ ] **Step 3:** Verify against the real box; the report must name the actual drift found in Phase 4 if any remains.
- [ ] **Step 4:** Commit.

### Task 5.2: Surface it through `atlas eyes`

- [ ] Add the delta to the existing read-only `skcapstone atlas eyes` pass. Assert in a test that no code path in `atlas/` calls an actuation verb.

### Task 5.3: Schedule it and publish to the dashboard

- [ ] **Step 1:** Declare the Atlas eyes job as a Service object with a schedule, materialized through the Phase 0 chain, which dogfoods the whole architecture.
- [ ] **Step 2:** Publish the estate view on the skcapstone dashboard at `:7778`.
- [ ] **Step 3:** Verify the report renders and its numbers match a hand-run `atlas eyes`.
- [ ] **Step 4:** Commit.

---

## Self-review

**Spec coverage.** §3.1 install chain covered by 0.1 to 0.3 and 2.2. §3.2 unverified link resolved and now implemented by Phase 0. §3.3 Atlas reconciler covered by 5.1 to 5.3. §4 seat model by 3.1 to 3.4. §4.2 role agents by 3.3. §4.3 ceremony by 3.1 and 3.2. §5.1 backends by 1.1 and 1.2. §5.2 lanes by 1.6. §5.3 auto-bucketing by 1.4 and 1.5. §5.4 health observation by 1.3. §6 worker runtime: pi stays default, skharness untouched, no task modifies it, which satisfies the requirement by construction. §7 phases match. §9 verification contract is in Global Constraints. §10 B1 closed by 1.1, B2 closed by Phase 0.

**Gap found and closed:** the spec's §7 listed Phase 0 as two investigation cards. Investigation is now complete, so Phase 0 became three implementation tasks. Phase 2 gained Task 2.6 so at least one imperative `install.sh` is actually retired; without it the plan would add a declarative path while leaving every imperative one in place, and the installation gap would persist under a new name.

**Type consistency:** `render_service_unit` / `render_timer_unit` / `UnitRenderError` defined in 0.1 are used under those exact names in 0.2. `apply_unit` / `remove_unit` defined in 0.2 are used under those names in 0.3. `reconcile` in 5.1 is consumed in 5.2.

**Counts:** 25 tasks. Phase 0 has 3, Phase 1 has 6, Phase 2 has 6, Phase 3 has 4, Phase 4 has 3, Phase 5 has 3.
