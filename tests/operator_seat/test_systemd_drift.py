"""Tests for the read-only effective-unit drift doctor."""

from pathlib import Path

from skcapstone import systemd_drift

SERVICE = """[Service]\nType=oneshot\nEnvironment=A=1\nExecStart=/bin/true\n"""


def test_compare_clean_source_and_effective() -> None:
    result = systemd_drift.compare_unit("x.service", SERVICE, "# path\n" + SERVICE)
    assert result.clean


def test_compare_detects_dropin_override_and_extra() -> None:
    effective = SERVICE + "\n[Service]\nExecStart=\nExecStart=/bin/false\nNice=10\n"
    result = systemd_drift.compare_unit("x.service", SERVICE, effective)
    assert "Service.ExecStart" in result.changed
    assert "Service.Nice" in result.extra


def test_audit_degrades_missing_effective_unit(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "x.service").write_text(SERVICE)
    monkeypatch.setattr(
        systemd_drift, "effective_unit", lambda unit: (_ for _ in ()).throw(RuntimeError("absent"))
    )
    result = systemd_drift.audit(tmp_path, ("x.service",))[0]
    assert not result.clean
    assert result.unavailable == "absent"
