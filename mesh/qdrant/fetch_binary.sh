#!/usr/bin/env bash
# Downloads the portable Qdrant binary this mesh runs against (no Homebrew
# formula exists for it - confirmed via `brew search qdrant`, unlike
# nginx/mongodb). Not committed to git (see .gitignore) - this script is
# what reproduces mesh/qdrant/qdrant-bin on a fresh machine.
set -euo pipefail

QDRANT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QDRANT_VERSION="v1.19.0"
QDRANT_URL="https://github.com/qdrant/qdrant/releases/download/${QDRANT_VERSION}/qdrant-aarch64-apple-darwin.tar.gz"

if [ -x "$QDRANT_DIR/qdrant-bin" ]; then
    echo "qdrant-bin already present at $QDRANT_DIR/qdrant-bin"
    exit 0
fi

echo "Downloading Qdrant ${QDRANT_VERSION}..."
tmp_tgz="$(mktemp)"
curl -fsSL -o "$tmp_tgz" "$QDRANT_URL"

tmp_dir="$(mktemp -d)"
tar -xzf "$tmp_tgz" -C "$tmp_dir"
mv "$tmp_dir/qdrant" "$QDRANT_DIR/qdrant-bin"
chmod +x "$QDRANT_DIR/qdrant-bin"
rm -rf "$tmp_tgz" "$tmp_dir"

echo "Ready: $("$QDRANT_DIR/qdrant-bin" --version 2>&1 | head -1)"
