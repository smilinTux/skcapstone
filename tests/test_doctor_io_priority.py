"""doctor io:scheduler / io:syncthing_dropin, and the install.sh bfq step.

noroc2027 (QEMU HDD, scheduler "none", no syncthing drop-in) sat at ~90% IO
pressure with every background sync "idle" on paper, 2026-11-12.
"""

from pathlib import Path

import pytest

from skcapstone import doctor

ROOT = Path(__file__).resolve().parents[1]


def _disk(sys_block: Path, name: str, rotational: str, scheduler: str) -> None:
    q = sys_block / name / "queue"
    q.mkdir(parents=True)
    (q / "rotational").write_text(rotational + "\n")
    (q / "scheduler").write_text(scheduler + "\n")


@pytest.fixture(autouse=True)
def _linux(monkeypatch):
    monkeypatch.setattr(doctor.platform, "system", lambda: "Linux")


def _by_name(checks):
    return {c.name: c for c in checks}


def test_spinning_disk_without_bfq_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    _disk(tmp_path, "sda", "1", "[none] mq-deadline")
    c = _by_name(doctor._check_io_priority(sys_block=tmp_path))["io:scheduler:sda"]
    assert not c.passed
    assert c.detail == "scheduler=none"
    assert "60-ioscheduler-bfq.rules" in c.fix


def test_spinning_disk_with_bfq_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    _disk(tmp_path, "sda", "1", "none mq-deadline [bfq]")
    assert _by_name(doctor._check_io_priority(sys_block=tmp_path))["io:scheduler:sda"].passed


def test_ssd_loop_and_zram_are_not_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    _disk(tmp_path, "nvme0n1", "0", "[none]")
    _disk(tmp_path, "loop0", "1", "[none]")
    _disk(tmp_path, "zram0", "1", "[none]")
    assert doctor._check_io_priority(sys_block=tmp_path) == []


def test_usb_attached_disk_is_not_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    real = tmp_path / "devices" / "usb2" / "2-1" / "block" / "sda"
    q = real / "queue"
    q.mkdir(parents=True)
    (q / "rotational").write_text("1\n")
    (q / "scheduler").write_text("[mq-deadline] none\n")
    sys_block = tmp_path / "block"
    sys_block.mkdir()
    (sys_block / "sda").symlink_to(real)
    assert doctor._check_io_priority(sys_block=sys_block) == []


def test_syncthing_dropin_missing_fails_and_present_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/syncthing")
    sys_block = tmp_path / "sys"
    sys_block.mkdir()
    units = tmp_path / "units"
    c = _by_name(doctor._check_io_priority(sys_block=sys_block, unit_dir=units))
    assert not c["io:syncthing_dropin"].passed
    (units / "syncthing.service.d").mkdir(parents=True)
    (units / "syncthing.service.d" / "nice.conf").write_text("[Service]\n")
    c = _by_name(doctor._check_io_priority(sys_block=sys_block, unit_dir=units))
    assert c["io:syncthing_dropin"].passed


def test_not_linux_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.platform, "system", lambda: "Darwin")
    assert doctor._check_io_priority(sys_block=tmp_path) == []


def test_udev_rule_targets_rotational_disks_only():
    rule = (ROOT / "systemd/udev/60-ioscheduler-bfq.rules").read_text()
    assert 'ATTR{queue/rotational}=="1"' in rule
    assert 'ATTR{queue/scheduler}="bfq"' in rule


def test_install_sh_installs_the_bfq_rule_and_module():
    sh = (ROOT / "scripts/install.sh").read_text()
    assert '"$REPO_ROOT/systemd/udev/60-ioscheduler-bfq.rules"' in sh
    assert "/etc/udev/rules.d/60-ioscheduler-bfq.rules" in sh
    assert "/etc/modules-load.d/bfq.conf" in sh
    assert "udevadm trigger --subsystem-match=block" in sh


def test_syncthing_dropin_sets_idle_io():
    conf = (ROOT / "systemd/syncthing.service.d/nice.conf").read_text().splitlines()
    assert "IOSchedulingClass=idle" in conf
