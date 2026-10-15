"""Run a native test worker whose sealed initial plan has a remote predecessor."""

from __future__ import annotations

import json
import runpy
import socket
import sys
from pathlib import Path

from . import production_test_plan as plan
from . import production_tests


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("expected plan and run directory")
    plan_path = Path(sys.argv[1]).resolve()
    directory = Path(sys.argv[2]).resolve()
    launch = production_tests.read_json(directory / "launch.json")
    binding = launch.get("binding")
    if not isinstance(binding, dict):
        raise SystemExit("native launch binding is missing")
    home = plan_path.parent.parent.parent.resolve()
    sealed, expected_path, plan_sha = production_tests.load_plan(
        home, binding, allow_remote_host=True
    )
    initial = json.loads(plan.read_private(plan._initial_plan_base(home, binding)))
    host = socket.gethostname().split(".")[0].lower()
    authority = launch.get("policy", {}).get("authority_host")
    if (
        expected_path != plan_path
        or plan.run_directory(home, plan_sha) != directory
        or launch.get("request_id") != plan_sha
        or sealed.get("initial_profile_qualification") is not True
        or sealed.get("host") != host
        or initial.get("remote_initial_profile_qualification") is not True
        or not isinstance(authority, str)
        or initial.get("authority_host") != authority
        or initial.get("host") == authority
    ):
        raise SystemExit("remote initial plan authority or binding is invalid")

    original = production_tests.load_plan
    expected_binding = json.dumps(binding, sort_keys=True, separators=(",", ":"))

    def scoped_load(request_home, request_binding, *args, **kwargs):
        if (
            Path(request_home).resolve() == home
            and json.dumps(request_binding, sort_keys=True, separators=(",", ":"))
            == expected_binding
        ):
            kwargs["allow_remote_host"] = True
        return original(request_home, request_binding, *args, **kwargs)

    production_tests.load_plan = scoped_load
    sys.argv = [
        "skcapstone.fleet.production_test_worker",
        str(plan_path),
        str(directory),
    ]
    runpy.run_module("skcapstone.fleet.production_test_worker", run_name="__main__")


if __name__ == "__main__":
    main()
