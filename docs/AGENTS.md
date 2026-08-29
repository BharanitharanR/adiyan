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

## Retired

`mesh/whatsapp_connector/` (webhook receiver + A2A client, no AgentCard of its own) is gone - replaced by the WhatsApp MCP server + Orchestrator Agent pair above, which splits the same job correctly: WhatsApp-specific concerns stay in the MCP server, routing/reply-decision concerns live in a real agent.

## Going live

The legacy orchestrator (`main.py`) is stopped. OpenWA's own `adiyan` session needs to be `ready` (QR-linked) before anything real can flow. Once it is, `python -m mesh.mcp.whatsapp.register_webhook` registers WhatsApp MCP's `/webhook/whatsapp` (port 8425) as the real webhook target - the mesh-native, non-interactive equivalent of the legacy `setup_openwa_webhook.py`.

**Known gap, not yet closed:** `handle_webhook` doesn't verify any HMAC signature on incoming webhook payloads - `register_webhook.py` deliberately doesn't send a `secret` either, since there's nothing on our side to check it against yet. Anything that can reach `127.0.0.1:8425` can currently forge an incoming-message event. Fine while this only runs on localhost; worth closing before this is reachable from anywhere else.
