# External Dependencies

Standalone products/services the mesh depends on at runtime - not pip
packages (those are pinned in `requirements.txt`), not code Adiyan owns.
Each needs its own install and its own running process, managed outside
`mesh/start_all.sh` (which only starts/stops the Adiyan agents themselves -
see its own header comment).

## Ollama

**What:** Local LLM inference runtime.
**Why Adiyan needs it:** Every agent's own reasoning (classify/extract/
route/humanize stages, Analysis Agent's ReAct loop) runs through
`langchain_ollama.ChatOllama` against models served here - `qwen3:8b-16k`
for reasoning, `nomic-embed-text` for embeddings. mem0 (conversation
memory) and the knowledge-base index (LlamaIndex) both use it too, for both
generation and embedding.
**Default endpoint:** `http://localhost:11434`
**Install:** https://ollama.com - then `ollama pull qwen3:8b-16k` and
`ollama pull nomic-embed-text`.

## Qdrant

**What:** Vector database.
**Why Adiyan needs it:** Backs both the knowledge base (uploaded documents,
`mesh/memory/memory_index.py`) and conversation memory (`mem0ai`, via
`mesh/memory/mem0_backend.py`) - two separate collections in the same
instance.
**Default endpoint:** `http://localhost:6339` (non-standard port -
deliberately not Qdrant's usual 6333, see `mesh/memory/constants.py`).
**Install:** https://qdrant.tech - typically run via Docker
(`docker run -p 6339:6333 qdrant/qdrant`, mapping the non-standard port).

## MongoDB

**What:** NoSQL document database.
**Why Adiyan needs it:** Backs the central config/prompt SDK
(`mesh/lib/config_sdk.py`) - per-agent stage settings and prompt templates,
with local-file fallback if unreachable. Not yet installed on this machine
as of this writing; every agent still runs fine without it (degrades to
local defaults), just without the Mongo-backed override/edit path.
**Default endpoint:** `mongodb://localhost:27017`, database `adiyan_config`
(both overridable via `ADIYAN_MONGO_URL`/`ADIYAN_MONGO_DB`).
**Install:** `brew install mongodb-community` (macOS) or
https://www.mongodb.com/docs/manual/installation/ - then run it yourself,
same standing rule as every other mesh process.

## Arize Phoenix

**What:** OpenTelemetry trace collector + web UI for LLM observability.
**Why Adiyan needs it:** Every agent calls `setup_tracing()`
(`mesh/observability/tracing.py`) at startup, which registers this as the
OTel collector - LangChain and A2A calls get auto-instrumented and show up
here as traces. Run as one standalone collector process (`phoenix serve`),
not a per-agent dependency - `mesh/start_all.sh` does start/stop this one
specifically, unlike the others in this doc, since it's still just a local
process like the agents themselves.
**Default endpoint:** `http://localhost:6006`
**Install:** `pip install arize-phoenix` (already in `requirements.txt`
indirectly via `openinference-instrumentation-langchain`/
`arize-phoenix-otel`, per `tracing.py`'s own header comment).

## OpenWA / penwa (`penwa/`)

**What:** WhatsApp Web browser automation library - the actual messaging
channel Adiyan runs on.
**Why Adiyan needs it:** `mesh/mcp/whatsapp/` wraps this as an MCP server
(send/receive tools) for Orchestrator to use - Adiyan itself has no direct
WhatsApp integration, it's entirely mediated through this.
**Why it's separate:** `penwa/` is a vendored copy of the open-source
OpenWA project (Node.js), not Adiyan-authored code - explicitly called out
in `mesh/start_all.sh`'s own header comment as external infra this script
does not start or stop.
**Install/run:** managed separately from the Python mesh - see `penwa/`'s
own docs.

## ngrok

**What:** Tunneling service, exposing a local port to a public URL.
**Why Adiyan needs it:** Needed for OpenWA/penwa's webhook delivery when
WhatsApp needs to reach this machine from outside the local network.
**Why it's separate:** Same as OpenWA above - explicitly called out in
`mesh/start_all.sh`'s header comment as externally managed infra.
**Install:** https://ngrok.com

## nginx

**What:** Reverse proxy - the single external entry point for every
registered agent (`http://<gateway>/agents/<agent_id>/...`), instead of a
dashboard or support tool needing to know each agent's own port.
**Why Adiyan needs it:** `mesh/nginx/watcher.py` keeps its config in sync
with the Agent Registry automatically, so a newly-registered agent gets a
route without hand-editing nginx config - see that module's own docstring
for the one-time setup (install nginx, add one `include` line to its main
config, start it yourself) and `mesh/nginx/generate_config.py` for what the
generated block actually looks like. Deliberately scoped to
external-facing traffic only - agent-to-agent calls still go direct via the
Agent Registry, untouched by this.
**Default gateway port:** `8081` (`mesh/nginx/generate_config.py`'s
`DEFAULT_GATEWAY_PORT` - Homebrew's own nginx defaults its docroot server
to 8080, confirmed live from its post-install caveats, so the gateway uses
a different port to avoid colliding with it).
**Install:** `brew install nginx` (macOS) or
https://nginx.org/en/docs/install.html - on Homebrew, no manual config
editing needed: its nginx.conf already auto-includes `servers/*`, which is
exactly where `mesh/nginx/watcher.py` writes the generated gateway config.
