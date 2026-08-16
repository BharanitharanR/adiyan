#!/usr/bin/env bash
# Daily launcher: start OpenWA, Ollama, and the Adiyan orchestrator, then open
# the dashboard. Run this each time you want Adiyan running - it is not a
# background service that starts on login. Ctrl+C (or closing this window)
# stops everything it started.
set -euo pipefail

ADIYAN_DIR="$HOME/.Adiyan"
APP_DIR="$ADIYAN_DIR/app"
OPENWA_DIR="$APP_DIR/openwa"
NODE_BIN="$APP_DIR/node-runtime/bin/node"
TOOLS_VENV_BIN="$APP_DIR/tools_venv/bin"
DASHBOARD_URL="http://localhost:5001"

# Which services THIS run started (vs. found already running) - only these
# get stopped on exit. Tracked by port, not by the backgrounded $! PID: the
# packaged orchestrator's bootloader can fork a worker under a different PID
# than the one `&` hands back, so killing $! alone can leave it running.
STARTED_OPENWA=0
STARTED_OLLAMA=0
STARTED_ORCHESTRATOR=0

log() { echo "[launch] $*" >&2; }

kill_port() {
    local port="$1"
    local pids
    pids="$(lsof -ti ":$port" 2>/dev/null || true)"
    if [ -n "$pids" ]; then
        echo "$pids" | xargs kill 2>/dev/null || true
        sleep 1
        # still alive? force it.
        pids="$(lsof -ti ":$port" 2>/dev/null || true)"
        [ -n "$pids" ] && echo "$pids" | xargs kill -9 2>/dev/null || true
    fi
}

cleanup() {
    log "Shutting down..."
    [ "$STARTED_ORCHESTRATOR" = "1" ] && kill_port 5001
    [ "$STARTED_OPENWA" = "1" ] && kill_port 2785
    [ "$STARTED_OLLAMA" = "1" ] && kill_port 11434
}
trap cleanup EXIT INT TERM

is_up() {
    curl -s -m 2 -o /dev/null "$1"
}

wait_for() {
    local url="$1"
    for _ in $(seq 1 30); do
        is_up "$url" && return 0
        sleep 1
    done
    return 1
}

start_openwa() {
    if is_up "http://localhost:2785/api/health"; then
        log "OpenWA already running"
        return
    fi
    log "Starting OpenWA..."
    (cd "$OPENWA_DIR" && "$NODE_BIN" dist/main.js > "$ADIYAN_DIR/openwa.log" 2>&1 &)
    STARTED_OPENWA=1

    wait_for "http://localhost:2785/api/health" || \
        log "WARNING: OpenWA did not come up within 30s - check $ADIYAN_DIR/openwa.log"
}

find_ollama_binary() {
    # Ollama is installed via Homebrew - resolve its bin dir directly rather than assuming
    # PATH is current (a Homebrew installed earlier in the same login session might not be).
    if command -v brew >/dev/null 2>&1 && [ -x "$(brew --prefix)/bin/ollama" ]; then
        echo "$(brew --prefix)/bin/ollama"
    elif [ -x /opt/homebrew/bin/ollama ]; then
        echo "/opt/homebrew/bin/ollama"
    elif [ -x /usr/local/bin/ollama ]; then
        echo "/usr/local/bin/ollama"
    elif command -v ollama >/dev/null 2>&1; then
        command -v ollama
    else
        echo ""
    fi
}

start_ollama() {
    # A `brew services start`-registered Ollama is a persistent launchd agent outside Adiyan's own
    # process tracking - stop it so what's actually serving requests is a process this run started
    # (and can therefore also stop on exit), consistent with "nothing runs unless Adiyan started it".
    if command -v brew >/dev/null 2>&1 && brew services list 2>/dev/null | grep -qE "^ollama +started"; then
        log "Found Ollama running as a Homebrew background service - stopping it..."
        brew services stop ollama >/dev/null 2>&1 || true
        sleep 1
    fi

    if is_up "http://localhost:11434/api/version"; then
        log "Ollama already running"
        return
    fi
    local ollama_bin
    ollama_bin="$(find_ollama_binary)"
    if [ -z "$ollama_bin" ]; then
        log "WARNING: Ollama not found - did installation complete?"
        return
    fi
    log "Starting Ollama..."
    (OLLAMA_HOST="127.0.0.1:11434" nohup "$ollama_bin" serve > "$ADIYAN_DIR/ollama.log" 2>&1 &)
    STARTED_OLLAMA=1

    wait_for "http://localhost:11434/api/version" || \
        log "WARNING: Ollama did not come up within 30s"
}

start_orchestrator() {
    if is_up "$DASHBOARD_URL"; then
        log "Adiyan orchestrator already running"
        return
    fi
    log "Starting Adiyan orchestrator..."
    # Puts search/crawl/Gmail-Calendar tools (installed by install.sh into their own
    # venv - see core/mcp_tools.py's module comment) on PATH so the orchestrator's
    # own shutil.which() lookups find them, same as a normal dev checkout would.
    (PATH="$TOOLS_VENV_BIN:$PATH" "$APP_DIR/adiyan" > "$ADIYAN_DIR/orchestrator_launcher.log" 2>&1 &)
    STARTED_ORCHESTRATOR=1

    wait_for "$DASHBOARD_URL" || \
        log "WARNING: dashboard did not come up within 30s - check $ADIYAN_DIR/orchestrator_launcher.log"
}

open_dashboard() {
    log "Opening dashboard: $DASHBOARD_URL"
    open "$DASHBOARD_URL"
}

main() {
    if [ ! -x "$APP_DIR/adiyan" ]; then
        log "ERROR: Adiyan isn't installed yet. Run the installer first."
        exit 1
    fi

    start_ollama
    start_openwa
    start_orchestrator
    open_dashboard

    log "Adiyan is running. Close this window to stop it."
    # Block until the orchestrator's port goes away (window closed) or we're
    # interrupted - `wait` alone doesn't work here since the services above
    # are intentionally detached (subshelled with `&` inside `()`), not
    # direct children this shell can wait on.
    while is_up "$DASHBOARD_URL"; do
        sleep 2
    done
}

main "$@"
