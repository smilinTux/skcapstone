"""Locate immutable accepted plans without assuming their generation path."""

from . import production_test_plan as plan


def read_accepted_plan(home, binding, digest):
    bases = {
        plan._plan_base(home, binding),
        plan._initial_plan_base(home, binding),
        plan._legacy_plan_base(home, binding),
    }
    for base in sorted(bases):
        for path in [base, *sorted(base.parent.glob(base.stem + ".*.json"))]:
            try:
                raw = plan.read_private(path)
            except FileNotFoundError:
                continue
            if plan.sha(raw) == digest:
                return raw
    raise plan.TestEvidenceError("retained historical test plan changed or missing")
