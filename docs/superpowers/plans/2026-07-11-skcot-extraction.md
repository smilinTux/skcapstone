# skcot Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the CoT/TAK subsystem out of skcomms core into a standalone `skcot` package so a domain protocol can never again flood core durable mailboxes.

**Architecture:** Move the 7 CoT/geo leaf modules (nothing in skcomms core imports them) into a new `skcot` package that depends on skcomms. The "no durable mailbox for beacons" property comes by composition (short-TTL + peer-gate + consume-to-GEO_STORE-and-delete), not a new pipe. skcot auto-advertises the `cot` capability so the peer gate self-configures.

**Tech Stack:** Python >=3.10, setuptools, pytest, skcomms (dependency), takproto (optional `[tak]` extra), systemd user units.

## Global Constraints
- Package name `skcot`; remote `github.com/smilinTux/skcot`; installs into `~/.skenv`.
- `install_requires = ["skcomms"]`; `extras_require = {"tak": ["takproto"]}`; `requires-python = ">=3.10"`.
- skcomms MUST NOT import skcot (one-way leaf dependency). No import shim in skcomms.
- Run tests with `~/.skenv/bin/python -m pytest` from `~`.
- Commit trailer: `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`.
- Spec: `docs/superpowers/specs/2026-07-11-skcot-extraction-design.md`.

---

### Task 1: Scaffold the skcot package

**Files:**
- Create: `~/clawd/skcapstone-repos/skcot/pyproject.toml`
- Create: `~/clawd/skcapstone-repos/skcot/src/skcot/__init__.py`
- Create: `~/clawd/skcapstone-repos/skcot/tests/test_smoke.py`

**Interfaces:**
- Produces: an importable `skcot` package (`skcot.__version__`).

- [ ] **Step 1:** `git init` the repo and add remote.
```bash
mkdir -p ~/clawd/skcapstone-repos/skcot/src/skcot ~/clawd/skcapstone-repos/skcot/tests
cd ~/clawd/skcapstone-repos/skcot && git init -q && git remote add origin https://github.com/smilinTux/skcot.git
```
- [ ] **Step 2:** Write `pyproject.toml` modeled on skcomms (setuptools backend, `[project]` name=skcot, version=0.1.0, requires-python>=3.10, `dependencies=["skcomms"]`, `[project.optional-dependencies] tak=["takproto"]`, `[tool.setuptools.packages.find] where=["src"]`, and `[tool.pytest.ini_options] pythonpath=["src"]`).
- [ ] **Step 3:** Write `src/skcot/__init__.py` with `__version__ = "0.1.0"`.
- [ ] **Step 4:** Write the smoke test:
```python
def test_import_skcot():
    import skcot
    assert skcot.__version__ == "0.1.0"
```
- [ ] **Step 5:** Editable-install + run: `~/.skenv/bin/pip install -e ~/clawd/skcapstone-repos/skcot && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skcot/tests/test_smoke.py -v` → PASS.
- [ ] **Step 6:** Commit `chore(skcot): scaffold standalone package`.

---

### Task 2: Move the 7 CoT/geo modules and fix imports

**Files (move from skcomms `src/skcomms/` to skcot `src/skcot/`, renaming):**
- `cot.py`→`codec.py`, `cot_server.py`→`server.py`, `cot_service.py`→`service.py`, `cot_agent.py`→`agent.py`, `cot_client.py`→`client.py`, `cot_pki.py`→`pki.py`, `geo.py`→`geo.py`

**Interfaces:**
- Consumes: skcomms public API (`from skcomms.envelope import Envelope`, `from skcomms.core import SKComms`, `from skcomms.discovery import PeerInfo`, `send_federated`).
- Produces: `skcot.codec` (`CotEvent`, `to_cot`, `parse_cot`, `is_ephemeral_beacon`, `cot_to_envelope`), `skcot.server` (`CotStreamServer`, `federation_ingest`, `_cot_peer_fqids`), `skcot.service` (`main`), `skcot.agent`, `skcot.client`, `skcot.pki`, `skcot.geo` (`GEO_STORE`).

- [ ] **Step 1:** Copy each module into `skcot/src/skcot/` with the new name (do NOT delete from skcomms yet — Task 8 does the removal after skcot is green, so skcomms stays working meanwhile).
- [ ] **Step 2:** Fix intra-package imports: replace `from .cot import` / `from .cot_server import` etc. with the new names (`from .codec import`, `from .server import`, ...). Leave `from skcomms... import` lines untouched (those are the correct upstream dependency). Grep to confirm: `grep -rn "from .cot\|import cot_\|skcomms.cot" src/skcot` returns nothing.
- [ ] **Step 3:** Verify import graph: `~/.skenv/bin/python -c "import skcot.codec, skcot.server, skcot.service, skcot.agent, skcot.client, skcot.pki, skcot.geo; print('ok')"` → `ok`. If takproto missing, `skcot.codec` must still import (lazy import preserved).
- [ ] **Step 4:** Confirm no reverse dependency: `grep -rn "import skcot\|from skcot" ~/clawd/skcapstone-repos/skcomms/src` → empty.
- [ ] **Step 5:** Commit `feat(skcot): move CoT/geo modules from skcomms (copy phase)`.

---

### Task 3: Move the CoT tests and get them green under skcot

**Files:**
- Move: `skcomms/tests/{test_cot_codec,test_cot_server,test_cot_pki,test_cot_agent_nameagnostic,test_cot_beacon_outbox,test_geo}.py` → `skcot/tests/` (renaming imports to `skcot.*`).
- Leave in skcomms (generic routing/send, not CoT-internal): `test_cot_beacon_default_delivery.py`, `test_cot_beacon_ephemeral_routing.py`, `test_core_inbound_beacon_skip.py` — but re-point their CoT-object imports (`is_ephemeral_beacon`, `cot_to_envelope`) to `skcot.codec`. If that creates a skcomms-test->skcot dependency, MOVE them to skcot instead (skcomms tests must not import skcot). Decide per-file: a test that asserts skcomms router behavior stays and imports skcot only as a test fixture is acceptable ONLY if skcot is a test-time dep; if in doubt, move it to skcot.

**Interfaces:**
- Consumes: the moved `skcot.*` modules.

- [ ] **Step 1:** Move the clearly-CoT-internal test files, rewriting `from skcomms.cot...`→`from skcot...`.
- [ ] **Step 2:** Run `~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skcot/tests -v`. Fix import paths until green.
- [ ] **Step 3:** For the 3 borderline files, apply the decision rule above; ensure skcomms's own suite has no `import skcot` (`grep -rn "skcot" ~/clawd/skcapstone-repos/skcomms/tests` → only allowed if skcot is declared a skcomms test-dep, otherwise move).
- [ ] **Step 4:** Commit `test(skcot): relocate CoT test suite`.

---

### Task 4: Auto-advertise the `cot` capability (TDD — property test 1)

**Files:**
- Modify: `skcot/src/skcot/service.py` (startup path in `main()`)
- Create: `skcot/tests/test_capability_advertise.py`

**Interfaces:**
- Consumes: skcomms discovery self-peer publish path (locate it: `grep -n "def .*publish\|write.*peer\|self" ~/clawd/skcapstone-repos/skcomms/src/skcomms/discovery.py`; the local node's PeerInfo is where `capabilities` must gain `"cot"`).
- Produces: `skcot.service.advertise_cot_capability(sk) -> None`.

- [ ] **Step 1:** Write the failing test:
```python
def test_skcot_advertises_cot_capability(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOMMS_HOME", str(tmp_path))
    from skcot.service import advertise_cot_capability
    from skcomms.core import SKComms
    sk = SKComms.from_config()
    advertise_cot_capability(sk)
    # the local node's published PeerInfo now lists "cot"
    from skcomms.discovery import load_self_peer  # or the actual accessor found in step Interfaces
    assert "cot" in load_self_peer().capabilities
```
- [ ] **Step 2:** Run → FAIL (`advertise_cot_capability` undefined).
- [ ] **Step 3:** Implement `advertise_cot_capability(sk)` that reads the local node's PeerInfo via the discovery accessor found above, adds `"cot"` to `capabilities` (idempotent), and writes it back. Call it from `service.main()` after `SKComms.from_config()`.
- [ ] **Step 4:** Run → PASS. Adjust the test's accessor name to the real discovery API if needed.
- [ ] **Step 5:** Commit `feat(skcot): auto-advertise cot capability on startup`.

---

### Task 5: Beacon-never-persists-durably (TDD — property test 2, the whole point)

**Files:**
- Create: `skcot/tests/test_beacon_no_durable_persist.py`
- Modify (only if the property is not already met by composition): `skcot/src/skcot/service.py` inbound handler.

**Interfaces:**
- Consumes: `skcot.codec.cot_to_envelope`, `skcot.geo.GEO_STORE`, skcomms inbound receive.

- [ ] **Step 1:** Write the failing test: deliver a CoT `a-*` beacon envelope to a node running skcot's inbound consumer; assert (a) `GEO_STORE` has the entity, (b) no `*.skc.json` remains in the node's comms inbox after a consume cycle.
```python
def test_beacon_lands_in_geostore_and_leaves_no_durable_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOMMS_HOME", str(tmp_path))
    from skcot.codec import CotEvent, CotPoint, cot_to_envelope
    from skcot.geo import GEO_STORE
    from skcot.service import consume_cot_inbound  # the receive hook
    ev = CotEvent(uid="X-1", type="a-f-G-U-C", point=CotPoint(lat=41.1, lon=-73.4))
    env = cot_to_envelope(ev, from_fqid="peer@x")
    consume_cot_inbound(env)
    assert GEO_STORE.get("X-1") is not None
    inbox = tmp_path / "inbox"
    assert not list(inbox.rglob("*.skc.json"))  # nothing durable retained
```
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `consume_cot_inbound(env)`: if `env.content_type == "application/cot+xml"`, parse via `skcot.codec`, `GEO_STORE.upsert_from_cot(...)`, and delete the source file if one exists; never write to a durable inbox. Wire it as the skcot service's inbound handler for `application/cot+xml`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(skcot): consume CoT beacons into GEO_STORE with no durable retention`.

---

### Task 6: Codec round-trip (TDD — property test 3)

**Files:**
- Create: `skcot/tests/test_codec_roundtrip.py`

- [ ] **Step 1:** Write the test: XML→`parse_cot`→`to_cot`→`parse_cot` preserves uid/type/lat/lon/callsign; and `is_ephemeral_beacon` is True for `a-*`, False for `b-*`.
```python
def test_cot_xml_roundtrip_and_ephemeral_classification():
    from skcot.codec import parse_cot, to_cot, is_ephemeral_beacon
    xml = ('<event version="2.0" uid="J-1" type="a-f-G-U-C" how="m-g" '
           'time="2026-07-11T00:00:00Z" start="2026-07-11T00:00:00Z" stale="2026-07-11T00:05:00Z">'
           '<point lat="41.1375" lon="-73.424" hae="10" ce="5" le="5"/>'
           '<detail><contact callsign="JARVIS"/></detail></event>')
    ev = parse_cot(xml.encode())
    assert ev.uid == "J-1" and ev.callsign == "JARVIS"
    ev2 = parse_cot(to_cot(ev).encode())
    assert ev2.uid == ev.uid and abs(ev2.point.lat - 41.1375) < 1e-6
    assert is_ephemeral_beacon(ev) is True
    ev.type = "b-t-f"
    assert is_ephemeral_beacon(ev) is False
```
- [ ] **Step 2:** Run → PASS (codec already implements this; this is a characterization test that guards the move). If it fails, fix the import path only.
- [ ] **Step 3:** Commit `test(skcot): codec round-trip + ephemeral classification`.

---

### Task 7: systemd units + MIGRATION.md

**Files:**
- Create: `skcot/systemd/skcot-service.service`, `skcot/systemd/skcot-agent@.service`, `skcot/MIGRATION.md`

- [ ] **Step 1:** Write `skcot-service.service` modeled on `skcomms-cot.service` but `ExecStart=%h/.skenv/bin/python -m skcot.service` and `Environment=PATH=%h/.skenv/bin:/usr/local/bin:/usr/bin:/bin`.
- [ ] **Step 2:** Write `skcot-agent@.service` from `skcomms-cot-agent@.service`, `ExecStart=%h/.skenv/bin/python -m skcot.agent %i ...` with the PATH env.
- [ ] **Step 3:** Write `MIGRATION.md`: unit renames (`skcomms-cot*`→`skcot-*`), entry-point changes (`python -m skcomms.cot_service`→`python -m skcot.service`), and the note that old units are already stopped+disabled.
- [ ] **Step 4:** Commit `feat(skcot): systemd units + migration notes`.

---

### Task 8: Remove the moved modules from skcomms; confirm clean separation

**Files:**
- Delete from skcomms: `src/skcomms/{cot.py,cot_server.py,cot_service.py,cot_agent.py,cot_client.py,cot_pki.py,geo.py}`
- Modify: any skcomms `__init__.py` / entry-point references to the removed modules.

- [ ] **Step 1:** Delete the 7 modules from skcomms.
- [ ] **Step 2:** `grep -rn "cot\|geo\b" ~/clawd/skcapstone-repos/skcomms/src/skcomms --include=*.py | grep -viE "test|capabilit|robot|category"` — resolve any dangling reference (there should be none in core; the generic beacon-gating pieces that STAY are `send_federated`/`PeerInfo.capabilities`, which do not import CoT).
- [ ] **Step 3:** Run the full skcomms suite: `~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skcomms/tests` → green (allowing the 1 known pre-existing standalone-flag flake). Confirm no `ModuleNotFoundError` for cot/geo.
- [ ] **Step 4:** Confirm `grep -rn "import skcot" ~/clawd/skcapstone-repos/skcomms/src` empty (no reverse dep).
- [ ] **Step 5:** Commit in skcomms `refactor(skcomms): remove CoT/geo (extracted to skcot)`.

---

### Task 9: Final verification

- [ ] **Step 1:** Fresh install check: `~/.skenv/bin/pip install -e ~/clawd/skcapstone-repos/skcot` then `~/.skenv/bin/python -m skcot.service --help` (or `-c "import skcot.service"`) → no error.
- [ ] **Step 2:** Run full skcot suite → green. Run full skcomms suite → green.
- [ ] **Step 3:** Confirm the three properties are covered (Tasks 4, 5, 6 pass).
- [ ] **Step 4:** Push both repos: `git -C ~/clawd/skcapstone-repos/skcot push -u origin main`; `git -C ~/clawd/skcapstone-repos/skcomms push origin main`.
- [ ] **Step 5:** Update coord epic `7a97fcb3` to done.

## Self-review notes
- Spec coverage: packaging (T1), module moves (T2), test moves (T6/T3), ephemeral-rail-by-composition (T5), capability advertise (T4), migration/units (T7), skcomms cleanup + no-reverse-dep (T8), verify (T9). All spec sections mapped.
- The one genuine unknown is the discovery self-peer publish accessor name (T4); the task tells the implementer to grep for it rather than guessing a name.
- Copy-then-delete ordering (T2 copies, T8 deletes) keeps skcomms working throughout, so a failed mid-extraction never leaves the tree broken.
