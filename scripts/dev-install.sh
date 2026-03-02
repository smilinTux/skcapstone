#!/bin/bash
set -euo pipefail
echo "=== Sovereign Agent Dev Installer ==="

# Check prerequisites
python3 --version || { echo "Python 3.11+ required"; exit 1; }
command -v pip3 >/dev/null || { echo "pip3 required"; exit 1; }

# Install packages in dependency order
cd "$(dirname "$0")/.."
pip install -e ../skseed/ 2>/dev/null || pip install -e skseed/
pip install -e ../skcomm/ 2>/dev/null || pip install -e skcomm/
pip install -e .[all,dev]

# Install dev/test tools
pip install pytest pytest-cov ruff

# Pull Ollama models (if Ollama installed)
if command -v ollama >/dev/null; then
  echo "Pulling Ollama models..."
  ollama pull llama3.2 || true
fi

# Initialize home directory
skcapstone doctor --fix 2>/dev/null || true

# Verify
python -c "import skcapstone; print('skcapstone OK')"
python -c "import skseed; print('skseed OK')"
python -c "import skcomm; print('skcomm OK')"
python -c "import pytest; print('pytest OK')"
echo "=== Dev installation complete ==="
echo "Run tests: pytest tests/"
echo "Lint:      ruff check src/"
