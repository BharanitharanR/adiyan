#!/usr/bin/env bash
# Starts or stops every mesh/ process: the Phoenix telemetry collector,
# MongoDB, all A2A agents and MCP servers, and the nginx gateway watcher.
#
#   mesh/start_all.sh          (or: mesh/start_all.sh start)
#   mesh/start_all.sh stop
#   mesh/start_all.sh restart adiyan_reader           (one component)
#   mesh/start_all.sh restart adiyan_reader analysis  (more than one)
#   mesh/start_all.sh stop adiyan_reader              (start/stop also take names)
#
# A component name after start/stop/restart scopes the action to just that
# one (or more) - everything else is left untouched. No name means every
# component, the original behavior. restart with no name restarts
# everything. An unrecognized name is a hard error (exit 1, nothing acted
# on) rather than silently doing nothing - a typo'd agent name should never
# look like a successful no-op.
#
# Start is idempotent - a component already running is left alone, not
# restarted. Does NOT start/stop ngrok or nginx itself - those are external
# infra this script assumes are managed separately (see
# docs/EXTERNAL_DEPENDENCIES.md). MongoDB, Qdrant, and OpenWA ARE
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

# Prefer install.sh's own venv over whatever bare python3/phoenix happen to
# resolve to in the caller's shell - confirmed live to matter: on a fresh
# terminal (no manual `source .venv/bin/activate` first), bare python3
# resolved to the system interpreter, which has none of requirements.txt
# installed, so every mesh.*.server component failed on import and came up
# DOWN, while the non-Python components (mongodb, mongo_mcp, openwa) were
# unaffected - the exact failure boundary this venv lookup closes.
if [ -x "$REPO_ROOT/.venv/bin/python3" ]; then
    PYTHON_BIN="$REPO_ROOT/.venv/bin/python3"
else
    PYTHON_BIN="python3"
fi
if [ -x "$REPO_ROOT/.venv/bin/phoenix" ]; then
    PHOENIX_BIN="$REPO_ROOT/.venv/bin/phoenix"
else
    PHOENIX_BIN="phoenix"
fi

# Regenerated every run, not committed as a static file - confirmed live:
# mesh/qdrant/config.yaml had '/Users/bharani/.Adiyan/qdrant_storage'
# checked into git, so qdrant failed with "Permission denied" trying to
# create that exact path on every machine that isn't this one. Rust's own
# config loader doesn't do tilde expansion, so this has to be a real
# absolute path written out with the current user's actual $HOME, not a
# committed value baked in for whoever happened to write the file.
cat > "$REPO_ROOT/mesh/qdrant/config.yaml" <<EOF
storage:
  storage_path: $HOME/.Adiyan/qdrant_storage

service:
  http_port: 6339
  grpc_port: 6340
EOF

ACTION="${1:-start}"
shift 2>/dev/null || true
TARGET_NAMES=("$@")

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
    "openwa|2785|env MEDIA_DOWNLOAD_TIMEOUT_MS=3000000 NODE_EXTRA_CA_CERTS=$HOME/.Adiyan/mcp/whatsapp/tls/cert.pem npm --prefix penwa start"
    "whatsapp_mcp|8425|mesh.mcp.whatsapp.server"
    "orchestrator|8426|mesh.orchestrator.server"
    "nginx_gateway_watcher|-|mesh.nginx.watcher"
)

refresh_listening() {
    LISTENING="$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null || true)"
}

is_target() {
    # No names given ("${TARGET_NAMES[@]}" empty) means "every component" -
    # the original, unscoped behavior. Otherwise only a name explicitly
    # listed matches.
    local name="$1"
    [ "${#TARGET_NAMES[@]}" -eq 0 ] && return 0
    local t
    for t in "${TARGET_NAMES[@]}"; do
        [ "$t" = "$name" ] && return 0
    done
    return 1
}

validate_target_names() {
    # Fails loudly on a typo'd name (exit 1, nothing started/stopped) rather
    # than silently acting on zero components - a name that matches nothing
    # should never look like a successful no-op.
    [ "${#TARGET_NAMES[@]}" -eq 0 ] && return 0
    local t known found
    for t in "${TARGET_NAMES[@]}"; do
        found=0
        for entry in "${COMPONENTS[@]}"; do
            IFS='|' read -r known _ _ <<< "$entry"
            [ "$known" = "$t" ] && found=1 && break
        done
        if [ "$found" -eq 0 ]; then
            echo "Unknown component: $t" >&2
            echo "Known components: $(for e in "${COMPONENTS[@]}"; do IFS='|' read -r n _ _ <<< "$e"; echo -n "$n "; done)" >&2
            exit 1
        fi
    done
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
        is_target "$name" || continue
        if component_alive "$port" "$cmd"; then
            echo "  [skip] $name already running"
            continue
        fi
        if [ "$name" = "phoenix" ]; then
            nohup "$PHOENIX_BIN" serve > "$LOG_DIR/$name.log" 2>&1 &
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
            nohup "$PYTHON_BIN" -m "$cmd" > "$LOG_DIR/$name.log" 2>&1 &
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
            is_target "$name" || continue
            component_alive "$port" "$cmd" || still_down=1
        done
        [ "$still_down" -eq 0 ] && break
    done
    for entry in "${COMPONENTS[@]}"; do
        IFS='|' read -r name port cmd <<< "$entry"
        is_target "$name" || continue
        if component_alive "$port" "$cmd"; then
            echo "  ok   :${port} ($name)"
        else
            echo "  DOWN :${port} ($name)  (check $LOG_DIR/$name.log - may just need more time to boot)"
        fi
    done

    # First-time-only: open the WhatsApp linking page automatically, but
    # only when there's actually a session to link - not on every restart
    # once you're already connected. Silently skipped (not a failure) if
    # config_server isn't up yet or the connection check itself can't run,
    # since this is a convenience, not something worth failing the whole
    # start over.
    if is_target "config_server" && component_alive "8500" "mesh.config_server.server"; then
        already_connected="no"
        if connected_check="$("$PYTHON_BIN" -c "
import asyncio
from mesh.lib.utilities.whatsapp.openwa_service import OpenWAService

async def main():
    svc = OpenWAService(base_url='http://localhost:2785', api_key='', session_name='adiyan')
    print('yes' if await svc.is_connected() else 'no')

asyncio.run(main())
" 2>/dev/null)"; then
            already_connected="$connected_check"
        fi
        if [ "$already_connected" != "yes" ]; then
            echo
            echo "WhatsApp isn't linked yet - opening the registration page..."
            open "http://localhost:8500/register" 2>/dev/null || true
        fi
    fi
}

do_stop() {
    echo "Stopping:"
    for entry in "${COMPONENTS[@]}"; do
        IFS='|' read -r name port cmd <<< "$entry"
        is_target "$name" || continue
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
            is_target "$name" || continue
            component_alive "$port" "$cmd" && still_up=1
        done
        [ "$still_up" -eq 0 ] && break
    done
    refresh_listening
    for entry in "${COMPONENTS[@]}"; do
        IFS='|' read -r name port cmd <<< "$entry"
        is_target "$name" || continue
        if component_alive "$port" "$cmd"; then
            echo "  still up :${port} ($name)  (kill manually if this persists)"
        else
            echo "  down     :${port} ($name)"
        fi
    done
}

validate_target_names

case "$ACTION" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; echo; do_start ;;
    *)
        echo "Usage: $0 [start|stop|restart] [component ...]" >&2
        exit 1
        ;;
esac
