"""
Sovereign Sync -- encrypted agent state synchronization.

The vault never travels naked. Every push encrypts with CapAuth PGP.
Every pull verifies the signature before restoring.

Backends: Syncthing (real-time P2P), GitHub, Forgejo, Google Drive, local filesystem.
The human picks the pipe. The agent secures the payload.
"""

from .engine import SyncEngine
from .vault import Vault

__all__ = ["SyncEngine", "Vault"]
