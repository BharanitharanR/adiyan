#!/usr/bin/env bash
# Adiyan installer. Run once. Requires an internet connection.
#
# Expects to be run from inside the distributable package, alongside the
# pre-built artifacts this script copies into place:
#   dist/adiyan            - the packaged Python orchestrator (build_python.sh)
#   dist_openwa/app/        - the packaged OpenWA bundle (build_openwa.sh)
#   node-runtime/           - portable Node runtime (build_openwa.sh)
#   model_ctx.json, context.modelfile.template, setup_ollama.sh,
#   setup_openwa_session.py, select_model.py
#
# It does NOT build anything - those are developer-side steps. This script
# only unpacks what's already built and wires up a fresh machine.
set -euo pipefail

INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADIYAN_DIR="$HOME/.Adiyan"
APP_DIR="$ADIYAN_DIR/app"
OPENWA_DIR="$APP_DIR/openwa"

log() { echo "[install] $*" >&2; }
fail() { log "ERROR: $*"; exit 1; }

check_prereqs() {
    log "Checking prerequisites..."

    if ! curl -s -m 5 -o /dev/null https://ollama.com; then
        fail "No internet connection detected. Adiyan needs internet for its one-time setup (downloading Ollama and an AI model)."
    fi

    local ram_gb
    ram_gb=$(($(sysctl -n hw.memsize) / 1024 / 1024 / 1024))
    if [ "$ram_gb" -lt 8 ]; then
        fail "This machine has ${ram_gb}GB RAM. Adiyan needs at least 8GB to run even its smallest model."
    fi
    log "RAM: ${ram_gb}GB - OK"

    local free_gb
    free_gb=$(df -g "$HOME" | tail -1 | awk '{print $4}')
    if [ "$free_gb" -lt 15 ]; then
        fail "Only ${free_gb}GB free disk space. Adiyan needs at least 15GB free (mostly for the AI model)."
    fi
    log "Disk space: ${free_gb}GB free - OK"
}

copy_bundled_app() {
    if [ -x "$APP_DIR/adiyan" ] && [ -x "$OPENWA_DIR/dist/main.js" ] 2>/dev/null; then
        log "App already installed at $APP_DIR"
        return
    fi

    log "Installing Adiyan to $APP_DIR..."
    mkdir -p "$APP_DIR"

    [ -x "$INSTALLER_DIR/dist/adiyan" ] || fail "Missing $INSTALLER_DIR/dist/adiyan - run build_python.sh first"
    [ -d "$INSTALLER_DIR/dist_openwa/app" ] || fail "Missing $INSTALLER_DIR/dist_openwa/app - run build_openwa.sh first"
    [ -x "$INSTALLER_DIR/node-runtime/bin/node" ] || fail "Missing $INSTALLER_DIR/node-runtime - run build_openwa.sh first"

    cp "$INSTALLER_DIR/dist/adiyan" "$APP_DIR/adiyan"
    chmod +x "$APP_DIR/adiyan"

    rm -rf "$OPENWA_DIR"
    cp -r "$INSTALLER_DIR/dist_openwa/app" "$OPENWA_DIR"
    mkdir -p "$OPENWA_DIR/data"

    rm -rf "$APP_DIR/node-runtime"
    cp -r "$INSTALLER_DIR/node-runtime" "$APP_DIR/node-runtime"

    log "App installed"
}

write_openwa_env() {
    local env_file="$OPENWA_DIR/.env"
    if [ -f "$env_file" ]; then
        return
    fi
    log "Writing OpenWA config..."
    cat > "$env_file" << 'EOF'
AUTO_START_SESSIONS=true
PORT=2785
NODE_ENV=production
DATABASE_TYPE=sqlite
DATABASE_NAME=./data/openwa.sqlite
STORAGE_TYPE=local
STORAGE_LOCAL_PATH=./data/media
EOF
}

start_openwa_temporarily() {
    if curl -s -m 2 -o /dev/null http://localhost:2785/api/health; then
        log "OpenWA already running"
        return
    fi
    log "Starting OpenWA for first-time setup..."
    (cd "$OPENWA_DIR" && "$APP_DIR/node-runtime/bin/node" dist/main.js > "$ADIYAN_DIR/openwa.log" 2>&1 &)
}

select_and_pull_model() {
    log "Selecting a model for this machine..."
    local result base final_name
    result="$(python3 "$INSTALLER_DIR/select_model.py")"
    base="$(echo "$result" | awk '{print $1}')"
    final_name="$(echo "$result" | awk '{print $2}')"

    bash "$INSTALLER_DIR/setup_ollama.sh" "$base" "$final_name"
}

bootstrap_openwa_session() {
    python3 "$INSTALLER_DIR/setup_openwa_session.py" "$OPENWA_DIR"
}

main() {
    log "Starting Adiyan installation..."
    check_prereqs
    copy_bundled_app
    write_openwa_env
    start_openwa_temporarily
    select_and_pull_model
    bootstrap_openwa_session

    log ""
    log "Installation complete."
    log "Run: bash \"$INSTALLER_DIR/launch_adiyan.sh\" to start, then scan the QR code to link WhatsApp."
}

main "$@"
