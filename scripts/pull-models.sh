#!/bin/bash
# Pull required Ollama models for the sovereign agent
set -euo pipefail

echo "Pulling Ollama models for SKCapstone..."

ollama pull llama3.2      # FAST tier (3.2B, ~2GB) — CPU-only inference
ollama pull devstral      # CODE tier — code generation and review

echo "Models ready"
