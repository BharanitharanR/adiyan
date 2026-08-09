#!/usr/bin/env bash
# Package OpenWA into a self-contained, runnable bundle: a portable Node
# runtime + the built app + production-only node_modules. The end user's
# machine never runs npm install or a TypeScript build.
set -euo pipefail

INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$INSTALLER_DIR")"
PENWA_DIR="$PROJECT_DIR/penwa"
OUT_DIR="$INSTALLER_DIR/dist_openwa"
NODE_RUNTIME_DIR="$INSTALLER_DIR/node-runtime"

NODE_VERSION="v22.23.2"  # matches penwa's package.json engines: node >=22.13
NODE_URL="https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-darwin-arm64.tar.gz"

log() { echo "[build_openwa] $*" >&2; }

download_node_runtime() {
    if [ -x "$NODE_RUNTIME_DIR/bin/node" ]; then
        log "Portable Node runtime already present"
        return
    fi

    log "Downloading portable Node ${NODE_VERSION}..."
    mkdir -p "$NODE_RUNTIME_DIR"
    local tmp_tgz
    tmp_tgz="$(mktemp)"
    curl -sL -o "$tmp_tgz" "$NODE_URL"

    log "Extracting..."
    tar -xzf "$tmp_tgz" -C "$NODE_RUNTIME_DIR" --strip-components=1
    rm -f "$tmp_tgz"
    log "Node runtime ready: $("$NODE_RUNTIME_DIR/bin/node" --version)"
}

build_app() {
    log "Building OpenWA (nest build)..."
    (cd "$PENWA_DIR" && rm -rf dist && npm run build)
}

package_app() {
    log "Staging production bundle..."
    rm -rf "$OUT_DIR"
    mkdir -p "$OUT_DIR/app"

    cp -r "$PENWA_DIR/dist" "$OUT_DIR/app/dist"
    cp "$PENWA_DIR/package.json" "$OUT_DIR/app/package.json"
    cp "$PENWA_DIR/package-lock.json" "$OUT_DIR/app/package-lock.json" 2>/dev/null || true

    # postinstall applies real bug-fix patches to whatsapp-web.js (not just setup) -
    # scripts/ has to be present for `npm install` to run it correctly.
    cp -r "$PENWA_DIR/scripts" "$OUT_DIR/app/scripts"

    log "Installing production-only dependencies into the bundle (this re-resolves native modules cleanly, rather than copying+pruning the dev tree)..."
    (
        cd "$OUT_DIR/app"
        "$NODE_RUNTIME_DIR/bin/npm" install --omit=dev --no-audit --no-fund
    )

    log "Bundle staged at $OUT_DIR"
}

main() {
    download_node_runtime
    build_app
    package_app

    local size
    size="$(du -sh "$OUT_DIR" | cut -f1)"
    log "Done. Bundle size: $size"
    log "Run with: $NODE_RUNTIME_DIR/bin/node $OUT_DIR/app/dist/main.js"
}

main "$@"
