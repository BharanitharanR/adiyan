# Analysis Agent — Plan Report

A plan, not an implementation. Nothing below is built yet except where explicitly marked "already exists."

## 1. What exists today

**Analysis Agent (`mesh/analysis/`)** has one skill, `analyze_document`, and it does exactly one thing: given a document already sitting in the knowledge base, split it into fixed chunks, analyze each chunk once, combine the results once. It has no other tools. It cannot search the internet, read email, or answer a question that isn't "analyze this specific document." If the document doesn't exist or isn't relevant, there's nothing else it can try.

**The gap this plan is actually responding to**: today, we sent Adiyan a trip-planning question and a menu-recommendation question. Neither matched any existing skill. Adiyan doesn't have a "just think about this and answer" capability anywhere in the system — every skill is a narrow, specific lookup or action. That's the real problem "Analysis Agent as the heart of Adiyan" is meant to solve.

**Two things already exist in this codebase that change the scope of this plan, found while preparing it:**

- **Internet search is already a listed dependency** (`duckduckgo-mcp-server`, in `requirements.txt`) — installed, unused by anything in the current mesh. Free, no API key, no account needed.
- **Gmail/Calendar access already exists**, built once before, in the pre-mesh system: `services/workspace_mcp_service.py` wraps a real, published package (`workspace-mcp`) as a persistent, owner-only, OAuth-gated server. It was never ported into the current mesh architecture, but the hard parts — the OAuth flow, keeping the auth listener alive, owner-only scoping — are already solved. This is **porting existing work, not building from scratch.**

Today's earlier work also matters here: conversation memory (`recall_contact_memory`) and document search (`search_knowledge_base`, `resolve_document`, `get_document_text`) are real, working, callable skills right now. They become tools in this design, not new work.

## 2. What's being proposed

Analysis Agent stops being "a document analyzer" and becomes Adiyan's general-purpose reasoning agent — the ReAct loop we already designed (search → read → decide → act, with a compacting scratchpad so it doesn't blow past what a local model can hold), but with a wider toolset than just documents:

- `search_documents` / `read_document` / `list_documents` — already designed, talks to Memory Agent's existing KB skills.
- `recall_contact_memory` — new addition, ties in today's memory work. Lets the loop pull in what's known about the specific person asking, not just documents.
- `finish(answer)` — ends the loop, same as before.

**Internet search and Gmail are deferred, not part of this build.** You're planning to repurpose the pre-mesh `workspace_mcp_service.py` (and the unused `duckduckgo-mcp-server` dependency) yourself, on your own timeline - this plan no longer includes building either. What this build *does* guarantee: Analysis Agent is wired for MCP tools exactly the way every other agent in this mesh is - `mcp_config.json` lists which MCP servers it's a client of, each tool function calls `mesh/lib/mcp_client.py`'s `call_tool()`, the same helper Scheduler already uses to reach `cron_trigger`. Nothing bespoke. When internet search or Gmail get wired in later, it's: stand up the MCP server, add its name to `mesh/analysis/mcp_config.json`, add one tool function - no change to Analysis Agent's own architecture required.

**What "graceful with or without business logic" concretely means today, with this narrower toolset**: the loop tries what it has first - documents and memory specific to this business/person - and reasons from general knowledge when nothing internal answers the question (no internet lookup yet, just what the model itself knows). A gym trainer who's uploaded a workout-plan document gets an answer grounded in that document; someone asking a generic question with nothing relevant uploaded still gets a real answer, from the model's own knowledge. Once internet search is wired in later, that fallback gets a lot stronger without any change to this loop's structure.

## 3. Decided: Analysis Agent is the fallback

When nothing else classifies, Orchestrator hands the message to Analysis Agent instead of giving up with "Sorry, I'm not sure how to help with that yet." Explicit call: *"Analysis agent is the heart and soul. If it doesn't have an answer its all crickets."* This directly fixes the trip-planning gap demonstrated today. Real cost accepted along with this: every unmatched message now potentially costs several sequential model calls instead of one fast classify-and-fail, and Adiyan starts giving opinions and general advice, not just executing known actions.

## 3a. New, from the platform/marketplace split — Analysis Agent's toolset isn't fixed

Adiyan is two parts: **the platform** (what's being built now — Orchestrator, Memory, Analysis, Scheduler, Journal, the Agent Registry, WhatsApp; general-purpose, no business logic baked in) and **the marketplace** (separate, future — installable vertical-specific agent personas, e.g. a Retail persona, a Gym Trainer persona), each plugging into the same Agent Registry. Analysis Agent, as the heart of the platform, is meant to draw on whichever marketplace agents happen to be installed as part of its own reasoning — not have Orchestrator route to them in isolation.

This means Analysis Agent's tools aren't a hardcoded list. Alongside the fixed tools (documents, memory, internet, Gmail), it gets one more: **discover and call any other agent currently registered** (reusing `mesh/lib/registry_client.py`'s `list_agents()`, the same mechanism Orchestrator's own router already uses). A vertical agent installed later becomes usable by Analysis Agent automatically, with no code change to Analysis Agent itself — this is the actual payoff of building the registry earlier this session, not just routing infrastructure.

## 4. Tool-by-tool risk, for what's actually in this build

- **Documents and memory** — no new risk. Already scoped correctly (owner/tier permissions already exist on the underlying skills).
- **Discovering/calling other registered agents** — bounded by whatever permission scope that agent's own skill already enforces (agent_executor.py's own tier check runs regardless of who's calling) - Analysis Agent gets no special access an owner-tier caller wouldn't already have.
- Internet search and Gmail: deferred (see section 2) - no risk to assess yet, since neither is being built now.

## 5. Explicitly not in this plan

- Internet search and Gmail - deferred to when you repurpose the pre-mesh `workspace_mcp_service.py` and the `duckduckgo-mcp-server` dependency yourself. This build only guarantees Analysis Agent is wired to accept MCP tools the standard way, ready for that later.
- The multi-vertical business-agent idea (a Retail Agent, a Gym Trainer Agent as separate pluggable agents) — a real, related idea from earlier, but separate scope from making Analysis Agent itself more capable.
- The still-open recency/correction-ranking issue from today's Mem0 work — unrelated, still open, not solved by this.
- A real end-to-end WhatsApp test of memory — still pending, independent of this plan.

## 6. Rough shape of the actual build, for scale awareness

- `mesh/analysis/skills/analyze.py` — full rewrite: the fixed pipeline is replaced by the ReAct loop, with a `discover_agents`/`call_agent` tool added alongside the document/memory ones (see 3a).
- `mesh/analysis/mcp_config.json` — stays `{"mcp_servers": []}` for now (no MCP dependencies yet), but the wiring convention (list servers here, call via `mesh/lib/mcp_client.py`) is what future internet-search/Gmail servers will plug into unchanged.
- `mesh/orchestrator/router.py` / `handle_message.py` — when `route_to_agent` returns no match, forward to Analysis Agent instead of the current canned "Sorry, I'm not sure how to help with that yet." reply.
- Memory Agent: no changes needed — every tool it needs to expose already exists.

---

Both open decisions are settled (fallback: yes; internet/Gmail: deferred to you). Ready to build once you confirm.
