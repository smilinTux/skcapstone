"""Shared test fixtures for skcapstone.

Coverage audit (task 945325c8, 2026-03-02):
- Reviewed git log: zero test-only commits found.  Every commit that adds or
  modifies test files also adds or modifies corresponding source files.
- All modified test files in the working tree (test_chat, test_consciousness_loop,
  test_dashboard, test_prompt_adapter) have matching modified source files.
- All new untracked test files have matching new untracked source files.
- New untracked source files that may still need test coverage integration:
    cli/errors_cmd.py, cli/mood_cmd.py, cli/profile_cmd.py, cli/search_cmd.py,
    cli/test_connection.py, cli/upgrade_cmd.py, cli/usage_cmd.py, cli/version_cmd.py
  (unit test stubs exist; integration tests pending - see task f675ef5c).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Token signing fixtures
# ---------------------------------------------------------------------------
#
# capauth.tokens.issue_token refuses to issue an unsigned token (it raises
# TokenSigningError and writes nothing).  That refusal is correct: an unsigned
# token is rejected by capauth.authz.decide, so a stored unsigned token is a
# grant that authorizes nothing while looking issued.
#
# It also means a test cannot get a token out of an agent home whose identity
# fingerprint has no matching SECRET key in the keyring gpg is pointed at.
# skcapstone.pillars.identity.generate_identity produces exactly that: the
# capauth profile it creates is pgpy-managed and its private half never lands
# in a gpg keyring, and when capauth is unavailable the fingerprint is a bare
# sha256 placeholder.  Either way `gpg --local-user <fp> --detach-sign` fails
# with "No secret key", on a bare CI runner and on a workstation with a full
# keyring alike.
#
# So tests that need a real token generate a real throwaway signing key in an
# isolated GNUPGHOME and point the agent's identity at it.  ed25519 generation
# is effectively instantaneous (~15ms) and needs no entropy wait, so this costs
# nothing measurable and exercises the genuine sign-then-store path.


def _gpg_or_fail() -> str:
    """Locate the gpg binary, failing loudly rather than skipping.

    Skipping here would silently drop the only coverage of the signed-issuance
    path, and token signing is not optional behaviour for this package.
    """
    gpg = shutil.which("gpg")
    if gpg is None:
        pytest.fail("gpg is required to test capability token signing but is not on PATH")
    return gpg


@pytest.fixture(scope="session")
def _gpg_signing_keyring():
    """A throwaway GNUPGHOME holding one real, passphrase-less signing key.

    Session-scoped because the key material is inert test data: every consumer
    wants "some fingerprint gpg can actually sign with", not a distinct
    identity, so generating it once keeps the suite fast.

    The home is created under the system temp dir rather than pytest's tmp_path
    so the gpg-agent socket path stays well under its 108-byte limit.
    """
    gpg = _gpg_or_fail()
    home = Path(tempfile.mkdtemp(prefix="skcapstone-gnupg-"))
    home.chmod(0o700)

    subprocess.run(
        [
            gpg,
            "--homedir",
            str(home),
            "--batch",
            "--quiet",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--quick-generate-key",
            "skcapstone test signer <token-tests@skcapstone.invalid>",
            "ed25519",
            "sign",
            "0",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )

    listing = subprocess.run(
        [gpg, "--homedir", str(home), "--list-secret-keys", "--with-colons"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    fingerprint = ""
    for line in listing.stdout.splitlines():
        if line.startswith("fpr:"):
            fingerprint = line.split(":")[9]
            break
    assert fingerprint, "throwaway gpg key was generated but exposed no fingerprint"

    try:
        yield home, fingerprint
    finally:
        # gpg-agent holds the home open; killing it first keeps the removal
        # clean and leaves no stray daemon behind after the run.
        subprocess.run(
            ["gpgconf", "--homedir", str(home), "--kill", "all"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def signing_identity(_gpg_signing_keyring, monkeypatch):
    """Point gpg at the throwaway keyring and hand back an identity binder.

    Returns a callable ``bind(agent_home) -> fingerprint`` that rewrites the
    agent's ``identity/identity.json`` fingerprint to the throwaway key, which
    is what capauth reads to pick the ``--local-user`` for signing.  Call it
    after ``generate_identity``, which writes that file.

    ``GNUPGHOME`` is redirected for the whole test, so nothing here can read or
    write the developer's real ``~/.gnupg``.
    """
    gnupghome, fingerprint = _gpg_signing_keyring
    monkeypatch.setenv("GNUPGHOME", str(gnupghome))

    def bind(agent_home: Path) -> str:
        identity_file = agent_home / "identity" / "identity.json"
        data = json.loads(identity_file.read_text(encoding="utf-8"))
        data["fingerprint"] = fingerprint
        identity_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return fingerprint

    bind.fingerprint = fingerprint
    return bind


@pytest.fixture
def unsignable_gnupghome(monkeypatch, tmp_path: Path) -> Path:
    """Redirect gpg at an empty keyring, so no issuer fingerprint can sign.

    Deliberately empty: this is how the refusal guard stays deterministic on a
    developer box that has a fully populated ``~/.gnupg``.
    """
    _gpg_or_fail()
    gnupghome = tmp_path / "empty-gnupg"
    gnupghome.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(gnupghome))
    return gnupghome


@pytest.fixture(autouse=True)
def _silence_desktop_notifications(monkeypatch):
    """Suppress real desktop notifications for the entire test session.

    Several code paths (consciousness_loop, kms_scheduler, the send_notification
    MCP tool, and NotificationManager) shell out to ``notify-send`` / libnotify.
    Left unmocked, a test run floods the live desktop's notification tray -
    and mass-closing that backlog can stall single-threaded shells like
    Cinnamon, freezing the whole UI.  Forcing the guard off keeps test runs
    silent regardless of which path fires.

    A test that needs to exercise the real dispatch can re-enable it locally
    with ``monkeypatch.setenv("SKCAPSTONE_DESKTOP_NOTIFY", "1")``.
    """
    monkeypatch.setenv("SKCAPSTONE_DESKTOP_NOTIFY", "0")


@pytest.fixture(autouse=True)
def _isolate_agent_env(monkeypatch):
    """Prevent host SKCAPSTONE_AGENT / SKMEMORY_AGENT from leaking into unit tests.

    The profile-aware runtime reads both the env var and the module-level
    skcapstone.SKCAPSTONE_AGENT (set at import time).  We clear both so that
    _memory_dir() falls back to the flat "home/memory" layout expected by
    tests that use the tmp_agent_home fixture.
    Tests that need a specific agent should override explicitly via monkeypatch.
    """
    monkeypatch.delenv("SKCAPSTONE_AGENT", raising=False)
    monkeypatch.delenv("SKMEMORY_AGENT", raising=False)
    import skcapstone

    monkeypatch.setattr(skcapstone, "SKCAPSTONE_AGENT", "")
    # _detect_active_agent() scans ~/.skcapstone/agents/ even when the env var
    # is cleared, returning a real agent name that routes memory writes to the
    # wrong directory.  Stub it out so tests using tmp directories get the flat
    # "home/memory" layout they expect.
    monkeypatch.setattr(skcapstone, "_detect_active_agent", lambda root=None: None)


@pytest.fixture(autouse=True)
def _isolate_joule_wallet(monkeypatch, tmp_path_factory):
    """Point the default joule wallet root at a throwaway directory.

    ``JouleWallet``/``JouleEngine`` fall back to ``skjoule.SHARED_ROOT`` when no
    ``home`` is given, and mint/spend are WRITES to real economic state. A test
    that forgets ``home=tmp_path`` therefore edits the operator's live ledger,
    and the resulting entries are indistinguishable from real ones: the sibling
    harness measured 1,366 fixture mints totalling 102,450 joules landing in a
    live wallet before anybody noticed. Isolation is the default here rather
    than something each test file has to remember to opt into.

    Only ``skjoule``'s own binding is redirected, not ``skcapstone.SHARED_ROOT``
    or the CLI's, because this fixture is about the wallet and nothing else. The
    complementary half is ``skjoule.assert_not_production_wallet_in_test()``,
    which raises when a test reaches a production root by some path this fixture
    does not cover (an explicit ``home=``, for instance). The fixture keeps
    honest tests safe; the assertion is what proves they were.
    """
    from skcapstone import skjoule

    monkeypatch.setattr(skjoule, "SHARED_ROOT", str(tmp_path_factory.mktemp("joule-wallet-root")))


@pytest.fixture
def tmp_agent_home(tmp_path: Path) -> Path:
    """Provide a temporary agent home directory for testing."""
    agent_home = tmp_path / ".skcapstone"
    agent_home.mkdir()
    return agent_home


@pytest.fixture
def initialized_agent_home(tmp_agent_home: Path) -> Path:
    """Provide a fully initialized agent home with directory structure."""
    for subdir in ("identity", "memory", "trust", "security", "skills", "config", "sync"):
        (tmp_agent_home / subdir).mkdir()

    import json
    from datetime import datetime, timezone

    manifest = {
        "name": "test-agent",
        "version": "0.1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "connectors": [],
    }
    (tmp_agent_home / "manifest.json").write_text(json.dumps(manifest, indent=2))

    import yaml

    config = {"agent_name": "test-agent", "auto_rehydrate": True, "auto_audit": True}
    (tmp_agent_home / "config" / "config.yaml").write_text(
        yaml.dump(config, default_flow_style=False)
    )

    return tmp_agent_home
