from skcapstone.fleet.installer import InstallPlan, InstallResult, InstallStep


def test_install_step_and_plan_are_immutable_records():
    step = InstallStep(name="skgateway.service", kind="unit", tier=4, backend_id="skchat")
    plan = InstallPlan(steps=[step])
    assert plan.steps[0].name == "skgateway.service"
    assert plan.steps[0].tier == 4


def test_install_result_carries_status_and_detail():
    step = InstallStep(name="capauth", kind="package", tier=1, backend_id="packages")
    r = InstallResult(step=step, status="ok")
    assert r.status == "ok" and r.detail == ""
