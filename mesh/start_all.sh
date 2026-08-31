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

# Single launch site for a component, shared between the first attempt and
# any down-component retries below, so the dispatch logic (which command
# shape each component needs) can't drift between the two call sites.
# is_retry=1 appends to the existing log instead of truncating it - losing
# the first attempt's crash output right when it's most useful to see why
# a component didn't come up is the wrong tradeoff for a fresh start.
launch_component() {
    local name="$1" cmd="$2" is_retry="${3:-0}"
    local logfile="$LOG_DIR/$name.log"
    if [ "$is_retry" = "1" ]; then
        echo "--- retrying $name ($(date)) ---" >> "$logfile"
    else
        : > "$logfile"
    fi
    if [ "$name" = "phoenix" ]; then
        nohup "$PHOENIX_BIN" serve >> "$logfile" 2>&1 &
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
        nohup npx -y $cmd >> "$logfile" 2>&1 &
    elif [ "$name" = "mongodb" ] || [ "$name" = "qdrant" ] || [ "$name" = "openwa" ]; then
        # A raw binary/npm invocation, not a `python3 -m` module - mongod
        # logs to its own configured path (systemLog.path in
        # mongod.conf) rather than this one; qdrant and openwa do log here.
        nohup $cmd >> "$logfile" 2>&1 &
    else
        nohup "$PYTHON_BIN" -m "$cmd" >> "$logfile" 2>&1 &
    fi
    echo "  [start] $name (pid $!) -> $logfile"
}

# penwa/data/.api-key -> OPENWA_API_KEY in the vault, whenever the file
# exists (a silent no-op otherwise - nothing to sync yet on a genuinely
# first-ever boot, before OpenWA has generated a key at all).
sync_openwa_api_key() {
    [ -f "$REPO_ROOT/penwa/data/.api-key" ] || return 0
    "$PYTHON_BIN" -c "
from mesh.lib.secrets_vault import set_secret
with open('$REPO_ROOT/penwa/data/.api-key') as f:
    set_secret('OPENWA_API_KEY', f.read().strip())
" 2>/dev/null || true
}

do_start() {
    # OpenWA persists its dashboard API key to penwa/data/.api-key on first
    # boot, but doesn't reliably create that directory itself first.
    # Confirmed live on a real fresh clone: "pre-write chmod 0o600 failed
    # for .../penwa/data/.api-key: ENOENT: no such file or directory" on
    # every single restart - meaning OpenWA generated a brand new random
    # key every time, with nothing on disk to persist the old one to.
    # Created here, unconditionally, before OpenWA ever starts - a no-op
    # if it already exists.
    mkdir -p "$REPO_ROOT/penwa/data"

    # Synced here too, before anything launches, not just after everything
    # is up (see the same call further down) - confirmed live this matters:
    # whatsapp_mcp reads OPENWA_API_KEY from the vault at its own process
    # startup, and it launches in the same batch as openwa itself, well
    # before the post-launch sync runs. Once a key file already exists from
    # a prior successful boot (the steady-state case, now that the
    # directory persists across restarts), whatsapp_mcp needs the current
    # value *before* it starts, or it 401s against OpenWA for the rest of
    # this run - no different from the message never arriving at all, and
    # nothing short of a second full restart fixes it, since the vault only
    # gets the right value written to it once, at the very end. The only
    # case this pre-launch call can't cover is a truly first-ever boot,
    # where no key file exists until OpenWA creates one during this same
    # run - the post-launch sync below still exists for exactly that case.
    sync_openwa_api_key

    refresh_listening
    echo "Starting:"
    for entry in "${COMPONENTS[@]}"; do
        IFS='|' read -r name port cmd <<< "$entry"
        is_target "$name" || continue
        if component_alive "$port" "$cmd"; then
            echo "  [skip] $name already running"
            continue
        fi
        launch_component "$name" "$cmd"
    done

    # Up to 3 attempts total, not just 1 - confirmed live this mesh needs
    # it: a component can crash-loop on a transient issue (Ollama still
    # warming up, a port not yet released from a just-killed prior run,
    # Mongo's data directory lock not yet cleared) and come up clean on a
    # bare restart with nothing else changed. Only components still down
    # at the end of an attempt get relaunched - anything already up is
    # left alone, so a slow-but-healthy component never gets restarted
    # out from under itself.
    MAX_START_ATTEMPTS=3
    for attempt in $(seq 1 "$MAX_START_ATTEMPTS"); do
        echo
        if [ "$attempt" -eq 1 ]; then
            echo "Waiting for everything to come up..."
        else
            echo "Waiting for everything to come up... (attempt $attempt/$MAX_START_ATTEMPTS)"
        fi
        # 90 x 2s = 3 minutes, not the original 30s - confirmed live that's
        # not generous enough: Scheduler Agent's own startup does a catch-up
        # pass over any overdue job (see mesh/scheduler/server.py) before it
        # starts listening at all, and each one composes its message via an
        # LLM call - with Ollama's single-request concurrency, a few of
        # those queued up can easily take longer than 30s. A component
        # still down after this either gets retried below or reported DOWN
        # at the very end - this per-attempt wait only avoids a false alarm
        # for something that's genuinely still booting, same "generous
        # timeout" reasoning already applied to the A2A client's own default.
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

        down_names=()
        for entry in "${COMPONENTS[@]}"; do
            IFS='|' read -r name port cmd <<< "$entry"
            is_target "$name" || continue
            component_alive "$port" "$cmd" || down_names+=("$name")
        done
        [ "${#down_names[@]}" -eq 0 ] && break
        [ "$attempt" -eq "$MAX_START_ATTEMPTS" ] && break

        echo "  ${#down_names[@]} component(s) still down after attempt $attempt - retrying: ${down_names[*]}"
        for entry in "${COMPONENTS[@]}"; do
            IFS='|' read -r name port cmd <<< "$entry"
            for down_name in "${down_names[@]}"; do
                [ "$name" = "$down_name" ] && launch_component "$name" "$cmd" 1
            done
        done
    done

    for entry in "${COMPONENTS[@]}"; do
        IFS='|' read -r name port cmd <<< "$entry"
        is_target "$name" || continue
        if component_alive "$port" "$cmd"; then
            echo "  ok   :${port} ($name)"
        else
            echo "  DOWN :${port} ($name)  (check $LOG_DIR/$name.log - tried $MAX_START_ATTEMPTS times, still not up)"
        fi
    done

    # Synced again here, now that OpenWA has actually had a chance to run -
    # covers the genuinely-first-ever-boot case, where no key file existed
    # yet for the pre-launch sync_openwa_api_key call above to pick up.
    # Everything launched *after* this point (the connection check right
    # below, and any future restart's pre-launch sync) sees the correct
    # value; whatsapp_mcp itself, already started earlier in this same run,
    # does not - see sync_openwa_api_key's own comment for why that's a
    # one-restart-late gap this call can't close by itself.
    if is_target "openwa"; then
        sync_openwa_api_key
    fi

    # First-time-only: create the 'adiyan' session (every skill in this
    # mesh that talks to WhatsApp assumes exactly this name - see
    # OPENWA_SESSION_NAME throughout mesh/) and open the WhatsApp linking
    # page automatically, but only when there's actually a session left
    # to link - not on every restart once you're already connected.
    # Silently skipped (not a failure) if config_server/openwa aren't up
    # yet or either check can't run, since this is a convenience, not
    # something worth failing the whole start over.
    if is_target "config_server" && component_alive "8500" "mesh.config_server.server" \
        && is_target "openwa" && component_alive "2785" "npm --prefix penwa start"; then
        "$PYTHON_BIN" -c "
import asyncio
from mesh.lib.utilities.whatsapp.openwa_service import OpenWAService

async def main():
    svc = OpenWAService(base_url='http://localhost:2785', api_key='', session_name='adiyan')
    try:
        session_id = await svc._session_id_or_refresh()
    except Exception:
        session_id = None
    if session_id is None:
        async with svc._new_client() as client:
            await client.post('/api/sessions', json={'name': 'adiyan'})

asyncio.run(main())
" 2>/dev/null || true

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
            # Printed once here, not just left in penwa/data/.api-key for
            # someone to go find by hand - confirmed live this was the
            # actual sticking point on a real first-time setup: the
            # dashboard's own login screen just says "the key is invalid"
            # with no hint where a real one comes from.
            if [ -f "$REPO_ROOT/penwa/data/.api-key" ]; then
                echo
                echo "Dashboard login key (paste this into the screen that just opened):"
                echo "  $(cat "$REPO_ROOT/penwa/data/.api-key")"
                echo
            fi
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
