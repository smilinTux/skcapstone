"""Operator preparation and immutable installation of native Node dependencies."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from . import production_node_test_recipe as native
from . import production_test_node as node
from . import production_test_plan as plan

# Existing fleet artifacts use this opaque marker. It is not a test verdict.
MARKER = b"after!"


def source_hashes(source: Path) -> dict[str, str]:
    """Bind regular root manifests and the exact bootstrap without following links."""
    result = {}
    for name in native.SOURCE_FILES:
        path = source / name
        if not path.is_file() or path.resolve() != source.resolve() / name:
            raise plan.TestEvidenceError("native artifact source missing or redirected")
        result[name] = plan.sha(path.read_bytes())
    return result


def prepare(source: Path, run: Path, *, offline: bool = False) -> Path:
    """Install locked packages in a fresh directory without any lifecycle scripts."""
    expected = source_hashes(source)
    run.mkdir(mode=0o700)
    artifact = run / "artifact"
    artifact.mkdir(mode=0o700)
    for name in native.SOURCE_FILES:
        target = artifact / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source / name).read_bytes())
    (run / "npm-home").mkdir(mode=0o700)
    for name in ("user.npmrc", "global.npmrc"):
        (run / name).write_text("")
    argv = ["/usr/bin/npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"]
    if offline:
        argv.append("--offline")
    env = {
        "PATH": str(node.NODE.parent) + ":/usr/bin:/bin",
        "HOME": str(run / "npm-home"),
        "NPM_CONFIG_CACHE": str(Path.home() / ".npm"),
        "NPM_CONFIG_USERCONFIG": str(run / "user.npmrc"),
        "NPM_CONFIG_GLOBALCONFIG": str(run / "global.npmrc"),
    }
    with (run / "npm-ci.log").open("xb") as log:
        subprocess.run(
            argv, cwd=artifact, env=env, stdout=log, stderr=log, check=True, timeout=600
        )
    if source_hashes(artifact) != expected or source_hashes(source) != expected:
        raise plan.TestEvidenceError("native artifact source changed during installation")
    return artifact


def install(source: Path, prepared: Path) -> dict:
    """Copy, seal and atomically publish dependencies, reusing only exact artifacts."""
    expected = source_hashes(source)
    if source_hashes(prepared) != expected:
        raise plan.TestEvidenceError("prepared native manifests or bootstrap differ from source")
    modules = prepared / "node_modules"
    if modules.is_symlink() or not modules.is_dir():
        raise plan.TestEvidenceError("prepared node_modules missing or redirected")
    plan.private_dir(node.ARTIFACT_ROOT, create=True)
    # Lookup resolves only the digest-named path, never this unpublished name.
    stage = Path(tempfile.mkdtemp(prefix=".node-artifact-", dir=node.ARTIFACT_ROOT))
    shutil.copytree(modules, stage / "node_modules", symlinks=True)
    for name in native.SOURCE_FILES:
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((prepared / name).read_bytes())
    if source_hashes(stage) != expected:
        raise plan.TestEvidenceError("native artifact source changed during copy")
    (stage / "qualification-marker").write_bytes(MARKER)
    for path in [*stage.rglob("*"), stage]:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            continue
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise plan.TestEvidenceError("native artifact contains special files")
        path.chmod(stat.S_IMODE(mode) & ~0o222)
    digest = node.artifact_digest(stage)
    destination = node.ARTIFACT_ROOT / digest
    if destination.exists() or destination.is_symlink():
        if node.artifact_digest(destination) != digest:
            raise plan.TestEvidenceError("existing native artifact digest mismatch")
        # A duplicate stage is ours; never chmod or overwrite the published tree.
        for path in [stage, *stage.rglob("*")]:
            if not path.is_symlink() and path.is_dir():
                path.chmod(path.stat().st_mode | 0o700)
        shutil.rmtree(stage)
    else:
        stage.rename(destination)
    value = {
        "node_sha256": plan.sha(node.NODE.read_bytes()),
        "platform": [platform.system(), platform.machine()],
        "artifact_sha256": digest,
        "source_sha256": expected,
    }
    node.validate_environment(value, source)
    if source_hashes(source) != expected:
        raise plan.TestEvidenceError("native artifact source changed during publication")
    return value


def main() -> None:
    """Expose explicit preparation and publication, leaving native builds to operators."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("source", type=Path)
    prepare_parser.add_argument("run", type=Path)
    prepare_parser.add_argument("--offline", action="store_true")
    install_parser = sub.add_parser("install")
    install_parser.add_argument("source", type=Path)
    install_parser.add_argument("prepared", type=Path)
    install_parser.add_argument("--operator", required=True)
    install_parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        print(prepare(args.source.resolve(), args.run.absolute(), offline=args.offline))
        return
    plan.private_dir(args.receipt.parent)
    if args.receipt.exists():
        raise plan.TestEvidenceError("operator receipt already exists")
    value = install(args.source.resolve(), args.prepared.resolve())
    receipt = {
        "schema": "skfleet.native-node-artifact-install/v1",
        "operator": args.operator,
        "host": platform.node(),
        "environment": value,
        "qualification_marker": MARKER.decode(),
        "product_tests_run": False,
    }
    plan.write_once(args.receipt, receipt)
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
