from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"


def _job(text: str, name: str, next_name: str | None = None) -> str:
    section = text.split(f"  {name}:\n", 1)[1]
    return section.split(f"  {next_name}:\n", 1)[0] if next_name else section


def test_release_waits_for_successful_current_main_ci() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    triggers = text.split("permissions:", 1)[0]
    tag = _job(text, "tag", "build")
    build = _job(text, "build", "pypi-publish")

    assert "workflow_run:" in triggers
    assert 'workflows: ["CI"]' in triggers
    assert "types: [completed]" in triggers
    assert "branches: [main]" in triggers
    assert 'branches: ["main"]' not in triggers
    assert "workflow_dispatch:" not in triggers

    assert "github.event.workflow_run.conclusion == 'success'" in tag
    assert "ref: ${{ github.event.workflow_run.head_sha }}" in tag
    assert 'target_sha="${{ github.event.workflow_run.head_sha }}"' in tag
    assert "refs/remotes/origin/main" in tag
    assert 'if [ "$target_sha" != "$current_main" ]; then' in tag

    assert "github.event_name == 'push'" in build
    assert "startsWith(github.ref, 'refs/tags/')" in build
    assert "needs.tag.outputs" not in build
