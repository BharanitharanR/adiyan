#!/usr/bin/env bash
# Adiyan installer. Run once. Requires an internet connection.
#
# Expects to be run from inside the distributable package, alongside the
# pre-built artifacts this script copies into place:
#   dist/adiyan            - the packaged Python orchestrator (build_python.sh)
#   dist_openwa/app/        - the packaged OpenWA bundle (build_openwa.sh)
#   node-runtime/           - portable Node runtime (build_openwa.sh)
#   qdrant-runtime/          - bundled Qdrant binary (services/qdrant_service.py)
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
TOOLS_VENV_DIR="$APP_DIR/tools_venv"

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
    # main.js is a plain JS file run via `node dist/main.js`, not directly executable - it never
    # has the +x bit set, so checking -x on it was always false and this short-circuit never fired.
    # Every run (including a retry after a failed bootstrap) fell through to the reinstall below,
    # which rm -rf'd $OPENWA_DIR - destroying data/ (bootstrap key, session DB, linked WhatsApp
    # auth) on every single re-run. Checked for existence (-f) instead.
    if [ -x "$APP_DIR/adiyan" ] && [ -f "$OPENWA_DIR/dist/main.js" ] 2>/dev/null; then
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

    # Preserve any existing data/ (bootstrap key, session DB, linked WhatsApp auth) across a
    # reinstall - only the app code itself should ever be replaced.
    local data_backup=""
    if [ -d "$OPENWA_DIR/data" ]; then
        data_backup="$(mktemp -d)/data"
        mv "$OPENWA_DIR/data" "$data_backup"
    fi

    rm -rf "$OPENWA_DIR"
    cp -r "$INSTALLER_DIR/dist_openwa/app" "$OPENWA_DIR"

    if [ -n "$data_backup" ]; then
        rm -rf "$OPENWA_DIR/data"
        mv "$data_backup" "$OPENWA_DIR/data"
        rmdir "$(dirname "$data_backup")" 2>/dev/null || true
    else
        mkdir -p "$OPENWA_DIR/data"
    fi

    rm -rf "$APP_DIR/node-runtime"
    cp -r "$INSTALLER_DIR/node-runtime" "$APP_DIR/node-runtime"

    if [ -x "$INSTALLER_DIR/qdrant-runtime/qdrant" ]; then
        rm -rf "$APP_DIR/qdrant-runtime"
        cp -r "$INSTALLER_DIR/qdrant-runtime" "$APP_DIR/qdrant-runtime"
    else
        log "⚠️  Missing qdrant-runtime - Adiyan will run without coaching-history recall"
    fi

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
PUPPETEER_CACHE_DIR=./.chromium-cache
# Needed so the coach can send a PDF to their own self-chat to add it to Adiyan's
# knowledge base (services/kb_ingestion_poller.py downloads it via the media endpoint,
# which 404s without this).
CHAT_MEDIA_ARCHIVE_ENABLED=true
EOF
}

kill_port() {
    local port="$1"
    local pids
    pids="$(lsof -ti ":$port" 2>/dev/null || true)"
    if [ -n "$pids" ]; then
        echo "$pids" | xargs kill 2>/dev/null || true
        sleep 1
        pids="$(lsof -ti ":$port" 2>/dev/null || true)"
        [ -n "$pids" ] && echo "$pids" | xargs kill -9 2>/dev/null || true
    fi
}

start_openwa_temporarily() {
    # Bootstrap never finished (no api key recorded yet) - anything already on this port must be an
    # orphaned process from an earlier failed attempt, left running by a previous `&` background
    # start that this script never tracked or cleaned up. It may be bound to a data/ directory from
    # before a reinstall, so it never comes up correctly (in particular: never writes a bootstrap
    # key) - kill it and start clean rather than trusting it's actually usable.
    if ! grep -q '"openwa_api_key"' "$ADIYAN_DIR/pipeline.json" 2>/dev/null; then
        kill_port 2785
    elif curl -s -m 2 -o /dev/null http://localhost:2785/api/health; then
        log "OpenWA already running"
        return
    fi
    log "Starting OpenWA for first-time setup..."
    (cd "$OPENWA_DIR" && "$APP_DIR/node-runtime/bin/node" dist/main.js > "$ADIYAN_DIR/openwa.log" 2>&1 &)
}

select_and_pull_model() {
    log "Selecting a model for this machine..."
    local result base final_name num_ctx
    result="$(python3 "$INSTALLER_DIR/select_model.py")"
    base="$(echo "$result" | awk '{print $1}')"
    final_name="$(echo "$result" | awk '{print $2}')"
    num_ctx="$(echo "$result" | awk '{print $3}')"

    bash "$INSTALLER_DIR/setup_ollama.sh" "$base" "$final_name" "$num_ctx"
}

bootstrap_openwa_session() {
    python3 "$INSTALLER_DIR/setup_openwa_session.py" "$OPENWA_DIR"
}

install_mcp_tools() {
    # duckduckgo-mcp-server (web search), workspace-mcp (owner's Gmail/Calendar),
    # and crawl4ai (JS-rendered page reading/crawling) are separate command-line
    # tools, not pure-Python libraries - they can't be frozen into the adiyan
    # binary itself, so they get their own small persistent venv here instead,
    # same self-contained-runtime pattern as openwa/node-runtime/qdrant-runtime.
    # core/mcp_tools.py and services/workspace_mcp_service.py both degrade
    # gracefully (feature just unavailable, not a crash) if this step is ever
    # skipped or fails, so a problem here doesn't block the rest of setup.
    if [ -x "$TOOLS_VENV_DIR/bin/duckduckgo-mcp-server" ] && \
       [ -x "$TOOLS_VENV_DIR/bin/workspace-mcp" ] && \
       [ -f "$TOOLS_VENV_DIR/crawl4ai_server.py" ]; then
        log "Search/crawl/Gmail-Calendar tools already installed"
        return
    fi
    log "Installing search, page-reading, and Gmail/Calendar tools (needs internet, a few minutes)..."
    if ! python3 -m venv "$TOOLS_VENV_DIR"; then
        log "WARNING: couldn't create tools venv - search/crawl/Gmail/Calendar tools will be unavailable"
        return
    fi
    "$TOOLS_VENV_DIR/bin/pip" install --quiet --upgrade pip
    if ! "$TOOLS_VENV_DIR/bin/pip" install --quiet duckduckgo-mcp-server workspace-mcp "crawl4ai>=0.9.0"; then
        log "WARNING: failed installing search/crawl/Gmail/Calendar tools - continuing without them"
        return
    fi
    "$TOOLS_VENV_DIR/bin/crawl4ai-setup" || \
        log "WARNING: crawl4ai-setup failed - page reading/crawling may not work"
    cp "$INSTALLER_DIR/mcp_servers/crawl4ai_server.py" "$TOOLS_VENV_DIR/crawl4ai_server.py"
    log "Search/crawl/Gmail-Calendar tools installed"
}

main() {
    log "Starting Adiyan installation..."
    check_prereqs
    copy_bundled_app
    write_openwa_env
    start_openwa_temporarily
    select_and_pull_model
    bootstrap_openwa_session
    install_mcp_tools

    log ""
    log "Installation complete."
    log "Run: bash \"$INSTALLER_DIR/launch_adiyan.sh\" to start, then scan the QR code to link WhatsApp."
}

main "$@"
