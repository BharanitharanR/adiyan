#!/usr/bin/env bash
# Starts or stops every mesh/ process: the Phoenix telemetry collector, plus
# all A2A agents and MCP servers.
#
#   mesh/start_all.sh          (or: mesh/start_all.sh start)
#   mesh/start_all.sh stop
#
# Start is idempotent - a component already listening on its port is left
# alone, not restarted. Does NOT start/stop OpenWA (penwa/) or ngrok - those
# are external infra this script assumes are managed separately.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$HOME/.Adiyan/logs"
mkdir -p "$LOG_DIR"
cd "$REPO_ROOT"

ACTION="${1:-start}"

# name -> port -> match pattern (used by both start's port_open check and
# stop's pkill -f, so the two commands can never drift apart on identity).
COMPONENTS=(
    "phoenix|6006|phoenix serve"
    "agent_registry|8424|mesh.mcp.agent_registry.server"
    "cron_trigger|8421|mesh.mcp.cron_trigger.server"
    "scheduler|8420|mesh.scheduler.server"
    "memory|8423|mesh.memory.server"
    "journal|8422|mesh.journal.server"
    "analysis|8427|mesh.analysis.server"
    "whatsapp_mcp|8425|mesh.mcp.whatsapp.server"
    "orchestrator|8426|mesh.orchestrator.server"
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

do_start() {
    refresh_listening
    echo "Starting:"
    for entry in "${COMPONENTS[@]}"; do
        IFS='|' read -r name port cmd <<< "$entry"
        if port_open "$port"; then
            echo "  [skip] $name already listening on $port"
            continue
        fi
        if [ "$name" = "phoenix" ]; then
            nohup phoenix serve > "$LOG_DIR/$name.log" 2>&1 &
        else
            nohup python3 -m "$cmd" > "$LOG_DIR/$name.log" 2>&1 &
        fi
        echo "  [start] $name (pid $!) -> $LOG_DIR/$name.log"
    done

    echo
    echo "Waiting for ports to come up..."
    for i in $(seq 1 15); do
        sleep 2
        refresh_listening
        still_down=0
        for entry in "${COMPONENTS[@]}"; do
            IFS='|' read -r name port cmd <<< "$entry"
            port_open "$port" || still_down=1
        done
        [ "$still_down" -eq 0 ] && break
    done
    for entry in "${COMPONENTS[@]}"; do
        IFS='|' read -r name port cmd <<< "$entry"
        if port_open "$port"; then
            echo "  ok   :$port ($name)"
        else
            echo "  DOWN :$port ($name)  (check $LOG_DIR/$name.log - may just need more time to boot)"
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
    echo "Waiting for ports to release..."
    for i in $(seq 1 10); do
        sleep 1
        refresh_listening
        still_up=0
        for entry in "${COMPONENTS[@]}"; do
            IFS='|' read -r name port cmd <<< "$entry"
            port_open "$port" && still_up=1
        done
        [ "$still_up" -eq 0 ] && break
    done
    refresh_listening
    for entry in "${COMPONENTS[@]}"; do
        IFS='|' read -r name port cmd <<< "$entry"
        if port_open "$port"; then
            echo "  still up :$port ($name)  (kill manually if this persists)"
        else
            echo "  down     :$port ($name)"
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
