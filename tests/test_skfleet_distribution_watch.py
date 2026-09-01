from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATCHER = ROOT / "scripts" / "fleet" / "skfleet-distribution-watch.sh"


def run_bash(source: str, tmp_path: Path, **environment: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "SKFLEET_DISTRIBUTION_WATCH_LIB_ONLY": "1",
            "SKFLEET_DISTRIBUTION_HOSTS": "chiap08",
            "SKFLEET_LOCAL_HOST": "chiap08",
            "SKFLEET_STATE_DIR": str(tmp_path / "state"),
            "SKFLEET_LOG_DIR": str(tmp_path / "log"),
            **environment,
        }
    )
    return subprocess.run(
        ["bash", "-c", f'source "{WATCHER}"\n{source}'],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def sample_script(*, probe: str, claims: str = "", gateway: str = "", active: int = 0) -> str:
    return f"""
probe_host() {{ printf '%b\\n' '{probe}'; }}
collect_claims() {{ printf '%b' '{claims}'; }}
collect_gateway_activity() {{ printf '%b' '{gateway}'; }}
curl() {{ printf '%s\\n' '{{"pool":{{"totalActive":{active},"totalQueued":14}}}}'; }}
skmail() {{ printf '%s\\n' "$*" >> "$SKFLEET_LOG_DIR/mail.log"; }}
sample
"""


def test_all_lanes_and_bash_with_pi_child_are_live(tmp_path: Path) -> None:
    result = run_bash(
        """
pgrep() { printf '42\\n'; }
ps() { printf 'pi\\n'; }
for name in codex-auto-12345678 glm-auto-12345678 qwen-auto-12345678 esc-auto-12345678; do
  lane_from_session "$name"
done
live_pi_child 100 >/dev/null
""",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["codex", "glm", "qwen", "escalate"]


def test_zero_workers_with_active_queue_is_collector_fault_and_notifies_both(
    tmp_path: Path,
) -> None:
    probe = (
        "META\\tchiap08\\tok\\tsuccess\\n"
        "POOL\\tchiap08\\tPOOL|chiap08|ready=0 POOL_IDS|chiap08|ids=-"
    )
    result = run_bash(
        sample_script(probe=probe, gateway="GATEWAY_UNATTRIBUTED\\t29", active=29), tmp_path
    )

    assert result.returncode == 0, result.stderr
    assert "state=collector_fault" in result.stdout
    assert "state=zero" not in result.stdout
    mail = (tmp_path / "log" / "mail.log").read_text()
    assert "jarvis lumina urgent FLEET-DISTRIBUTION-COLLECTOR-FAULT" in mail
    assert "jarvis jarvis urgent FLEET-DISTRIBUTION-COLLECTOR-FAULT" in mail
    assert "FLEET-DISTRIBUTION-DOWN" not in mail


def test_failed_notification_is_reported_and_retried(tmp_path: Path) -> None:
    result = run_bash(
        """
probe_host() {
  printf '%b\n' \
    'META\\tchiap08\\tempty\\tsuccess\\n'\
'POOL\\tchiap08\\tPOOL|chiap08|ready=0 POOL_IDS|chiap08|ids=-'
}
collect_claims() { :; }
collect_gateway_activity() { printf 'GATEWAY_UNATTRIBUTED\\t0\\n'; }
curl() { printf '%s\\n' '{"pool":{"totalActive":0,"totalQueued":0}}'; }
skmail() {
  printf '%s\\n' "$*" >> "$SKFLEET_LOG_DIR/mail.log"
  [[ "$3" != jarvis ]]
}
sample
sample
""",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("state=collector_fault") == 2
    assert result.stdout.count("notification_delivery_failed") == 2
    assert not (tmp_path / "state" / "distribution-watch.state").exists()
    mail = (tmp_path / "log" / "mail.log").read_text()
    assert mail.count("jarvis jarvis urgent FLEET-DISTRIBUTION-DOWN") == 2


def test_sessions_claims_and_gateway_join_by_full_identity_without_counting_queue(
    tmp_path: Path,
) -> None:
    sessions = "\\n".join(
        [
            "SESSION\\tchiap08\\tcodex-auto-11111111\\t11111111\\tcodex\\tlive\\tpi"
            "\\tpi-codex-chiap08-11111111\\t-",
            "SESSION\\tchiap08\\tglm-auto-22222222\\t22222222\\tglm\\tlive\\tpi"
            "\\tpi-glm-chiap08-22222222\\t-",
            "SESSION\\tchiap08\\tqwen-auto-33333333\\t33333333\\tqwen\\tlive\\tbash"
            "\\tpi-qwen-chiap08-33333333\\t-",
            "SESSION\\tchiap08\\tesc-auto-44444444\\t44444444\\tescalate\\tlive\\tpi"
            "\\tpi-esc-chiap08-44444444\\t-",
        ]
    )
    probe = (
        "META\\tchiap08\\tok\\tsuccess\\n"
        "POOL\\tchiap08\\tPOOL|chiap08|ready=0 POOL_IDS|chiap08|ids=-\\n" + sessions
    )
    claims = "\\n".join(
        [
            "CLAIM\\t11111111\\tpi-codex-chiap08-11111111\\tdoing\\tr1\\tchiap08\\tcodex",
            "CLAIM\\t22222222\\tpi-glm-chiap08-22222222\\tdoing\\tr2\\tchiap08\\tglm",
            "CLAIM\\t33333333\\tpi-qwen-chiap08-33333333\\tdoing\\tr3\\tchiap08\\tqwen",
            "CLAIM\\t44444444\\tpi-esc-chiap08-44444444\\tdoing\\tr4\\tchiap08\\tescalate",
        ]
    )
    gateway = (
        "GATEWAY\\t33333333\\tpi-qwen-chiap08-33333333\\t8\\tchiap08\\tqwen"
        "\\tqwen\\nGATEWAY_UNATTRIBUTED\\t6"
    )
    result = run_bash(
        sample_script(probe=probe, claims=claims, gateway=gateway, active=14),
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert "state=up workers=4 codex=1 glm=1 qwen=1 escalate=1" in result.stdout
    assert "queue_active=14" in result.stdout
    assert "unmatched_sessions=- unmatched_claims=- unmatched_gateway=-" in result.stdout


def test_unmatched_claim_is_explicit_collector_fault(tmp_path: Path) -> None:
    probe = (
        "META\\tchiap08\\tok\\tsuccess\\n"
        "POOL\\tchiap08\\tPOOL|chiap08|ready=0 POOL_IDS|chiap08|ids=-"
    )
    claims = "CLAIM\\tdeadbeef\\tpi-glm-chiap08-deadbeef\\tdoing\\trevision\\tchiap08\\tglm"
    result = run_bash(sample_script(probe=probe, claims=claims), tmp_path)

    assert result.returncode == 0, result.stderr
    assert "state=collector_fault" in result.stdout
    assert "unmatched_claims=pi-glm-chiap08-deadbeef:revision" in result.stdout


def test_same_card_across_three_worker_identities_is_never_joined(tmp_path: Path) -> None:
    probe = (
        "META\\tchiap08\\tok\\tsuccess\\n"
        "POOL\\tchiap08\\tPOOL|chiap08|ready=0 POOL_IDS|chiap08|ids=-\\n"
        "SESSION\\tchiap08\\tqwen-auto-deadbeef\\tdeadbeef\\tqwen\\tlive\\tpi"
        "\\tpi-qwen-chiap08-deadbeef\\t-"
    )
    claims = "CLAIM\\tdeadbeef\\tpi-glm-chiap02-deadbeef\\tdoing\\trevision\\tchiap02\\tglm"
    gateway = "GATEWAY\\tdeadbeef\\tpi-codex-chiap03-deadbeef\\t4\\tchiap03\\tcodex\\tcodex"
    result = run_bash(sample_script(probe=probe, claims=claims, gateway=gateway), tmp_path)

    assert result.returncode == 0, result.stderr
    assert "state=collector_fault" in result.stdout
    assert "unmatched_sessions=pi-qwen-chiap08-deadbeef:qwen-auto-deadbeef" in result.stdout
    assert "unmatched_claims=pi-glm-chiap02-deadbeef:revision" in result.stdout
    assert "unmatched_gateway=pi-codex-chiap03-deadbeef:codex:4" in result.stdout
    assert "state=up" not in result.stdout


def test_host_and_tmux_failure_are_distinct_from_honest_zero(tmp_path: Path) -> None:
    pool = "POOL\\tchiap08\\tPOOL|chiap08|ready=0 POOL_IDS|chiap08|ids=-"
    cases = [
        ("META\\tchiap08\\tunreachable\\tunavailable", "unavailable"),
        (f"META\\tchiap08\\tunavailable\\tsuccess\\n{pool}", "collector_fault"),
        (f"META\\tchiap08\\tempty\\tsuccess\\n{pool}", "zero"),
    ]
    for index, (probe, expected) in enumerate(cases):
        case_dir = tmp_path / str(index)
        result = run_bash(sample_script(probe=probe), case_dir)
        assert result.returncode == 0, result.stderr
        assert f"state={expected}" in result.stdout


def test_claim_collector_ignores_historical_owner_without_active_revision(tmp_path: Path) -> None:
    package = tmp_path / "modules" / "skcoord"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "card_store.py").write_text("""
class Value:
    def __init__(self, value): self.value = value
class Card:
    def __init__(self, card_id, owner, revision, status="doing"):
        self.id = card_id
        self.owner = owner
        self.status = Value(status)
        self.meta = {} if revision is None else {"_claim_revision": revision}
class CardStore:
    def __init__(self, home): pass
    def list_cards(self, include_archived=False):
        return [
            Card("11111111", "pi-qwen-chiap01-11111111", "live-revision"),
            Card("22222222", "pi-qwen-chiap01-22222222", None),
            Card("33333333", "pi-qwen-chiap01-33333333", "residue", "done"),
            Card("44444444", "pi-qwen-chiap01-44444444", "residue", "backlog"),
        ]
""")
    result = run_bash(
        "collect_claims",
        tmp_path,
        PYTHONPATH=str(tmp_path / "modules"),
    )

    assert result.returncode == 0, result.stderr
    assert "11111111" in result.stdout
    assert "live-revision" in result.stdout
    assert "22222222" not in result.stdout
    assert "33333333" not in result.stdout
    assert "44444444" not in result.stdout
