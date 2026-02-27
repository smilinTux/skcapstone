"""
Syncthing auto-setup skill for skcapstone / OpenClaw agents.

Detects, installs, and configures Syncthing for Sovereign Singularity —
real-time P2P sync of the entire agent home directory. Identity, memory,
trust, security, coordination, and sync seeds all propagate across every
node in the mesh automatically.
"""

import json
import os
import platform
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional


AGENT_HOME = Path.home() / ".skcapstone"
SYNC_DIR = AGENT_HOME / "sync"
SHARED_FOLDER_ID = "skcapstone-sync"

# Reason: .stignore protects private keys from leaving this node
STIGNORE_CONTENTS = """\
// SKCapstone Sovereign Singularity — Syncthing ignore rules
// Private key material must never leave this node
*.key
*.pem
**/private.*

// Python cache
__pycache__
*.pyc
*.pyo

// OS metadata
.DS_Store
Thumbs.db
desktop.ini
"""

# Reason: Syncthing stores its config in different locations per platform
if platform.system() == "Windows":
    _local_app = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    SYNCTHING_CONFIG_DIR = Path(_local_app) / "Syncthing"
else:
    SYNCTHING_CONFIG_DIR = Path.home() / ".config" / "syncthing"

SYNCTHING_CONFIG_FILE = SYNCTHING_CONFIG_DIR / "config.xml"


def detect_syncthing() -> Optional[str]:
    """Check if Syncthing is installed and return its path.

    Returns:
        Optional[str]: Path to syncthing binary, or None.
    """
    return shutil.which("syncthing")


def get_install_instructions() -> str:
    """Return OS-appropriate install instructions for Syncthing.

    Returns:
        str: Human-readable install instructions.
    """
    system = platform.system().lower()
    if system == "linux":
        distro = _detect_linux_distro()
        if distro in ("ubuntu", "debian"):
            return (
                "Install Syncthing on Debian/Ubuntu:\n"
                "  sudo apt install syncthing\n"
                "Or via official repo:\n"
                "  curl -s https://syncthing.net/release-key.gpg | "
                "sudo gpg --dearmor -o /usr/share/keyrings/syncthing-archive-keyring.gpg\n"
                '  echo "deb [signed-by=/usr/share/keyrings/syncthing-archive-keyring.gpg] '
                'https://apt.syncthing.net/ syncthing stable" | '
                "sudo tee /etc/apt/sources.list.d/syncthing.list\n"
                "  sudo apt update && sudo apt install syncthing"
            )
        elif distro in ("arch", "manjaro"):
            return "Install Syncthing on Arch/Manjaro:\n  sudo pacman -S syncthing"
        elif distro in ("fedora", "centos", "rhel"):
            return "Install Syncthing on Fedora/RHEL:\n  sudo dnf install syncthing"
        else:
            return "Install Syncthing:\n  https://syncthing.net/downloads/"
    elif system == "darwin":
        return "Install Syncthing on macOS:\n  brew install syncthing"
    elif system == "windows":
        return (
            "Install Syncthing on Windows:\n"
            "  winget install SyncthingFoundation.Syncthing\n"
            "Or download from: https://syncthing.net/downloads/"
        )
    return "Install Syncthing from: https://syncthing.net/downloads/"


def _detect_linux_distro() -> str:
    """Detect the Linux distribution family.

    Returns:
        str: Distribution identifier (e.g., 'ubuntu', 'arch').
    """
    if platform.system() != "Linux":
        return "unknown"

    os_release = Path("/etc/os-release")
    if not os_release.exists():
        return "unknown"

    try:
        text = os_release.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("ID="):
                return line.strip().split("=")[1].strip('"').lower()
            if line.startswith("ID_LIKE="):
                like = line.strip().split("=")[1].strip('"').lower()
                if "arch" in like:
                    return "arch"
                if "debian" in like:
                    return "debian"
    except OSError:
        pass
    return "unknown"


def get_device_id() -> Optional[str]:
    """Get the local Syncthing device ID.

    Returns:
        Optional[str]: The device ID string, or None if not available.
    """
    st = detect_syncthing()
    if not st:
        return None
    try:
        result = subprocess.run(
            [st, "--device-id"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fallback: parse config.xml
    if SYNCTHING_CONFIG_FILE.exists():
        try:
            tree = ET.parse(SYNCTHING_CONFIG_FILE)
            root = tree.getroot()
            for device in root.iter("device"):
                if device.get("name") == platform.node():
                    return device.get("id")
        except ET.ParseError:
            pass
    return None


def ensure_shared_folder() -> Path:
    """Create the full skcapstone agent home directory structure.

    Ensures every pillar data directory exists so Syncthing has a
    complete tree to replicate across nodes. Also writes the .stignore
    to prevent private key material from leaving the node.

    Returns:
        Path: The agent home path (the Syncthing share root).
    """
    AGENT_HOME.mkdir(parents=True, exist_ok=True)

    pillar_dirs = [
        AGENT_HOME / "identity",
        AGENT_HOME / "memory" / "short-term",
        AGENT_HOME / "memory" / "mid-term",
        AGENT_HOME / "memory" / "long-term",
        AGENT_HOME / "trust" / "febs",
        AGENT_HOME / "security",
        AGENT_HOME / "coordination" / "tasks",
        AGENT_HOME / "coordination" / "agents",
        AGENT_HOME / "config",
        AGENT_HOME / "skills",
        SYNC_DIR / "outbox",
        SYNC_DIR / "inbox",
        SYNC_DIR / "archive",
    ]
    for d in pillar_dirs:
        d.mkdir(parents=True, exist_ok=True)

    _write_stignore()
    return AGENT_HOME


def _write_stignore() -> Path:
    """Write the .stignore file to the agent home directory.

    Syncthing reads this to know which files should never propagate
    to other nodes (private keys, cache files, etc.).

    Returns:
        Path: The .stignore file path.
    """
    stignore_path = AGENT_HOME / ".stignore"
    stignore_path.write_text(STIGNORE_CONTENTS, encoding="utf-8")
    return stignore_path


def configure_syncthing_folder() -> bool:
    """Add or update the skcapstone shared folder in Syncthing config.

    Points Syncthing at the entire agent home (~/.skcapstone/) so all
    pillar data — identity, memory, trust, security, coordination, and
    sync seeds — replicates automatically across every node.

    If an older config pointed at the sync/ subfolder, it gets upgraded
    to share the full agent home instead.

    Returns:
        bool: True if configuration was added/updated.
    """
    if not SYNCTHING_CONFIG_FILE.exists():
        return False

    try:
        tree = ET.parse(SYNCTHING_CONFIG_FILE)
        root = tree.getroot()
    except ET.ParseError:
        return False

    agent_home_str = str(AGENT_HOME)
    old_sync_str = str(SYNC_DIR)

    for folder in root.iter("folder"):
        if folder.get("id") == SHARED_FOLDER_ID:
            current_path = folder.get("path", "")
            if current_path == agent_home_str:
                return True
            # Reason: upgrade old sync/-only share to full agent home
            folder.set("path", agent_home_str)
            folder.set("label", "SKCapstone Sovereign")
            tree.write(str(SYNCTHING_CONFIG_FILE), xml_declaration=True)
            return True

    folder_elem = ET.SubElement(root, "folder")
    folder_elem.set("id", SHARED_FOLDER_ID)
    folder_elem.set("label", "SKCapstone Sovereign")
    folder_elem.set("path", agent_home_str)
    folder_elem.set("type", "sendreceive")
    folder_elem.set("rescanIntervalS", "60")
    folder_elem.set("fsWatcherEnabled", "true")
    folder_elem.set("fsWatcherDelayS", "10")

    tree.write(str(SYNCTHING_CONFIG_FILE), xml_declaration=True)
    return True


def start_syncthing() -> bool:
    """Start Syncthing as a background process or systemd service.

    On Linux: tries systemd first, then falls back to direct launch.
    On Windows/macOS: launches directly as a detached background process.

    Returns:
        bool: True if started successfully.
    """
    # Reason: systemctl is Linux-only; skip on other platforms
    if platform.system() == "Linux":
        result = subprocess.run(
            ["systemctl", "--user", "start", "syncthing.service"],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return True

    st = detect_syncthing()
    if not st:
        return False

    popen_kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    # Reason: on Windows, CREATE_NO_WINDOW prevents a console flash
    if platform.system() == "Windows":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        )

    subprocess.Popen([st, "--no-browser", "--no-restart"], **popen_kwargs)
    return True


def generate_qr_code(device_id: str) -> Optional[str]:
    """Generate a QR code for the device ID.

    Args:
        device_id: Syncthing device ID string.

    Returns:
        Optional[str]: ASCII QR code string, or None if qrcode not installed.
    """
    try:
        import qrcode
        from io import StringIO

        qr = qrcode.QRCode(box_size=1, border=1)
        qr.add_data(device_id)
        qr.make(fit=True)

        buf = StringIO()
        qr.print_ascii(out=buf)
        return buf.getvalue()
    except ImportError:
        return None


def add_remote_device(device_id: str, name: str = "peer") -> bool:
    """Add a remote device to Syncthing config for pairing.

    Args:
        device_id: The remote device's Syncthing device ID.
        name: Friendly name for the device.

    Returns:
        bool: True if device was added.
    """
    if not SYNCTHING_CONFIG_FILE.exists():
        return False

    try:
        tree = ET.parse(SYNCTHING_CONFIG_FILE)
        root = tree.getroot()
    except ET.ParseError:
        return False

    for device in root.iter("device"):
        if device.get("id") == device_id:
            return True

    device_elem = ET.SubElement(root, "device")
    device_elem.set("id", device_id)
    device_elem.set("name", name)
    device_elem.set("compression", "metadata")
    device_elem.set("introducer", "false")

    # Also add this device to the shared folder
    for folder in root.iter("folder"):
        if folder.get("id") == SHARED_FOLDER_ID:
            dev_ref = ET.SubElement(folder, "device")
            dev_ref.set("id", device_id)
            break

    tree.write(str(SYNCTHING_CONFIG_FILE), xml_declaration=True)
    return True


def full_setup() -> dict:
    """Run the complete Syncthing setup flow for Sovereign Singularity.

    Creates the full agent home directory structure, configures Syncthing
    to share the entire ~/.skcapstone/ tree, and starts the daemon.

    Returns:
        dict: Setup result with device_id, folder_path, status.
    """
    result = {
        "syncthing_installed": False,
        "device_id": None,
        "folder_path": str(AGENT_HOME),
        "folder_configured": False,
        "started": False,
        "qr_code": None,
        "install_instructions": None,
    }

    st_path = detect_syncthing()
    if not st_path:
        result["install_instructions"] = get_install_instructions()
        return result

    result["syncthing_installed"] = True

    ensure_shared_folder()
    result["folder_configured"] = configure_syncthing_folder()

    result["started"] = start_syncthing()

    device_id = get_device_id()
    result["device_id"] = device_id

    if device_id:
        result["qr_code"] = generate_qr_code(device_id)

    return result
