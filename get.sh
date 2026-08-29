#!/usr/bin/env bash
# Adiyan Platform - one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/BharanitharanR/adiyan/main/get.sh | bash
#
# Run on a Mac with no copy of Adiyan yet - this is the script that gets one.
# Clones the repo, then hands off to its own install.sh for everything else
# (Python venv, Qdrant, Ollama models, OpenWA dependencies). Doesn't start
# anything itself - same as install.sh, starting Adiyan is always a
# separate, explicit step you run yourself afterward.
set -euo pipefail

REPO_URL="https://github.com/BharanitharanR/adiyan.git"
INSTALL_DIR="$HOME/Adiyan"

BOLD='\033[1m'
GREEN='\033[32m'
RED='\033[31m'
RESET='\033[0m'

step() { echo -e "\n${BOLD}==> $1${RESET}"; }
ok()   { echo -e "${GREEN}  ok${RESET} - $1"; }
fail() { echo -e "${RED}$1${RESET}"; exit 1; }

step "Getting Adiyan"
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "  Already at $INSTALL_DIR - pulling the latest version instead of a fresh clone."
    git -C "$INSTALL_DIR" pull --ff-only
elif [ -d "$INSTALL_DIR" ]; then
    fail "$INSTALL_DIR already exists but isn't an Adiyan checkout - move or remove it first, then re-run this."
else
    command -v git >/dev/null 2>&1 || fail "Git isn't installed - install it first, then re-run this: xcode-select --install"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi
ok "Adiyan is at $INSTALL_DIR"

step "Running setup"
cd "$INSTALL_DIR"
chmod +x install.sh
./install.sh

echo -e "\n${GREEN}${BOLD}Adiyan is ready.${RESET}"
echo -e "Whenever you want to start it: ${BOLD}cd $INSTALL_DIR && mesh/start_all.sh${RESET}"
echo -e "The first time it starts, you'll link your WhatsApp by scanning a QR code, same as WhatsApp Web."
