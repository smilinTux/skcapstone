"""The automatic refresh has one durable native qualification slot."""

from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import profile_requalification as refresh


def test_only_one_unfinished_native_refresh_is_advanced(tmp_path):
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    first = root / ("a" * 64 + ".job.json")
    second = root / ("b" * 64 + ".job.json")
    plan.write_once(first, {"card": "1234abcd"})
    plan.write_once(second, {"card": "5678abcd"})
    pending = list(refresh._records(root))
    assert len(pending) == 2
    assert pending[0][1]["card"] == "1234abcd"
    plan.write_once(
        pending[0][0].with_name(pending[0][0].stem + ".done.json"),
        {"card": "1234abcd"},
    )
    assert [job["card"] for _, job in refresh._records(root)] == ["5678abcd"]
