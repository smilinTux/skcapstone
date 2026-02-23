"""
Syncthing auto-setup skill for skcapstone / OpenClaw agents.

Detects, installs, and configures Syncthing for sovereign P2P
memory synchronization. Generates device IDs, shared folder config,
and optional QR codes for easy pairing.
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


SYNC_DIR = Path.home() / ".skcapstone" / "sync"
SYNCTHING_CONFIG_DIR = Path.home() / ".config" / "syncthing"
SYNCTHING_CONFIG_FILE = SYNCTHING_CONFIG_DIR / "config.xml"
SHARED_FOLDER_ID = "skcapstone-sync"


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
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("ID="):
                    return line.strip().split("=")[1].strip('"').lower()
                if line.startswith("ID_LIKE="):
                    like = line.strip().split("=")[1].strip('"').lower()
                    if "arch" in like:
                        return "arch"
                    if "debian" in like:
                        return "debian"
    except FileNotFoundError:
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
    """Create the skcapstone sync shared folder if it doesn't exist.

    Returns:
        Path: The shared folder path.
    """
    for subdir in ("outbox", "inbox", "archive"):
        (SYNC_DIR / subdir).mkdir(parents=True, exist_ok=True)
    return SYNC_DIR


def configure_syncthing_folder() -> bool:
    """Add the skcapstone sync folder to Syncthing config.

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

    for folder in root.iter("folder"):
        if folder.get("id") == SHARED_FOLDER_ID:
            return True

    folder_elem = ET.SubElement(root, "folder")
    folder_elem.set("id", SHARED_FOLDER_ID)
    folder_elem.set("label", "SKCapstone Sync")
    folder_elem.set("path", str(SYNC_DIR))
    folder_elem.set("type", "sendreceive")
    folder_elem.set("rescanIntervalS", "60")
    folder_elem.set("fsWatcherEnabled", "true")
    folder_elem.set("fsWatcherDelayS", "10")

    tree.write(str(SYNCTHING_CONFIG_FILE), xml_declaration=True)
    return True


def start_syncthing() -> bool:
    """Start Syncthing as a background process or systemd service.

    Returns:
        bool: True if started successfully.
    """
    # Reason: try systemd user service first, then fall back to direct launch
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

    subprocess.Popen(
        [st, "--no-browser", "--no-restart"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
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
    """Run the complete Syncthing setup flow.

    Returns:
        dict: Setup result with device_id, folder_path, status.
    """
    result = {
        "syncthing_installed": False,
        "device_id": None,
        "folder_path": str(SYNC_DIR),
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
