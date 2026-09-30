"""Resolve one operator-built, read-only Python test dependency prefix.

A queue job may carry an optional test_environment selector with exactly
lock_sha256 (64 lowercase hex) and python=3.12. Selection is fail-closed:
absent means the existing stdlib-only behavior, and anything malformed,
missing, unready, symlinked or bound to a different repository is refused
before model execution. The worker never installs, downloads or writes a
dependency; it only mounts an operator-built prefix read-only.
"""
import hashlib
import json
import os
from pathlib import Path
import re

from fiber_admission import Refused

DATA = Path.home() / '.local/share/skfleet-fiber'
SCHEMA = 'schema1'
PYTHON = 'python3.12'
LOCK_NAME = 'uv.lock'
SOURCE_DIR = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_./-]*')
MAX_SOURCE_DIRS = 32


def select(job):
    """Return the normalized selector, or None for the stdlib-only default.

    The selector may contain only lock_sha256 and python. Any extra or
    malformed field is refused so no task can smuggle a host path.
    """
    if job is None:
        return None
    if not isinstance(job, dict):
        raise Refused('invalid-job')
    selector = job.get('test_environment')
    if selector is None:
        return None
    if not isinstance(selector, dict) or set(selector) != {'lock_sha256', 'python'}:
        raise Refused('test-environment-selector-invalid')
    lock = selector.get('lock_sha256')
    if not isinstance(lock, str) or not re.fullmatch('[0-9a-f]{64}', lock):
        raise Refused('test-environment-selector-invalid')
    if selector.get('python') != '3.12':
        raise Refused('test-environment-selector-invalid')
    return {'lock_sha256': lock, 'python': '3.12'}


def _safe_source_dir(value):
    """Accept only a bounded repository-relative Python source directory."""
    if not isinstance(value, str) or not value or len(value) > 256:
        raise Refused('test-environment-source-dir')
    if value.startswith('/') or value.startswith('~') or '\\' in value or '\x00' in value:
        raise Refused('test-environment-source-dir')
    if not SOURCE_DIR.fullmatch(value):
        raise Refused('test-environment-source-dir')
    parts = value.split('/')
    if any(part in {'', '.', '..'} for part in parts):
        raise Refused('test-environment-source-dir')
    return value


def qualify(selector, repository):
    """Read and validate the operator manifest for one exact repository.

    The artifact directory and manifest must not be symlinks, the manifest
    must bind schema1, the exact repository string, the selected lock digest,
    python3.12 and ready=true, and every source directory must be safe.
    """
    lock = selector['lock_sha256']
    base = DATA / 'test-environments'
    artifact = base / lock
    if artifact.is_symlink() or base.is_symlink() or not artifact.is_dir():
        if artifact.exists() or artifact.is_symlink():
            raise Refused('test-environment-path')
        raise Refused('test-environment-unavailable')
    # Resolve once and confirm the resolved artifact still lives directly
    # under the trusted base; this refuses a symlinked parent component.
    resolved = artifact.resolve(strict=True)
    if resolved.parent != base.resolve(strict=True) or resolved.name != lock:
        raise Refused('test-environment-path')
    manifest_path = artifact / 'manifest.json'
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise Refused('test-environment-path')
    try:
        document = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        raise Refused('test-environment-manifest-invalid') from exc
    if not isinstance(document, dict):
        raise Refused('test-environment-manifest-invalid')
    expected = {'schema', 'repository', 'lock_sha256', 'python', 'ready', 'source_dirs'}
    if set(document) != expected:
        raise Refused('test-environment-manifest-mismatch')
    if (document.get('schema') != SCHEMA or document.get('lock_sha256') != lock
            or document.get('python') != PYTHON):
        raise Refused('test-environment-manifest-mismatch')
    if document.get('repository') != repository:
        raise Refused('test-environment-repository-mismatch')
    if document.get('ready') is not True:
        raise Refused('test-environment-not-ready')
    source_dirs = document.get('source_dirs')
    if not isinstance(source_dirs, list) or len(source_dirs) > MAX_SOURCE_DIRS:
        raise Refused('test-environment-source-dir')
    checked = [_safe_source_dir(item) for item in source_dirs]
    if len(set(checked)) != len(checked):
        raise Refused('test-environment-source-dir')
    venv = artifact / 'venv'
    if venv.is_symlink() or not (venv / 'bin').is_dir():
        raise Refused('test-environment-unavailable')
    return {'venv': str(venv), 'lock_sha256': lock, 'source_dirs': checked}


def verify_lock(path, expected):
    """Require the workspace uv.lock bytes to equal the selected digest."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise Refused('test-environment-lock-missing')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise Refused('test-environment-lock-mismatch')
    return digest
