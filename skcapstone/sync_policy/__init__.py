"""Private-material policy enforcement for every Syncthing folder root.

Discovers every configured Syncthing folder, resolves symlinks, and fails
closed when a folder can synchronize private keys, revocation certificates,
passphrases, secret keyrings, or token stores. The CLI surface is
``skcapstone sync audit``.
"""

from .core import (
    FINGERPRINT_FILE_NAMES,
    MAX_WALK_ENTRIES,
    apply_remediation,
    audit,
    audit_folder,
)
from .discovery import (
    CONFIG_CANDIDATES,
    DiscoveredFolder,
    default_config_paths,
    discover_folders,
    parse_config_folders,
)
from .model import (
    MATERIAL_CLASSES,
    Finding,
    FolderReport,
    MaterialClass,
    SyncPolicyReport,
)
from .stignore import Coverage, compile_pattern, evaluate, load_ruleset

__all__ = [
    "CONFIG_CANDIDATES",
    "FINGERPRINT_FILE_NAMES",
    "MATERIAL_CLASSES",
    "MAX_WALK_ENTRIES",
    "Coverage",
    "DiscoveredFolder",
    "Finding",
    "FolderReport",
    "MaterialClass",
    "SyncPolicyReport",
    "apply_remediation",
    "audit",
    "audit_folder",
    "compile_pattern",
    "default_config_paths",
    "discover_folders",
    "evaluate",
    "load_ruleset",
    "parse_config_folders",
]
