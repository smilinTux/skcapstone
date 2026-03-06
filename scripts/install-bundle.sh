#!/bin/bash
# SKCapstone Complete Bundle Installer
# Installs skcapstone + skmemory + sksecurity + cloud9-protocol as a unified package

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "🚀 SKCapstone Complete Bundle Installer"
echo "========================================"
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check Python version
print_status "Checking Python version..."
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
REQUIRED_VERSION="3.10"

if [ "$(printf '%s\n' "$REQUIRED_VERSION" "$PYTHON_VERSION" | sort -V | head -n1)" != "$REQUIRED_VERSION" ]; then 
    print_error "Python 3.10+ required. Found: $PYTHON_VERSION"
    exit 1
fi
print_status "✓ Python $PYTHON_VERSION detected"

# Check pip
print_status "Checking pip..."
if ! command -v pip3 &> /dev/null; then
    print_error "pip3 not found. Please install pip."
    exit 1
fi
print_status "✓ pip3 available"

# Define package paths
SKMEMORY_PATH="${REPO_ROOT}/../skcapstone-repos/skmemory"
SKSECURITY_PATH="${REPO_ROOT}/../skcapstone-repos/sksecurity"
CLOUD9_PATH="${REPO_ROOT}/../skcapstone-repos/cloud9-python"
SKCAPSTONE_PATH="${REPO_ROOT}"

# Check if repos exist
print_status "Checking SK repositories..."
for repo_path in "$SKMEMORY_PATH" "$SKSECURITY_PATH" "$CLOUD9_PATH"; do
    if [ ! -d "$repo_path" ]; then
        print_error "Repository not found: $repo_path"
        print_error "Please clone all SK repositories first:"
        print_error "  git clone https://github.com/smilinTux/skmemory.git"
        print_error "  git clone https://github.com/smilinTux/sksecurity.git"
        print_error "  git clone https://github.com/smilinTux/cloud9-python.git"
        exit 1
    fi
done
print_status "✓ All repositories found"

# Install in dependency order
print_status "Installing packages in dependency order..."
print_status "Order: cloud9 → skmemory → sksecurity → skcapstone"

cd "$CLOUD9_PATH"
print_status "Installing cloud9-protocol..."
pip3 install -e .

cd "$SKMEMORY_PATH"
print_status "Installing skmemory..."
pip3 install -e ".[skvector]"

cd "$SKSECURITY_PATH"
print_status "Installing sksecurity..."
pip3 install -e .

cd "$SKCAPSTONE_PATH"
print_status "Installing skcapstone (with all dependencies)..."
pip3 install -e .

# Verify installation
print_status "Verifying installation..."
python3 << 'EOF'
import sys
try:
    import skmemory
    print("  ✓ skmemory:", skmemory.__version__ if hasattr(skmemory, '__version__') else "installed")
except ImportError as e:
    print("  ✗ skmemory: FAILED -", e)
    sys.exit(1)

try:
    import sksecurity
    print("  ✓ sksecurity:", sksecurity.__version__ if hasattr(sksecurity, '__version__') else "installed")
except ImportError as e:
    print("  ✗ sksecurity: FAILED -", e)
    sys.exit(1)

try:
    import cloud9_protocol
    print("  ✓ cloud9-protocol:", cloud9_protocol.__version__ if hasattr(cloud9_protocol, '__version__') else "installed")
except ImportError as e:
    print("  ✗ cloud9-protocol: FAILED -", e)
    sys.exit(1)

try:
    import skcapstone
    print("  ✓ skcapstone:", skcapstone.__version__ if hasattr(skcapstone, '__version__') else "installed")
except ImportError as e:
    print("  ✗ skcapstone: FAILED -", e)
    sys.exit(1)

print("\n✓ All packages installed successfully!")
EOF

# Setup agent memory
print_status "Setting up agent memory structure..."
mkdir -p ~/.skcapstone/agent/lumina/memory/{short,medium,long}
mkdir -p ~/.skcapstone/agent/lumina/coordination/{inbox,plebeian,archive}
mkdir -p ~/.skcapstone/agent/lumina/config
mkdir -p ~/.skcapstone/agent/lumina/cron
mkdir -p ~/.skcapstone/agent/lumina/logs

print_status "✓ Memory directories created"

# Check for SQLite
print_status "Checking SQLite configuration..."
python3 -c "
import sqlite3
print(f'  ✓ SQLite version: {sqlite3.sqlite_version}')
print(f'  ✓ SQLite module: {sqlite3.version}')

# Test database creation
test_db = '~/.skcapstone/agent/lumina/test.db'
import os
os.makedirs(os.path.dirname(os.path.expanduser(test_db)), exist_ok=True)
conn = sqlite3.connect(os.path.expanduser(test_db))
conn.execute('CREATE TABLE IF NOT EXISTS test (id INTEGER PRIMARY KEY)')
conn.close()
os.remove(os.path.expanduser(test_db))
print('  ✓ SQLite database operations working')
"

echo ""
echo "========================================"
echo "✅ Installation Complete!"
echo "========================================"
echo ""
echo "Installed packages:"
echo "  • skcapstone (sovereign agent framework)"
echo "  • skmemory (universal AI memory)"
echo "  • sksecurity (enterprise security)"
echo "  • cloud9-protocol (emotional continuity)"
echo ""
echo "Next steps:"
echo "  1. Run: skcapstone doctor"
echo "  2. Run: skcapstone init"
echo "  3. Check for updates: ~/.skcapstone/scripts/check-updates.py"
echo ""
echo "For help: skcapstone --help"
