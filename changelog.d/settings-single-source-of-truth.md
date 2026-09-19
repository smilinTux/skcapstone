- **Fleet and gateway settings now have one registry, and the numbers in it are
  CI-asserted against the code instead of retyped.** The trigger was a doc that
  contradicted *itself*: a table said the codex pool ceiling was 32 while a paragraph
  three above it said 4, left behind when the cap was raised 4 → 32 on 2026-08-25.
  Three readers in a row reasoned from the stale half and produced wrong capacity
  recommendations. Nothing detected it, because nothing checked prose against config.
  `docs/fleet/SETTINGS-REGISTRY.md` is now the one place a fleet or gateway setting is
  declared, split honestly into what CI can reach and what it cannot. Repo defaults
  (§1) are **paired** to their code constant by ten new tier-3 assertions that extract
  the value from *both* sides and compare: builder ceiling, codex lane model, qwen and
  kimi lane target defaults, and both daemon ports. A literal `grep -q` was deliberately
  not used — it passes when both sides are edited to the same wrong value and stays
  silent when only the doc moves. Estate values (§2: the two gateways' ports, the chi
  pool ceilings, per-host lane targets, per-node builder-capacity labels) cannot be read
  from a runner with no account on those hosts, so they are recorded as dated
  measurements each carrying its re-measure command, and tier 3 asserts the property CI
  *can* see: that they are declared in the registry and **nowhere else**, since a second
  copy is the thing that rots. Two further assertions pin the failure mode directly —
  the chi gateway is never given the nor gateway's port (`chiap01:18780`), and no doc
  may reassert the superseded codex ceiling of 4 or 16. `scripts/docs/prose_grep.sh`
  strips `SOP.md`'s own evidence block so a forbid-this-string check does not match its
  own text. `SOP.md` §6 claimed "the chi estate runs codex 30, glm 9, kimi 9"; measured
  2026-09-19 it runs codex 18, glm 0, kimi 6, so the numbers were removed from the SOP
  rather than corrected in place — an estate number in a repo doc is a copy that rots by
  construction. The rule is one paragraph in `CONTRIBUTING.md`: a number in prose with
  no assertion behind it is a defect.

- **A document can no longer contradict itself on a registered fact.** The original
  defect was not a doc disagreeing with the code — it was a doc disagreeing with
  *itself*, table versus prose, three paragraphs apart. `scripts/docs/check_self_consistency.py`
  registers a fact name and a narrow regex, extracts every value that fact is given
  within a single file, and fails when they disagree. Generic prose contradiction
  detection is not tractable and this deliberately does not attempt it; a registered set
  is. Proven against a faithful reconstruction of the original note (table `codex max
  32`, prose `codex slots 4`): red, with both line numbers, and green once corrected.
  Six facts registered to start. `docs/fleet/model-lane-routing.md` was in exactly this
  state and is fixed: its lane table gave the codex default model as `sk-codex` while
  the same file said `sk-codex-mid` twice further down, matching the code. Its kimi
  gateway caps (`max: 28` / `max: 14`, `maxQueue: 400`) were an intended design that was
  never deployed and had been reading as current for weeks — live on chiap01 is 5 and 4
  at `maxQueue: 8` — so they now point at the registry, and an assertion forbids the
  superseded pair from reappearing. The account-family limits (30 / 16), a genuinely
  different fact, stay where they were measured.

- **Two more duplication sites pinned.** `systemd/` and `src/skcapstone/data/systemd/`
  are byte-identical 43-file trees, so every `Environment=SKFLEET_*` value is declared
  twice and an edit can land in one copy only; tier 3 now asserts `diff -rq` between
  them. And the codex lane model is pinned in five places across `src/` and `scripts/`
  (`skfleet-rotate.py`, `skrsi_estate_adapters.py`, `seat_manifest_audit.py`,
  `lifecycle_seats.py`, `lifecycle-seat-profiles.json`); an assertion requires all five
  to be the same string. The pattern is deliberately `"sk-codex-[a-z]+"`, not
  `"sk-codex[a-z-]*"`: the broader form matched the legitimate `model.startswith("sk-codex")`
  prefix test in `skworld-digest.py` and red-flagged a correct file, and a gate that
  cries wolf gets waived.
