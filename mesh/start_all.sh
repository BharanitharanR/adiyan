#!/usr/bin/env bash
# Starts or stops every mesh/ process: the Phoenix telemetry collector,
# MongoDB, all A2A agents and MCP servers, and the nginx gateway watcher.
#
#   mesh/start_all.sh          (or: mesh/start_all.sh start)
#   mesh/start_all.sh stop
#
# Start is idempotent - a component already running is left alone, not
# restarted. Does NOT start/stop OpenWA (penwa/), ngrok, or nginx itself -
# those are external infra this script assumes are managed separately (see
# mesh/EXTERNAL_DEPENDENCIES.md). MongoDB IS started/stopped here despite
# also being third-party infra - explicit exception, per instruction, since
# mesh/lib/config_sdk.py's whole point is degrading gracefully without it,
# so having start_all.sh guarantee it's up removes the most common reason
# that fallback would silently kick in. Started directly via `mongod
# --config`, the same invocation Homebrew's own launchd plist uses
# (confirmed from /opt/homebrew/Cellar/mongodb-community/*/
# homebrew.mxcl.mongodb-community.plist) - not `brew services`, so this
# script's own idempotency/pkill model (identical for every other
# component here) applies to it too, and `stop` shuts it down the same
# clean way (pkill sends SIGTERM, which mongod treats as a graceful
# shutdown request, not a kill -9).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$HOME/.Adiyan/logs"
mkdir -p "$LOG_DIR"
cd "$REPO_ROOT"

ACTION="${1:-start}"

# name -> port -> match pattern (used by both start's port_open check and
# stop's pkill -f, so the two commands can never drift apart on identity).
# port is "-" for a component with no listening socket of its own (the
# nginx watcher is a polling loop, not a server) - see component_alive()/
# do_start()/do_stop() below for how that's handled without needing a fake
# port just to fit this shape.
COMPONENTS=(
    "phoenix|6006|phoenix serve"
    "mongodb|27017|mongod --config /opt/homebrew/etc/mongod.conf"
    "agent_registry|8424|mesh.mcp.agent_registry.server"
    "cron_trigger|8421|mesh.mcp.cron_trigger.server"
    "scheduler|8420|mesh.scheduler.server"
    "memory|8423|mesh.memory.server"
    "journal|8422|mesh.journal.server"
    "analysis|8427|mesh.analysis.server"
    "whatsapp_mcp|8425|mesh.mcp.whatsapp.server"
    "orchestrator|8426|mesh.orchestrator.server"
    "nginx_gateway_watcher|-|mesh.nginx.watcher"
)

refresh_listening() {
    LISTENING="$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null || true)"
}

port_open() {
    # -n/-P avoid lsof's hostname/service-name resolution, which alone took
    # ~20s per call on this machine - snapshot once up front instead of
    # shelling out to lsof per port check.
    grep -q ":$1 " <<< "$LISTENING"
}

component_alive() {
    # port "-" (no listening socket, e.g. the nginx watcher) falls back to
    # a process-match check instead - pgrep against the exact same pattern
    # do_stop()'s pkill already uses, so start's idempotency check and
    # stop's kill can never drift apart on what "this component" means.
    local port="$1" cmd="$2"
    if [ "$port" = "-" ]; then
        pgrep -f "$cmd" > /dev/null 2>&1
    else
        port_open "$port"
    fi
}

do_start() {
    refresh_listening
    echo "Starting:"
    for entry in "${COMPONENTS[@]}"; do
        IFS='|' read -r name port cmd <<< "$entry"
        if component_alive "$port" "$cmd"; then
            echo "  [skip] $name already running"
            continue
        fi
        if [ "$name" = "phoenix" ]; then
            nohup phoenix serve > "$LOG_DIR/$name.log" 2>&1 &
        elif [ "$name" = "mongodb" ]; then
            # mongod logs to its own configured path (systemLog.path in
            # mongod.conf), not this file - $LOG_DIR/mongodb.log just
            # catches anything printed before that takes over.
            nohup $cmd > "$LOG_DIR/$name.log" 2>&1 &
        else
            nohup python3 -m "$cmd" > "$LOG_DIR/$name.log" 2>&1 &
        fi
        echo "  [start] $name (pid $!) -> $LOG_DIR/$name.log"
    done

    echo
    echo "Waiting for everything to come up..."
    for i in $(seq 1 15); do
        sleep 2
        refresh_listening
        still_down=0
        for entry in "${COMPONENTS[@]}"; do
            IFS='|' read -r name port cmd <<< "$entry"
            component_alive "$port" "$cmd" || still_down=1
        done
        [ "$still_down" -eq 0 ] && break
    done
    for entry in "${COMPONENTS[@]}"; do
        IFS='|' read -r name port cmd <<< "$entry"
        if component_alive "$port" "$cmd"; then
            echo "  ok   :${port} ($name)"
        else
            echo "  DOWN :${port} ($name)  (check $LOG_DIR/$name.log - may just need more time to boot)"
        fi
    done
}

do_stop() {
    echo "Stopping:"
    for entry in "${COMPONENTS[@]}"; do
        IFS='|' read -r name port cmd <<< "$entry"
        if pkill -f "$cmd" 2>/dev/null; then
            echo "  [stop] $name"
        else
            echo "  [skip] $name not running"
        fi
    done

    echo
    echo "Waiting for everything to shut down..."
    for i in $(seq 1 10); do
        sleep 1
        refresh_listening
        still_up=0
        for entry in "${COMPONENTS[@]}"; do
            IFS='|' read -r name port cmd <<< "$entry"
            component_alive "$port" "$cmd" && still_up=1
        done
        [ "$still_up" -eq 0 ] && break
    done
    refresh_listening
    for entry in "${COMPONENTS[@]}"; do
        IFS='|' read -r name port cmd <<< "$entry"
        if component_alive "$port" "$cmd"; then
            echo "  still up :${port} ($name)  (kill manually if this persists)"
        else
            echo "  down     :${port} ($name)"
        fi
    done
}

case "$ACTION" in
    start) do_start ;;
    stop)  do_stop ;;
    *)
        echo "Usage: $0 [start|stop]" >&2
        exit 1
        ;;
esac
