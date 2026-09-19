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
