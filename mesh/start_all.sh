#!/usr/bin/env bash
# Starts or stops every mesh/ process: the Phoenix telemetry collector,
# MongoDB, all A2A agents and MCP servers, and the nginx gateway watcher.
#
#   mesh/start_all.sh          (or: mesh/start_all.sh start)
#   mesh/start_all.sh stop
#
# Start is idempotent - a component already running is left alone, not
# restarted. Does NOT start/stop ngrok or nginx itself - those are external
# infra this script assumes are managed separately (see
# mesh/EXTERNAL_DEPENDENCIES.md). MongoDB, Qdrant, and OpenWA ARE
# started/stopped here despite also being third-party infra - explicit
# exceptions:
#   - MongoDB: mesh/lib/config_sdk.py's whole point is degrading gracefully
#     without it, so having start_all.sh guarantee it's up removes the most
#     common reason that fallback would silently kick in. Started via
#     `mongod --config`, the same invocation Homebrew's own launchd plist
#     uses.
#   - Qdrant: confirmed live that it doesn't survive a machine restart on
#     its own (no launchd/brew service - no Homebrew formula exists for it
#     at all) - Memory Agent's knowledge base and conversation memory both
#     silently degrade to "unavailable" without it, the same class of
#     surprise MongoDB's exception already exists to prevent. Runs the
#     vendored binary (mesh/qdrant/, fetched via fetch_binary.sh - see its
#     own docstring) against mesh/qdrant/config.yaml, which points at the
#     real on-disk data directory (~/.Adiyan/qdrant_storage), not a fresh
#     empty one.
#   - OpenWA: confirmed live the worst version of this failure - it doesn't
#     survive a restart either, but unlike Mongo/Qdrant nothing degrades
#     gracefully without it: every incoming WhatsApp message just silently
#     never arrives anywhere (no error in any log, since whatsapp_mcp's
#     webhook is never even called). Went unnoticed for ~10 hours before
#     this exception was added. Runs `npm --prefix penwa start` (the NestJS
#     API only, not `npm run dev`'s dashboard+API pair - the mesh only
#     needs the API, port 2785).
#   - Mongo MCP server: same class of issue as Qdrant - an ephemeral `npx`-
#     invoked process, no launchd/brew service, doesn't survive a restart
#     on its own. Without it, Analysis Agent's mcp_servers-driven tool
#     loading (mesh/analysis/skills/analyze.py's _load_mcp_tools()) just
#     logs a warning and silently contributes zero tools - a quiet
#     degradation, not a crash, but one that's easy to not notice.
#     --readOnly (no write/destructive tools ever exposed) and
#     --connectionScope global (confirmed live: the default `session`
#     scope invalidates a connectionId between separate tool calls,
#     breaking the auto-connect interceptor _make_connection_interceptor()
#     relies on) are both load-bearing flags, not defaults to tidy up later.
# All three use this script's own idempotency/pkill model (identical for
# every other component here), not `brew services` - `stop` shuts them
# down the same clean way (pkill sends SIGTERM, a graceful shutdown
# request, not a kill -9). OpenWA is the one exception worth a second look
# if `stop` ever leaves a stray node process behind - npm's own process
# tree (npm -> nest's CLI -> the actual server) doesn't always forward
# signals as cleanly as a single Python process does.
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
    "qdrant|6339|mesh/qdrant/qdrant-bin --config-path mesh/qdrant/config.yaml"
    "mongo_mcp|3000|mongodb-mcp-server --readOnly --transport http --httpPort 3000 --httpHost 127.0.0.1 --connectionScope global"
    "agent_registry|8424|mesh.mcp.agent_registry.server"
    "cron_trigger|8421|mesh.mcp.cron_trigger.server"
    "scheduler|8420|mesh.scheduler.server"
    "memory|8423|mesh.memory.server"
    "journal|8422|mesh.journal.server"
    "adiyan_reader|8429|mesh.adiyan_reader.server"
    "analysis|8427|mesh.analysis.server"
    "config_agent|8428|mesh.config_agent.server"
    "config_server|8500|mesh.config_server.server"
    # NODE_EXTRA_CA_CERTS set via `env` here, not penwa/.env - Node reads that
    # var once at its own process bootstrap (before dotenv-style application
    # code runs), so setting it only in .env is silently too late. Trusts
    # whatsapp_mcp's self-signed webhook-receiver cert (see
    # mesh/mcp/whatsapp/server.py's ensure_self_signed_cert) so OpenWA's
    # outbound webhook fetch doesn't reject it with "TypeError: fetch failed".
    "openwa|2785|env NODE_EXTRA_CA_CERTS=$HOME/.Adiyan/mcp/whatsapp/tls/cert.pem npm --prefix penwa start"
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
        elif [ "$name" = "mongo_mcp" ]; then
            # cmd is deliberately just "mongodb-mcp-server --flags", not
            # "npx -y mongodb-mcp-server@latest --flags" - confirmed live
            # that npx/npm rewrite their own invocation internally (ps shows
            # "npm exec mongodb-mcp-server@latest ..." for the wrapper and
            # "node .../mongodb-mcp-server --flags" for the actual worker
            # holding the port), so a cmd starting with "npx -y ...@latest"
            # never appears verbatim in either real process and do_stop()'s
            # pkill -f would silently fail to match anything. "npx -y" is
            # prepended only here, at the actual launch site, while cmd
            # itself stays exactly what pkill needs to find the real worker.
            nohup npx -y $cmd > "$LOG_DIR/$name.log" 2>&1 &
        elif [ "$name" = "mongodb" ] || [ "$name" = "qdrant" ] || [ "$name" = "openwa" ]; then
            # A raw binary/npm invocation, not a `python3 -m` module - mongod
            # logs to its own configured path (systemLog.path in
            # mongod.conf) rather than this one; qdrant and openwa do log here.
            nohup $cmd > "$LOG_DIR/$name.log" 2>&1 &
        else
            nohup python3 -m "$cmd" > "$LOG_DIR/$name.log" 2>&1 &
        fi
        echo "  [start] $name (pid $!) -> $LOG_DIR/$name.log"
    done

    echo
    echo "Waiting for everything to come up..."
    # 90 x 2s = 3 minutes, not the original 30s - confirmed live that's not
    # generous enough: Scheduler Agent's own startup does a catch-up pass
    # over any overdue job (see mesh/scheduler/server.py) before it starts
    # listening at all, and each one composes its message via an LLM call -
    # with Ollama's single-request concurrency, a few of those queued up
    # can easily take longer than 30s. A component that's still down after
    # this reports DOWN below either way - this only avoids a false alarm
    # for something that's genuinely still booting, same "generous timeout"
    # reasoning already applied to the A2A client's own default.
    for i in $(seq 1 90); do
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
