#!/usr/bin/env bash
# Adiyan Platform - one-time setup for a fresh Mac.
#
# Sets up everything this mesh needs to run: a Python venv with its
# dependencies, the Qdrant binary, the OpenWA/Node dependencies, and the
# three Ollama models every agent's LLM stage relies on. Does NOT start any
# service itself - mesh/start_all.sh is the one thing that starts/stops
# processes, and that's a deliberate, separate, explicit step you run
# yourself once this script finishes.
#
# Run from the repo root:
#   ./install.sh
#
# Safe to re-run - every step checks whether its own work is already done
# before doing it again, the same idempotent shape mesh/start_all.sh's own
# "start is a no-op for something already running" already follows.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

BOLD='\033[1m'
DIM='\033[2m'
RED='\033[31m'
GREEN='\033[32m'
RESET='\033[0m'

step() { echo -e "\n${BOLD}==> $1${RESET}"; }
ok()   { echo -e "${GREEN}  ok${RESET} - $1"; }
skip() { echo -e "${DIM}  skip${RESET} - $1"; }
fail() { echo -e "${RED}  missing${RESET} - $1"; MISSING=1; }

MISSING=0

step "Checking prerequisites"

if ! command -v brew >/dev/null 2>&1; then
    fail "Homebrew not found - install it first: https://brew.sh"
else
    ok "Homebrew"
fi

PYTHON_BIN=""
for candidate in python3.11 python3.12 python3.13 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        version="$("$candidate" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
        major="${version%%.*}"
        minor="${version##*.}"
        if [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON_BIN="$candidate"
            break
        fi
    fi
done
if [ -z "$PYTHON_BIN" ]; then
    fail "Python 3.11+ not found - install with: brew install python@3.11"
else
    ok "Python ($PYTHON_BIN, $("$PYTHON_BIN" --version 2>&1))"
fi

if ! command -v node >/dev/null 2>&1; then
    fail "Node.js not found - install with: brew install node"
else
    ok "Node ($(node --version))"
fi

if ! command -v mongod >/dev/null 2>&1; then
    fail "MongoDB not found - install with: brew tap mongodb/brew && brew install mongodb-community"
else
    ok "MongoDB"
fi

if ! command -v ollama >/dev/null 2>&1; then
    fail "Ollama not found - install with: brew install ollama"
else
    ok "Ollama"
fi

if [ "$MISSING" -eq 1 ]; then
    echo -e "\n${RED}One or more prerequisites are missing - install them with the commands above, then re-run ./install.sh.${RESET}"
    exit 1
fi

step "Python virtual environment"
if [ -d ".venv" ]; then
    skip ".venv already exists"
else
    "$PYTHON_BIN" -m venv .venv
    ok "Created .venv"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
ok "Python dependencies installed"

step "Qdrant binary"
if [ -x "mesh/qdrant/qdrant-bin" ]; then
    skip "mesh/qdrant/qdrant-bin already present"
else
    bash mesh/qdrant/fetch_binary.sh
fi

step "OpenWA (WhatsApp) Node dependencies"
if [ -d "penwa/node_modules" ]; then
    skip "penwa/node_modules already present"
else
    npm --prefix penwa install
    ok "penwa dependencies installed"
fi

step "Ollama models"
# Base models pulled from Ollama's own library. qwen3:8b-16k is not a
# stock tag - it's this deployment's own 16k-context variant of qwen3:8b
# (every agent's own runtime_config.json/seed_config.json assumes this
# exact tag exists), built here via a Modelfile rather than pulled.
pull_if_missing() {
    local model="$1"
    if ollama list | awk '{print $1}' | grep -qx "$model"; then
        skip "$model already pulled"
    else
        echo "Pulling $model (this can take a while - it's a multi-GB download)..."
        ollama pull "$model"
    fi
}
pull_if_missing "qwen3:8b"
pull_if_missing "qwen3-vl:8b"
pull_if_missing "nomic-embed-text"

if ollama list | awk '{print $1}' | grep -qx "qwen3:8b-16k"; then
    skip "qwen3:8b-16k already built"
else
    echo "Building qwen3:8b-16k (qwen3:8b with a 16384-token context window)..."
    modelfile="$(mktemp)"
    printf 'FROM qwen3:8b\nPARAMETER num_ctx 16384\n' > "$modelfile"
    ollama create qwen3:8b-16k -f "$modelfile"
    rm -f "$modelfile"
    ok "Built qwen3:8b-16k"
fi

step "Secrets"
echo -e "${DIM}  This mesh stores secrets in the macOS Keychain, not a .env file. Set one with:${RESET}"
echo -e "${DIM}    .venv/bin/python3 -m mesh.tools.set_secret PERMISSIONS_JWT_SECRET${RESET}"
echo -e "${DIM}  See which known keys are already set: .venv/bin/python3 -m mesh.tools.set_secret --list${RESET}"

echo -e "\n${GREEN}${BOLD}Setup complete.${RESET}"
echo -e "Next: run ${BOLD}mesh/start_all.sh${RESET} when you're ready to start the mesh - this script deliberately doesn't start anything itself."
