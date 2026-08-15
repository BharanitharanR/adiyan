#!/usr/bin/env bash
# Downloads the portable Qdrant binary Adiyan bundles for its coaching-history
# memory index (services/qdrant_service.py). Mirrors build_openwa.sh's
# download_node_runtime - the binary itself isn't committed to git (see
# .gitignore), this script is what reproduces it.
set -euo pipefail

INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QDRANT_RUNTIME_DIR="$INSTALLER_DIR/qdrant-runtime"

QDRANT_VERSION="v1.19.0"
QDRANT_URL="https://github.com/qdrant/qdrant/releases/download/${QDRANT_VERSION}/qdrant-aarch64-apple-darwin.tar.gz"

log() { echo "[build_qdrant] $*" >&2; }
fail() { log "ERROR: $*"; exit 1; }

download_qdrant_runtime() {
    if [ -x "$QDRANT_RUNTIME_DIR/qdrant" ]; then
        log "Qdrant runtime already present"
        return
    fi

    log "Downloading Qdrant ${QDRANT_VERSION}..."
    mkdir -p "$QDRANT_RUNTIME_DIR"
    local tmp_tgz
    tmp_tgz="$(mktemp)"
    curl -fsSL -o "$tmp_tgz" "$QDRANT_URL"

    log "Extracting..."
    tar -xzf "$tmp_tgz" -C "$QDRANT_RUNTIME_DIR"
    rm -f "$tmp_tgz"
    chmod +x "$QDRANT_RUNTIME_DIR/qdrant"
    log "Qdrant runtime ready: $("$QDRANT_RUNTIME_DIR/qdrant" --version 2>&1 | head -1)"
}

download_qdrant_runtime
