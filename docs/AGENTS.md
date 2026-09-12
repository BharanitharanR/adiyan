# Mesh Agents

Every agent/component running under `mesh/`, what it's actually for, and where to reach it. Kept by hand for humans reading this file - the agents themselves no longer need it kept in sync: which A2A agents exist and what they can do is now discovered at runtime via the Agent Registry (see below), not hardcoded.

| Name | Type | What it solves | Port |
|---|---|---|---|
| **Agent Registry** | MCP server | Pure bookkeeping - an in-memory directory of every A2A agent that has registered itself (agent id, URL, skills, verified by fetching that agent's own agent-card back). No AI, no decisions of its own. Tools: `register_agent`, `list_agents`. | 8424 |
| **Scheduler** | A2A agent | Creates and tracks scheduled WhatsApp jobs (like "remind me every night to journal"), and asks Cron Trigger to wake it up at the right time. Skills: `schedule_job`, `run_routine`, `delete_job`, `list_jobs`. | 8420 |
| **Cron Trigger** | MCP server | Pure clockwork - remembers "call this agent at this exact time" and does it. No AI, no decisions of its own. Tools: `register_trigger`, `remove_trigger`. | 8421 |
| **Journal** | A2A agent | Crafts a tailored reflection question for a user, using what Memory knows about them - or an honest generic one if it knows nothing yet. | 8422 |
| **Memory** | A2A agent | Looks up what's actually known about one specific person from past coaching conversations, and owns the knowledge base (coach-uploaded documents) - search, share the original file back, and the internal resolve/fetch skills Analysis calls. Fetches, doesn't judge - Journal/Analysis decide what to do with what it finds. | 8423 |
| **Analysis** | A2A agent | Reads an *entire* uploaded document (not a similarity-matched snippet) and analyzes, reviews, critiques, or synthesizes something from it per an instruction - "find the spelling mistakes," "summarize this," "review for inconsistencies." Owner-only. Map-reduce over the full text (fetched from Memory Agent), not the small chunks Memory stores for embedding search. | 8427 |
| **WhatsApp** | MCP server | Sends WhatsApp messages (`send_message`/`send_document` tools) and listens for incoming ones (its own internal webhook, pushed into Orchestrator via A2A). Only component that knows WhatsApp's own API. | 8425 |
| **Orchestrator** | A2A agent | The real routing brain. Given an incoming message, picks which agent should handle it, forwards the raw text, turns the structured result back into a readable reply, sends it via the WhatsApp MCP tool. | 8426 |

## How they talk to each other

- Every A2A agent (Scheduler, Journal, Memory, Orchestrator) → Agent Registry: `register_agent` MCP tool call, once at its own startup, from the shared `mesh/lib/bootstrap.py`'s `serve()` - not code any individual agent's own `server.py` has to carry. Agent Registry calls back to the registering agent's own `/.well-known/agent-card.json` to verify the URL actually works and to read its real skill list, rather than trusting what the caller claims.
- Orchestrator → Agent Registry: `list_agents` MCP tool call, once at its own startup (`mesh/orchestrator/router.py`'s `load_agent_pool()`), to build the pool it classifies against. Not re-fetched per message - see `router.py`'s own docstring for why.
- Scheduler → Cron Trigger: MCP tool call (`register_trigger`), to be woken up later.
- Cron Trigger → Scheduler: plain A2A call, at the scheduled time, saying "run this job now."
- Scheduler → Journal → Memory: each a hardcoded A2A client of the next (`mesh/lib/a2a_client.py`), via a structured `Part.data` call - both ends already know exactly what they want.
- WhatsApp (MCP) → Orchestrator: on each incoming message, a precise `DataPart` A2A call (`handle_message`, text + chat_id) - same shape as Cron Trigger's push into Scheduler.
- Orchestrator → (Scheduler | Journal | Memory | Analysis): a coarse "which agent" classify (pooled across the Agent Registry's current directory, `mesh/orchestrator/router.py`), then plain free text forwarded via `TextPart` - the target agent's own classify_skill/extract_parameters does the rest.
- Orchestrator → Memory (`ingest_document`) → Orchestrator → Analysis (`analyze_document`): a document upload whose caption reads as an actual instruction (not just a label) does both in one reply - ingest first via a direct DataPart call (bypassing classify, same reasoning as Cron Trigger's push into Scheduler), then Orchestrator calls Analysis directly with the just-resolved `source_filename`, skipping a separate document-resolution step.
- Analysis → Memory (`resolve_document`, `get_document_text`): when a *later* text-only message names a document by topic instead of uploading it fresh, Analysis resolves which document that means and fetches its full text itself - Orchestrator doesn't do this resolution; it just forwards the free text like any other routed message.
- Orchestrator → WhatsApp (MCP): `send_message` or `send_document` tool call to actually deliver the reply - any target agent's skill result carrying `content_b64` is delivered as a file, not text, regardless of which skill produced it (Memory's `share_knowledge_document`, Analysis's `analyze_document` when the result runs long). Orchestrator knows nothing about WhatsApp's own API - only that something called `whatsapp` exposes these tools.

**Recovery model:** no heartbeat/liveness polling anywhere in this - if an agent crashes, restarting it is how it reappears (both in the registry, via re-registration, and in Orchestrator's pool, via an Orchestrator restart if the affected agent was added/changed after Orchestrator's own last startup). The registry itself is in-memory only, by design - an Agent Registry restart just means every agent re-registers once it's back up.

## Platform-wide memory wiring (2026-09-11/12)

Not agents themselves - shared `mesh/lib/` modules every agent gets by
construction, the same category as `config_sdk.py`/`agent_sdk.py`:

- `graph_client.py` / `graph_store.py`: the structured-fact memory graph
  (Neo4j - see `docs/EXTERNAL_DEPENDENCIES.md`'s Neo4j section).
  `graph_client.py` is bare graph primitives with zero memory-domain
  concepts; `graph_store.py` is the actual `Identity`/`Fact`/`Document`
  schema built on top of it (`about`/`stated_by`/`visible_to`/
  `supersedes`/`sourced_from`).
- `memory_hook.py`: the platform contract every agent calls instead of
  touching the graph directly - `ensure_identity`/`fetch_context`/
  `record_fact`, plus `identity_from_claims()` (derives the caller's
  identity from their A2A token, skipping machine/service callers).
- `identity.py`: `resolve_identity_key()`, moved here from
  `mesh/orchestrator/db.py` once agents needed the same identity-key
  derivation orchestrator already used - a pure function shared by both
  sides rather than reimplemented.
- **The two platform hooks that make this automatic, zero code change per
  agent:** `mesh/lib/bootstrap.py`'s `_MemoryWiredExecutor` (wraps every
  agent's own executor - resolves identity and fetches context *before*
  the agent's own code runs, via a `contextvars.ContextVar`) and
  `mesh/lib/agent_sdk.py`'s `AdiyanAgent.ask()` (reads that same
  ContextVar and prepends it to the prompt automatically - except for
  `schema`/`image_b64` calls, since injecting "what you know about this
  person" ahead of a strict structured-extraction instruction risks
  degrading `skill_router.py`'s classify/extract accuracy for no benefit).
  Read side is fully automatic; writing a fact (`record_fact`) stays a
  deliberate, explicit call per skill - deciding what's worth remembering
  is domain judgment the platform can't infer generically.
- `model_tiers.py`: classifies a model name into `small`/`large` (parsed
  parameter count vs. a dashboard-tunable threshold) and
  `config_sdk.get_tiered_constant()` namespaces a prompt/constant key by
  it - built because the identical `decide_next_step` prompt in
  `mesh/analysis/skills/analyze.py` reliably confused a smaller local
  model while a larger one handled it correctly; both tiers seed from the
  same default, so using this causes zero behavior change until a
  `__small`/`__large` variant is deliberately edited.
- `llm_log.py`: one central, plain-text log
  (`~/.Adiyan/logs/llm_calls.log`) of every prompt any agent sends to the
  LLM and the response (or error) it gets back, wired into all four
  branches of `ask()` - zero per-agent code, same "every agent already
  passes through this one function" reasoning as the memory hooks above.
  **Known real gap:** for a plain-text call that goes through Inference
  Router (`mesh/inference_router/skills/complete.py`), the model actually
  used is resolved *inside* Inference Router from that stage's own
  config - which can differ from, and is never reported back to, the
  `model` value `agent_sdk.py` logs. Confirmed live this can misattribute
  which model actually produced a given answer, especially once
  `communitySearch` peer offload is involved (`mesh/p2p/p2p_app.py`'s
  `discover_and_dispatch()` has its own confirmed bug: a peer answers with
  whatever model it has loaded, ignoring the requested model entirely).
  Not yet fixed - `served_by` and the real resolved model would need to
  flow back through `complete.py`'s return value into the log call.

Test scripts for all of the above (`mesh/lib/test_graph_client.py`,
`test_graph_store.py`, `test_memory_hook.py`, `test_bootstrap_memory_wiring.py`,
`mesh/micro_habits/test_memory_wiring.py`) run in isolation against a real
Neo4j, no agent server or A2A traffic required - see each file's own
docstring. `mesh/tools/seed_memory_graph.py` seeds a demo scenario into the
graph for manual exploration via Neo4j Browser (they wipe the graph on
entry/exit, so nothing survives after running them).

## Retired

`mesh/whatsapp_connector/` (webhook receiver + A2A client, no AgentCard of its own) is gone - replaced by the WhatsApp MCP server + Orchestrator Agent pair above, which splits the same job correctly: WhatsApp-specific concerns stay in the MCP server, routing/reply-decision concerns live in a real agent.

## Going live

The legacy orchestrator (`main.py`) is stopped. OpenWA's own `adiyan` session needs to be `ready` (QR-linked) before anything real can flow. Once it is, `python -m mesh.mcp.whatsapp.register_webhook` registers WhatsApp MCP's `/webhook/whatsapp` (port 8425) as the real webhook target - the mesh-native, non-interactive equivalent of the legacy `setup_openwa_webhook.py`.

**Known gap, not yet closed:** `handle_webhook` doesn't verify any HMAC signature on incoming webhook payloads - `register_webhook.py` deliberately doesn't send a `secret` either, since there's nothing on our side to check it against yet. Anything that can reach `127.0.0.1:8425` can currently forge an incoming-message event. Fine while this only runs on localhost; worth closing before this is reachable from anywhere else.
