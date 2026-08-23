"""Source-drift checks: is the code we RUN the same as the code anyone else can get?

Services here import straight from editable checkouts, so production IS the
working tree and nothing forces "what runs" to equal "what is committed" or
"what is published". These pin the three questions doctor now asks, and - more
importantly - pin the two ways this check could quietly become useless:
comparing version strings instead of content, and reporting an unanswerable
check as a pass.

Design spec: docs/superpowers/specs/2026-08-08-dev-ci-prod-sync-design.md
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from skcapstone.doctor import (
    Check,
    DiagnosticReport,
    _check_source_drift,
    _digest_py_files,
    _distribution_name,
    _repo_root_for,
    _unpushed_check,
)


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A real git repo with one commit, so the checks exercise real git."""
    r = tmp_path / "pkg-repo"
    (r / "src" / "demo").mkdir(parents=True)
    (r / "src" / "demo" / "__init__.py").write_text('__version__ = "1.0.0"\n')
    _run(r.parent, "init", "-q", str(r))
    _run(r, "config", "user.email", "t@t.test")
    _run(r, "config", "user.name", "T")
    _run(r, "add", "-A")
    _run(r, "commit", "-qm", "init")
    return r


# ── the tri-state: an unanswerable check is not a pass ─────────────────────


class TestUnknownIsNeitherPassNorFail:
    """Reporting an unanswerable check as a pass is a lie; reporting it as a
    failure trains people to ignore the whole category. It must be its own
    state, or the check erodes into noise either way."""

    def test_unknown_counts_toward_neither_tally(self):
        report = DiagnosticReport(
            checks=[
                Check(name="a", description="", passed=True),
                Check(name="b", description="", passed=False),
                Check(name="c", description="", passed=False, unknown=True),
            ]
        )
        assert report.passed_count == 1
        assert report.failed_count == 1
        assert report.unknown_count == 1
        assert report.total_count == 3

    def test_an_unknown_never_inflates_the_passed_count(self):
        report = DiagnosticReport(
            checks=[Check(name="a", description="", passed=True, unknown=True)]
        )
        assert report.passed_count == 0
        assert report.unknown_count == 1

    def test_to_dict_surfaces_unknown(self):
        report = DiagnosticReport(
            checks=[Check(name="a", description="", passed=False, unknown=True)]
        )
        data = report.to_dict()
        assert data["unknown"] == 1
        assert data["checks"][0]["unknown"] is True

    def test_existing_checks_are_unaffected(self):
        """The field defaults off, so every pre-existing check behaves exactly
        as before."""
        c = Check(name="a", description="", passed=True)
        assert c.unknown is False


# ── the load-bearing decision: compare CONTENT, not version strings ────────


class TestContentComparisonNotVersionStrings:
    """capauth's repo and its published wheel BOTH said 0.2.14 while differing
    in eight authorization rules. A version comparison would have reported
    them as in sync, actively confirming the thing we were trying to disprove.
    This is the case the whole check exists for."""

    def test_same_version_different_code_is_detected(self, tmp_path):
        published = tmp_path / "published" / "demo"
        checkout = tmp_path / "checkout" / "demo"
        for d in (published, checkout):
            d.mkdir(parents=True)
            (d / "__init__.py").write_text('__version__ = "0.2.14"\n')

        (published / "authz.py").write_text("RULES = ['a', 'b', 'c']\n")
        (checkout / "authz.py").write_text("RULES = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']\n")

        # Identical version strings...
        assert (published / "__init__.py").read_text() == (checkout / "__init__.py").read_text()
        # ...and the check still sees them as different code.
        assert _digest_py_files(published) != _digest_py_files(checkout)

    def test_identical_trees_hash_equal(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        for d in (a, b):
            (d / "sub").mkdir(parents=True)
            (d / "mod.py").write_text("x = 1\n")
            (d / "sub" / "other.py").write_text("y = 2\n")
        assert _digest_py_files(a) == _digest_py_files(b)

    def test_a_moved_file_changes_the_digest(self, tmp_path):
        """Path is hashed alongside content, so relocating code is drift too."""
        a, b = tmp_path / "a", tmp_path / "b"
        (a / "sub").mkdir(parents=True)
        (b / "sub").mkdir(parents=True)
        (a / "mod.py").write_text("x = 1\n")
        (b / "sub" / "mod.py").write_text("x = 1\n")
        assert _digest_py_files(a) != _digest_py_files(b)

    def test_pycache_is_ignored(self, tmp_path):
        """A wheel and a checkout legitimately differ in caches; treating that
        as drift would make the check cry wolf on every package."""
        a, b = tmp_path / "a", tmp_path / "b"
        for d in (a, b):
            d.mkdir()
            (d / "mod.py").write_text("x = 1\n")
        cache = b / "__pycache__"
        cache.mkdir()
        (cache / "mod.cpython-312.py").write_text("garbage\n")
        assert _digest_py_files(a) == _digest_py_files(b)

    def test_non_python_files_do_not_count(self, tmp_path):
        """Build metadata and data files differ between a wheel and a tree for
        reasons that are not code drift."""
        a, b = tmp_path / "a", tmp_path / "b"
        for d in (a, b):
            d.mkdir()
            (d / "mod.py").write_text("x = 1\n")
        (b / "README.md").write_text("hello\n")
        assert _digest_py_files(a) == _digest_py_files(b)

    def test_a_missing_tree_is_unknown_not_a_match(self, tmp_path):
        assert _digest_py_files(tmp_path / "nope") is None

    def test_two_empty_trees_must_not_hash_equal(self, tmp_path):
        """The nastiest false pass available here. rglob over an empty dir
        yields nothing and hashes to the digest of the empty string, so two
        failed wheel extractions would hash identically and the check would
        confidently report "matches the release" having compared nothing."""
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert _digest_py_files(a) is None
        assert _digest_py_files(b) is None

    def test_a_tree_holding_only_non_python_files_is_unknown(self, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        (d / "README.md").write_text("hi\n")
        assert _digest_py_files(d) is None


# ── git state ──────────────────────────────────────────────────────────────


class TestUncommitted:
    def test_a_clean_checkout_passes(self, repo, monkeypatch):
        monkeypatch.setattr(
            "skcapstone.doctor._repo_root_for",
            lambda p: repo if p == "skcapstone" else None,
        )
        checks = {c.name: c for c in _check_source_drift()}
        assert checks["source:skcapstone:uncommitted"].passed

    def test_an_edited_tracked_file_fails(self, repo, monkeypatch):
        (repo / "src" / "demo" / "__init__.py").write_text('__version__ = "9.9.9"\n')
        monkeypatch.setattr(
            "skcapstone.doctor._repo_root_for",
            lambda p: repo if p == "skcapstone" else None,
        )
        c = {x.name: x for x in _check_source_drift()}["source:skcapstone:uncommitted"]
        assert not c.passed
        assert not c.unknown
        assert "exists nowhere else" in c.detail

    def test_untracked_files_are_not_drift(self, repo, monkeypatch):
        """A working checkout collects build output and scratch files
        constantly. Flagging those buries the signal that matters."""
        (repo / "scratch.log").write_text("noise\n")
        (repo / "build_output.py").write_text("x = 1\n")
        monkeypatch.setattr(
            "skcapstone.doctor._repo_root_for",
            lambda p: repo if p == "skcapstone" else None,
        )
        assert {x.name: x for x in _check_source_drift()}["source:skcapstone:uncommitted"].passed


class TestUnpushed:
    def test_no_upstream_is_unknown_not_a_pass(self, repo):
        """A branch with no upstream is a legitimate state, but it means we
        cannot answer the question. Answering it anyway would be a false pass
        on the most dangerous case: code that exists only on this node."""
        c = _unpushed_check("demo", repo)
        assert c.unknown
        assert not c.passed
        assert c.detail.startswith("UNKNOWN")

    def test_in_sync_with_upstream_passes(self, repo, tmp_path):
        remote = tmp_path / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True
        )
        _run(repo, "remote", "add", "origin", str(remote))
        branch = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        _run(repo, "push", "-q", "-u", "origin", branch)

        c = _unpushed_check("demo", repo)
        assert c.passed and not c.unknown

    def test_local_commits_ahead_of_the_remote_fail(self, repo, tmp_path):
        remote = tmp_path / "remote2.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True
        )
        _run(repo, "remote", "add", "origin", str(remote))
        branch = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        _run(repo, "push", "-q", "-u", "origin", branch)

        (repo / "src" / "demo" / "new.py").write_text("x = 1\n")
        _run(repo, "add", "-A")
        _run(repo, "commit", "-qm", "local only")

        c = _unpushed_check("demo", repo)
        assert not c.passed
        assert not c.unknown
        assert "1 commit(s) ahead" in c.detail
        assert "CI has never seen this" in c.detail

    def test_a_bad_repo_path_is_unknown(self, tmp_path):
        c = _unpushed_check("demo", tmp_path / "not-a-repo")
        assert c.unknown
        assert not c.passed


class TestScope:
    def test_a_package_with_no_checkout_emits_nothing(self, monkeypatch):
        """Installed from a real wheel is the REPRODUCIBLE case, so it is not a
        finding. Not installed at all is already reported by _check_packages,
        and duplicating it here would double-count."""
        monkeypatch.setattr("skcapstone.doctor._repo_root_for", lambda p: None)
        assert _check_source_drift() == []

    def test_deep_is_off_by_default(self, repo, monkeypatch):
        """The content comparison is network-bound and slow. Session-start runs
        must stay fast and work offline."""
        monkeypatch.setattr(
            "skcapstone.doctor._repo_root_for",
            lambda p: repo if p == "skcapstone" else None,
        )
        names = {c.name for c in _check_source_drift()}
        assert "source:skcapstone:unpublished" not in names

        deep_names = {c.name for c in _check_source_drift(deep=True)}
        assert "source:skcapstone:unpublished" in deep_names

    def test_skcapstone_itself_resolves_to_a_checkout_here(self):
        """Guards the resolver against an editable-install layout change: if
        this returns None the whole check silently becomes a no-op."""
        root = _repo_root_for("skcapstone")
        assert root is not None
        assert (root / ".git").exists()

    def test_import_name_maps_to_the_pypi_distribution_name(self):
        """skchat imports as `skchat` but publishes as `skchat-sovereign`;
        getting this wrong makes the check compare against the wrong project."""
        assert _distribution_name("skchat") == "skchat-sovereign"
        assert _distribution_name("capauth") == "capauth"


# ── the CLI gate: --category and --strict ──────────────────────────────────


class TestStrictAndCategory:
    """`doctor` always exited 0, so nothing could gate on it. These two flags
    are what lets a scheduled job alert on drift, and the narrowness matters as
    much as the exit code: a gate that trips on unrelated pre-existing failures
    is one people learn to ignore."""

    @staticmethod
    def _invoke(monkeypatch, checks, *args):
        import json as _json

        from click.testing import CliRunner

        from skcapstone.cli import main
        from skcapstone.doctor import DiagnosticReport

        monkeypatch.setattr(
            "skcapstone.doctor.run_diagnostics",
            lambda home, deep=False: DiagnosticReport(checks=list(checks)),
        )
        result = CliRunner().invoke(main, ["doctor", *args])
        return result, _json

    def test_category_narrows_the_report(self, monkeypatch):
        checks = [
            Check(name="source:a", description="a", passed=False, category="source"),
            Check(name="pkg:b", description="b", passed=False, category="packages"),
        ]
        result, js = self._invoke(monkeypatch, checks, "--category", "source", "--json-out")
        data = js.loads(result.output)
        assert [c["name"] for c in data["checks"]] == ["source:a"]

    def test_strict_exits_nonzero_on_a_failure(self, monkeypatch):
        checks = [Check(name="source:a", description="a", passed=False, category="source")]
        result, _ = self._invoke(monkeypatch, checks, "--category", "source", "--strict")
        assert result.exit_code == 1

    def test_strict_exits_zero_when_everything_passes(self, monkeypatch):
        checks = [Check(name="source:a", description="a", passed=True, category="source")]
        result, _ = self._invoke(monkeypatch, checks, "--category", "source", "--strict")
        assert result.exit_code == 0

    def test_an_unknown_alone_does_not_trip_strict(self, monkeypatch):
        """The whole point of the tri-state. An offline node cannot answer the
        unpublished question, and paging someone for that would train them to
        mute the alert - which costs us the real drift signal too."""
        checks = [
            Check(
                name="source:a",
                description="a",
                passed=False,
                unknown=True,
                category="source",
            )
        ]
        result, _ = self._invoke(monkeypatch, checks, "--category", "source", "--strict")
        assert result.exit_code == 0

    def test_strict_ignores_failures_outside_the_chosen_category(self, monkeypatch):
        """A drift alarm must not fire because some unrelated check is red."""
        checks = [
            Check(name="source:a", description="a", passed=True, category="source"),
            Check(name="pkg:b", description="b", passed=False, category="packages"),
        ]
        result, _ = self._invoke(monkeypatch, checks, "--category", "source", "--strict")
        assert result.exit_code == 0

    def test_without_strict_a_failure_still_exits_zero(self, monkeypatch):
        """Unchanged default: doctor is a report, not a gate, unless asked."""
        checks = [Check(name="source:a", description="a", passed=False, category="source")]
        result, _ = self._invoke(monkeypatch, checks, "--category", "source")
        assert result.exit_code == 0


class TestJsonOutIsActuallyMachineReadable:
    """--json-out documents itself as machine-readable. Optional third-party
    deps imported during the checks print banners at import time (liboqs-python
    prints one), which landed ahead of the JSON and made json.load() fail on
    the very first character."""

    def test_a_banner_printed_during_checks_does_not_corrupt_the_json(self, monkeypatch):
        import json as _json

        from click.testing import CliRunner

        from skcapstone.cli import main
        from skcapstone.doctor import DiagnosticReport

        def _noisy(home, deep=False):
            print("liboqs-python faulthandler is disabled")
            return DiagnosticReport(
                checks=[Check(name="source:a", description="a", passed=True, category="source")]
            )

        monkeypatch.setattr("skcapstone.doctor.run_diagnostics", _noisy)
        result = CliRunner().invoke(main, ["doctor", "--category", "source", "--json-out"])

        data = _json.loads(result.output)  # would raise before the fix
        assert data["total"] == 1

    def test_human_output_is_left_alone(self, monkeypatch):
        """Only --json-out has a purity contract; do not silently swallow
        output a human asked to see."""
        from click.testing import CliRunner

        from skcapstone.cli import main
        from skcapstone.doctor import DiagnosticReport

        def _noisy(home, deep=False):
            print("SOME-BACKEND-BANNER")
            return DiagnosticReport(
                checks=[Check(name="source:a", description="a", passed=True, category="source")]
            )

        monkeypatch.setattr("skcapstone.doctor.run_diagnostics", _noisy)
        result = CliRunner().invoke(main, ["doctor", "--category", "source"])
        assert "SOME-BACKEND-BANNER" in result.output
