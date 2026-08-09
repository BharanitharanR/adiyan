#!/usr/bin/env bash
# Install (if needed) via Homebrew, start, and pull a model into Ollama.
# Idempotent - safe to re-run. Installs Homebrew itself first if it isn't
# already present, so this still works as a one-line, hands-off install.
set -euo pipefail

ADIYAN_DIR="$HOME/.Adiyan"
OLLAMA_PORT=11434
LOG_FILE="$ADIYAN_DIR/ollama.log"
INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL_CTX_FILE="$INSTALLER_DIR/model_ctx.json"
MODELFILE_TEMPLATE="$INSTALLER_DIR/context.modelfile.template"

log() { echo "[setup_ollama] $*" >&2; }

is_server_up() {
    curl -s -m 2 -o /dev/null "http://localhost:${OLLAMA_PORT}/api/version"
}

brew_prefix_bin() {
    # Homebrew's own bin dir isn't guaranteed to be on PATH yet within this same
    # script run (a just-installed Homebrew only updates shell profiles for the
    # *next* shell) - ask brew directly instead of assuming PATH is current.
    if command -v brew >/dev/null 2>&1; then
        echo "$(brew --prefix)/bin"
    elif [ -x /opt/homebrew/bin/brew ]; then
        echo "/opt/homebrew/bin"
    elif [ -x /usr/local/bin/brew ]; then
        echo "/usr/local/bin"
    else
        echo ""
    fi
}

find_ollama_binary() {
    local brew_bin
    brew_bin="$(brew_prefix_bin)"
    if [ -n "$brew_bin" ] && [ -x "$brew_bin/ollama" ]; then
        echo "$brew_bin/ollama"
    elif command -v ollama >/dev/null 2>&1; then
        command -v ollama
    else
        echo ""
    fi
}

ensure_homebrew() {
    if command -v brew >/dev/null 2>&1 || [ -x /opt/homebrew/bin/brew ] || [ -x /usr/local/bin/brew ]; then
        return
    fi
    log "Homebrew not found - installing it first (this may prompt for your password)..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    command -v brew >/dev/null 2>&1 || [ -x /opt/homebrew/bin/brew ] || [ -x /usr/local/bin/brew ] || \
        { log "ERROR: Homebrew install did not complete"; exit 1; }
    log "Homebrew installed"
}

install_ollama() {
    local existing
    existing="$(find_ollama_binary)"
    if [ -n "$existing" ]; then
        log "Ollama already installed at $existing"
        return
    fi

    ensure_homebrew
    local brew_bin
    brew_bin="$(brew_prefix_bin)"
    log "Installing Ollama via Homebrew..."
    "$brew_bin/brew" install ollama
    log "Ollama installed at $("$brew_bin/brew" --prefix ollama)/bin/ollama"
}

start_server() {
    if is_server_up; then
        log "Ollama server already running on port $OLLAMA_PORT"
        return
    fi

    local bin
    bin="$(find_ollama_binary)"
    if [ -z "$bin" ]; then
        log "ERROR: no ollama binary found after install"
        exit 1
    fi

    log "Starting ollama serve (logging to $LOG_FILE)..."
    mkdir -p "$ADIYAN_DIR"
    OLLAMA_HOST="127.0.0.1:${OLLAMA_PORT}" nohup "$bin" serve > "$LOG_FILE" 2>&1 &
    disown

    log "Waiting for server to become ready..."
    for _ in $(seq 1 30); do
        if is_server_up; then
            log "Server is up"
            return
        fi
        sleep 1
    done

    log "ERROR: ollama serve did not become ready within 30s - check $LOG_FILE"
    exit 1
}

pull_model() {
    local model="$1"
    local bin
    bin="$(find_ollama_binary)"

    log "Pulling model: $model (this can take a while on first run)"
    OLLAMA_HOST="127.0.0.1:${OLLAMA_PORT}" "$bin" pull "$model"
    log "Model ready: $model"
}

model_exists() {
    local model="$1"
    local bin
    bin="$(find_ollama_binary)"
    OLLAMA_HOST="127.0.0.1:${OLLAMA_PORT}" "$bin" show "$model" >/dev/null 2>&1
}

build_context_variant() {
    local base="$1"
    local final_name="$2"
    local bin
    bin="$(find_ollama_binary)"

    if model_exists "$final_name"; then
        log "$final_name already exists locally, skipping build"
        return
    fi

    local num_ctx
    num_ctx="$(python3 -c "import json; print(json.load(open('$MODEL_CTX_FILE'))['context']['num_ctx'])")"

    log "Building $final_name from $base (num_ctx=$num_ctx)..."
    local tmp_modelfile
    tmp_modelfile="$(mktemp)"
    sed -e "s/{{BASE}}/$base/" -e "s/{{NUM_CTX}}/$num_ctx/" "$MODELFILE_TEMPLATE" > "$tmp_modelfile"

    OLLAMA_HOST="127.0.0.1:${OLLAMA_PORT}" "$bin" create "$final_name" -f "$tmp_modelfile"
    rm -f "$tmp_modelfile"
    log "Built $final_name"
}

main() {
    local base="${1:-}"
    local final_name="${2:-}"
    if [ -z "$base" ] || [ -z "$final_name" ]; then
        log "ERROR: usage: setup_ollama.sh <base-model> <final-model-name>"
        exit 1
    fi

    install_ollama
    start_server
    pull_model "$base"
    build_context_variant "$base" "$final_name"
    log "Ollama setup complete. Adiyan will use: $final_name"
}

main "$@"
