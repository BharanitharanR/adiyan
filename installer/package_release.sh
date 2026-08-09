#!/usr/bin/env bash
# Package everything needed for a fresh machine into one distributable zip.
# Run this AFTER build_python.sh and build_openwa.sh have produced their
# artifacts - this script only collects and zips what already exists.
set -euo pipefail

INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGE_DIR="$INSTALLER_DIR/release_stage"
VERSION="${1:-$(date +%Y%m%d)}"
ZIP_NAME="adiyan-${VERSION}.zip"

log() { echo "[package_release] $*" >&2; }
fail() { log "ERROR: $*"; exit 1; }

check_artifacts() {
    [ -x "$INSTALLER_DIR/dist/adiyan" ] || fail "Missing dist/adiyan - run build_python.sh first"
    [ -d "$INSTALLER_DIR/dist_openwa/app" ] || fail "Missing dist_openwa/app - run build_openwa.sh first"
    [ -x "$INSTALLER_DIR/node-runtime/bin/node" ] || fail "Missing node-runtime - run build_openwa.sh first"
}

stage() {
    log "Staging release contents..."
    rm -rf "$STAGE_DIR"
    mkdir -p "$STAGE_DIR/adiyan"

    cp "$INSTALLER_DIR/install.sh" "$STAGE_DIR/adiyan/"
    cp "$INSTALLER_DIR/launch_adiyan.sh" "$STAGE_DIR/adiyan/"
    cp "$INSTALLER_DIR/setup_ollama.sh" "$STAGE_DIR/adiyan/"
    cp "$INSTALLER_DIR/setup_openwa_session.py" "$STAGE_DIR/adiyan/"
    cp "$INSTALLER_DIR/select_model.py" "$STAGE_DIR/adiyan/"
    cp "$INSTALLER_DIR/model_ctx.json" "$STAGE_DIR/adiyan/"
    cp "$INSTALLER_DIR/context.modelfile.template" "$STAGE_DIR/adiyan/"
    cp -r "$INSTALLER_DIR/dist" "$STAGE_DIR/adiyan/dist"
    cp -r "$INSTALLER_DIR/dist_openwa" "$STAGE_DIR/adiyan/dist_openwa"
    cp -r "$INSTALLER_DIR/node-runtime" "$STAGE_DIR/adiyan/node-runtime"

    cat > "$STAGE_DIR/adiyan/README.txt" << 'EOF'
Adiyan - your coach's digital twin
===================================

SETUP (one time, needs internet - downloads an AI model, can take a while):

  1. Unzip this folder if you haven't already.
  2. Open Terminal (search "Terminal" in Spotlight).
  3. Drag this folder into the Terminal window, then type a space, then "install.sh",
     then press Enter. It should look something like:

       bash /Users/you/Downloads/adiyan/install.sh

  4. Wait for it to finish - it downloads an AI model, which can take several
     minutes depending on your internet connection.

EVERY TIME YOU WANT TO USE ADIYAN:

  Do the same thing, but with "launch_adiyan.sh" instead:

       bash /Users/you/Downloads/adiyan/launch_adiyan.sh

  This opens your dashboard in a browser. The first time, you'll need to
  scan a QR code there to link your WhatsApp - same as linking WhatsApp Web.

  To stop Adiyan, close the Terminal window it's running in.

Requires macOS and an internet connection for setup.
EOF

    chmod +x "$STAGE_DIR/adiyan/"*.sh
}

zip_it() {
    log "Creating $ZIP_NAME..."
    (cd "$STAGE_DIR" && zip -rq -X "$INSTALLER_DIR/$ZIP_NAME" adiyan)
    rm -rf "$STAGE_DIR"
    local size
    size="$(du -sh "$INSTALLER_DIR/$ZIP_NAME" | cut -f1)"
    log "Done: $INSTALLER_DIR/$ZIP_NAME ($size)"
}

main() {
    check_artifacts
    stage
    zip_it
}

main "$@"
