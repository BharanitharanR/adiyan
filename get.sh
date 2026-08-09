#!/usr/bin/env bash
# One-line installer for Adiyan.
#   curl -fsSL https://raw.githubusercontent.com/BharanitharanR/adiyan/main/get.sh | bash
#
# Downloads the latest release zip from GitHub, unzips it into ~/Adiyan,
# and runs its one-time setup (install.sh).
set -euo pipefail

REPO="BharanitharanR/adiyan"
DEST="$HOME/Adiyan"

log() { echo "[adiyan] $*"; }
fail() { echo "[adiyan] ERROR: $*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "Adiyan currently supports macOS only."

log "Looking up the latest release..."
ASSET_URL="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
  | grep -o '"browser_download_url": *"[^"]*\.zip"' \
  | head -n1 \
  | sed -E 's/"browser_download_url": *"([^"]*)"/\1/')"

[ -n "$ASSET_URL" ] || fail "Could not find a release zip. Visit https://github.com/${REPO}/releases and download it manually."

TMP_ZIP="$(mktemp -t adiyan).zip"
log "Downloading $(basename "$ASSET_URL")..."
curl -fL --progress-bar "$ASSET_URL" -o "$TMP_ZIP"

log "Unzipping to $DEST..."
rm -rf "$DEST"
mkdir -p "$DEST"
unzip -q "$TMP_ZIP" -d "$DEST"
rm -f "$TMP_ZIP"

# The zip contains a top-level "adiyan" folder - flatten it into $DEST.
if [ -d "$DEST/adiyan" ]; then
  shopt -s dotglob
  mv "$DEST/adiyan/"* "$DEST/"
  rmdir "$DEST/adiyan"
  shopt -u dotglob
fi

chmod +x "$DEST/"*.sh

log "Running one-time setup..."
bash "$DEST/install.sh"

log "Done! To use Adiyan any time, run:"
log "  bash $DEST/launch_adiyan.sh"
