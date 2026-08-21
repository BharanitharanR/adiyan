# Running Record

This is not a changelog and not a pitch. It's a working record of the decisions made
building Adiyan, kept specifically to capture what went wrong and why, alongside what
was decided and why — so the next time something looks like a familiar shape of bug,
there's a record to check against before re-learning the same lesson. Wins get one
line if they get a line at all. Mistakes get the full treatment: what was decided,
what actually broke, the real root cause (not just the symptom), how it was fixed, and
what the durable lesson is.

Meant to be appended to, not rewritten. New entries go at the bottom of whichever
section they belong to, or in a new section if they don't fit an existing one.

A note on sourcing: most of what's below is reconstructed directly from comments left
in the code itself — this codebase has an unusual habit of writing genuinely
retrospective docstrings ("confirmed live", "dropped by mistake in this port",
"deliberately parked") right next to the fix, not in a separate doc that drifts out of
date. Those comments are treated here as primary sources, not paraphrased. Where a
detail isn't independently confirmable in the repo (no commit, no comment), it's
described as remembered rather than stated as fact.

---

## 1. The legacy pipeline and why it got replaced

Adiyan started as a single monolithic process: `main.py` wiring together a 7-stage
LangGraph pipeline (`agents/parser_agent.py` → `validator_agent.py` → `router_agent.py`
→ `llm_agent.py` → `synthesizer_agent.py` → `storage_agent.py` → `publisher_agent.py`),
with WhatsApp I/O, MCP tool loading, cron scheduling, and knowledge-base ingestion all
living as `services/*.py` modules imported straight into that one process
(`services/whatsapp_bridge.py`, `services/openwa_poller.py`, `services/kb_ingestion_poller.py`,
`services/cron_scheduler.py`, and so on). It worked, and a fair amount of the reasoning
in it was sound enough to carry forward unchanged (the group-message exclusion, the
werkzeug/httpx log-noise suppression, the "only respond in 1:1 chats" rule).

What it wasn't was decomposed. Every capability — scheduling, journaling, memory,
routing, WhatsApp mechanics — lived inside one process's import graph, with no clean
boundary between "this component talks to WhatsApp's API" and "this component decides
what to do about a message." Adding a new capability meant touching the shared
pipeline. There was no way for one piece to fail or restart independently of the rest,
and no way to reason about "what can this component actually do" without reading the
whole thing.

The rewrite (`mesh/`) restructures this as an A2A/MCP agent mesh: each real capability
(Scheduler, Journal, Memory, Analysis, Orchestrator) is its own A2A agent with its own
`AgentSkill` catalog and its own process; things that are pure mechanism with no
judgment of their own (WhatsApp send/receive, cron ticking, the agent directory) are
MCP servers instead — no AgentCard, no reasoning, just tools. Orchestrator is the one
component that does routing: given an incoming message, it picks which agent's skill
applies (`mesh/orchestrator/router.py`), forwards the raw text, and lets that agent's
own `classify_skill`/`extract_parameters` do the rest — no duplicated NLU anywhere
else in the mesh. See `mesh/AGENTS.md` for the current map of what talks to what.

This is worth naming as a real decision, not a rewrite-for-its-own-sake: the recovery
model is now "restart the process that broke," per component, instead of restarting
one shared pipeline for any failure anywhere in it. The tradeoff that comes with it —
no heartbeat/liveness polling anywhere in the mesh, an Agent Registry that's in-memory
only and rebuilds itself from re-registration — is discussed in Section 5, because it
produced its own startup-ordering bug on first rollout.

As of this writing, the mesh rewrite exists entirely as uncommitted working-tree
changes (`mesh/` is untracked, several legacy files show as renamed-with-modifications
into `mesh/`) — the migration itself hasn't landed as a merged commit yet, only lived
through as working history and in-code comments. Worth remembering when reading git
log for chronology: none of Sections 3–8 below have commit history to point to yet.

---

## 2. The WhatsApp safety incident: `is_owner()` was not enough

**What was decided, originally:** if a message came from the owner's own phone number
(`is_owner(from_number)` true), treat it as an Adiyan command.

**What went wrong:** that's not what "owner" means in practice. The owner's phone
sends two very different kinds of messages: real commands to Adiyan, and completely
ordinary WhatsApp conversation with real people. With no distinction between the two,
an ordinary message to a real contact — "Hi" to Sripriya — got hijacked: Adiyan read it
as a command, failed to make sense of it, and replied into that same conversation with
a confused fallback message. The owner's own phone was, in effect, wired to fire
Adiyan into any conversation it touched. Layered on top of this, before the group
exclusion existed (see Section 4), a message from *any* group member — registered or
not — got a reply posted back into the group, and the owner's own group chatter got
misread as a command and answered into the group too, because `is_owner()` ran before
anything else knew this was a group message at all. Between the two failures, this is
the incident referred to internally as Adiyan "going berserk" — messages landing in
places they had no business landing, triggered by signals (ownership, sender identity)
that were necessary but not sufficient.

**Root cause:** `is_owner()` answers "did this message come from the owner's phone,"
which is a fact about the sender. It says nothing about whether the owner meant this
specific message as a command to Adiyan. Those are different questions, and the code
was answering only the first one while acting as if it had answered both.

**The fix:** an explicit two-part eligibility test, in `mesh/orchestrator/rules_engine.py`'s
`check()`. An owner-authored message only ever triggers Adiyan when **both** of the
following hold:

1. The chat is either the owner's own self-chat or an already-registered client's
   chat — never a conversation with someone who isn't a client.
2. The message explicitly contains an `@Adiyan` mention (`mesh/orchestrator/rules_engine.py`'s
   `ADIYAN_MENTION` check).

Ordinary chatting — including with a registered client — never fires Adiyan just
because the owner sent it. The mention is stripped before the text reaches routing
(`strip_adiyan_mention`) so it doesn't confuse the skill classifier as leftover noise.
This is written directly into `check()`'s docstring as the incident this function
closes, specifically so the reasoning survives independent of whoever reads the code
next.

The stranger case got its own related fix: an unregistered sender, or a group message,
gets **total silence**, not even a polite "you're not registered" reply. The earlier
behavior — replying to every unregistered sender — is what let a single WhatsApp group
flood a "not registered" reply back into itself for every message any member sent,
since there was no group exclusion catching it upstream either. `check()`'s docstring
is explicit that `(None, None)` — reply and tier both unset — is the only combination
that means "send nothing," and warns against restructuring the function without
preserving that distinction.

**Durable lesson:** a signal that correctly identifies *who* is not the same as a
signal that correctly identifies *whether this specific message was meant for you*.
Any "is this the owner / is this an admin / is this privileged" check needs a second,
independent check for intent before it's allowed to act — especially when the first
check alone is broad enough to cover a system's own ordinary background chatter.

---

## 3. `is_self_chat`: the same bug, twice, for different reasons

Self-chat detection (recognizing "the owner messaging themselves," used as a legitimate
command channel alongside the mention-gated self/whitelisted flow above) broke twice,
and each time for a genuinely different reason — worth recording as two separate
entries, not one, because "we already fixed self-chat detection" turned out not to be
true the first time it was said.

**First failure — inconsistent identity field forms.** The naive check compared
`from == to` on the webhook payload. WhatsApp does not report identity consistently:
for a genuine self-chat, `from` sometimes arrives in phone form (`<phone>@c.us`) while
`to`/`chatId`/`author` arrive in privacy form (`@lid`) for the exact same identity —
`chatId` and `author` equal each other, but neither equals the phone-form `from`. The
`from == to` comparison silently evaluated false for a real self-chat every time this
happened. The fix moved off `from`/`to` entirely and onto `chatId == author`
(normalized) instead — `author` is confirmed live to always carry the account's own
identity on a `message.sent` event regardless of destination chat, so `chatId ==
author` is true exactly when the destination chat *is* the account's own identity.

**Second failure — a multi-device suffix survived the first fix.** After the
`chatId == author` fix, a self-chat message that should have triggered still went
silent. The proximate cause: `author` sometimes carries a WhatsApp multi-device suffix
(`6503050272861:14@lid`) that `chatId` never does for the same identity — so the raw
string comparison `chatId == author` silently evaluated unequal again, same failure
*shape* as the first bug (a comparison that looks obviously correct but silently fails
on a real-world field-form inconsistency), different specific cause. The fix added
`_strip_device_suffix()` (`mesh/lib/utilities/whatsapp/openwa_receiver.py`) and ran
*both* sides of the comparison through it, with an explicit note that either side could
carry a suffix in the future, so both must always be normalized, not just the one that
broke this time.

Notably, the code carries a "TEMP DIAGNOSTIC round 2" log block
(`openwa_receiver.py`, around the `is_self_chat` field) left in specifically because
the first fix was verified correct against a captured payload and still didn't hold up
live — the diagnostic exists to re-check the actual fields on the next failure rather
than assume the same root cause without looking again.

**Durable lesson:** don't trust a WhatsApp identity-field comparison to be structurally
sound just because it matches one captured payload. Verify against live traffic, and
when a second failure of the same *symptom* shows up, don't assume it's the same root
cause you already fixed — log the raw fields again and check.

---

## 4. Reliability bugs: dedup, and the group-exclusion regression

**Webhook dedup — the value of rejecting your own first instinct.** OpenWA redelivers
a webhook when it doesn't get a timely response, and Adiyan's LLM calls routinely run
past whatever window triggers that retry. With nothing to recognize a redelivery, each
one ran the full pipeline again as a brand-new message — confirmed live to produce 2-3
independent replies to a single message actually sent once (and, absent Scheduler's own
separate embedding-based dedup, would have created duplicate scheduled jobs too). The
first instinct for fixing this was to hash message content for dedup. That instinct was
caught and rejected before being built: a content hash keyed on chat_id+text would
treat a *genuinely* repeated message — sending "Hi" twice, on two different days — as a
duplicate and silently drop the second, real one. The actual fix keys on WhatsApp's own
message id (`mesh/mcp/whatsapp/dedup.py`), which has no such false-positive risk since
it's unique per message by construction. Marked as seen *before* dispatch, not after, so
two near-simultaneous redeliveries can't both slip past the check. Worth recording
specifically as a case where the obvious-sounding first design was wrong in a way that
would only show up rarely and confusingly (a legitimately repeated message silently
eaten) — good instinct to interrogate before implementing, not just implement and wait
for the bug report.

**Group-message exclusion dropped in the port.** The retired `openwa_poller.py`
unconditionally skipped every `@g.us` chat_id. When the receiver was ported to the
webhook-based `mesh/lib/utilities/whatsapp/openwa_receiver.py`, that exclusion did not
come along — dropped by mistake in the port, not a deliberate scope change. It had to
be re-added explicitly, keyed off OpenWA's own server-derived `kind` classifier
(`kind == 'group'`) rather than pattern-matching the JID by hand, since `kind` is
authoritative and a manual `@g.us` check would just be reinventing something OpenWA
already computes reliably. Confirmed live to matter twice over: without it, every
group member's message (registered or not) got a reply posted back into the group, and
compounded with Section 2's `is_owner()` gap, the owner's own ordinary group chatter
got misread as a command and answered into the group too. This same file also had to
add an explicit `kind == 'individual'` check separately, after a WhatsApp Channel
broadcast post — which OpenWA's `kind` classifier doesn't call a group, but also isn't
a real 1:1 conversation — got treated as an inbound chat message and replied to inside
the channel itself.

**Durable lesson (both of the above):** a behavior that exists in the code you're
porting *from* is not guaranteed to survive the port just because the port is "the same
logic, cleaner." Anything load-bearing for safety (group exclusion, channel exclusion)
needs to be checked for explicitly against the new code, not assumed to have carried
over because the new code "does the same thing."

---

## 5. The Agent Registry: parked for a long stretch, then two real bugs on rollout

**Deliberately parked.** For a long stretch, the mesh ran with agent URLs hardcoded as
plain constants — `mesh/journal/constants.py`'s `MEMORY_AGENT_URL` still carries the
comment "registry idea deliberately parked, so this is a plain constant, not a lookup,"
and `mesh/scheduler/skills/run_routine.py` says the same. This wasn't an oversight;
it was a conscious choice to not build the dynamic registry until enough of the mesh
existed to make it worth the added moving part. When it was finally built
(`mesh/mcp/agent_registry/`), it's deliberately pure bookkeeping — no AgentCard, no
reasoning — and it doesn't trust a registering agent's self-reported skill list; it
fetches that agent's own `/.well-known/agent-card.json` back and treats that as the
source of truth, specifically so a registration can't claim skills it doesn't actually
have.

**Bug 1 — the startup race.** Orchestrator loads its routing pool once, at its own
startup (`mesh/orchestrator/router.py`'s `load_agent_pool()`), not per-message — a
deliberate choice, since a live registry round-trip on every message is exactly the
class of added latency that had already caused the webhook-redelivery bug in Section 4.
The first version of that startup load retried for a short window (5 seconds total)
before giving up. Confirmed live: with the Agent Registry itself already up, Scheduler,
Journal, and Memory still took roughly 9–13 seconds to import their own dependencies
and reach their first registration attempt. Orchestrator's 5-second retry budget lost
that race reliably, and Orchestrator silently began serving with an empty pool — every
message routed to "nothing matches," with no visible crash to point at the cause. The
fix widened the retry budget to 40 attempts × 1.5s (a 60-second ceiling), comfortably
covering a cold start, with the reasoning for the exact numbers left in the code
(`router.py`'s `_LOAD_RETRY_ATTEMPTS`/`_LOAD_RETRY_DELAY_SECONDS` comment) so a future
edit doesn't shrink it back down without knowing why it's sized the way it is.

**Bug 2 — protobuf serialization.** `list_agents` failed on every single call with
`Unable to serialize unknown type: google._upb._message.RepeatedScalarContainer`. A2A's
`AgentSkill` is protobuf-backed, and fields like `tags`, `examples`, `input_modes`, and
`output_modes` are backed by `RepeatedScalarContainer`, not a plain Python list —
FastMCP's tool-result serializer had no way to JSON-encode that type. The fix is a
one-line `list(...)` conversion around each field in `_fetch_skills()`
(`mesh/mcp/agent_registry/server.py`), but the diagnosis mattered more than the fix:
both bugs were root-caused from a full traceback against a live reproduction, not
guessed from the error text or the general shape of the problem.

**Durable lesson:** a "retry a few times at startup" number that was picked without
measuring how long the things it's waiting on actually take is a bug waiting to happen
the moment the mesh grows past whatever was true when the number was chosen. And a
serialization error with an unfamiliar type name in it (`RepeatedScalarContainer`) is
worth taking literally and looking up, not pattern-matching against a more familiar
class of bug.

---

## 6. Knowledge Bank and document ingestion

**Rejected integrations, on confirmed mismatch rather than assumption.** Building real
document ingestion (`ingest_pdf`/`retrieve_knowledge_base`, which existed dormant in
`memory_index.py` before being wired into real skills) involved evaluating and
rejecting three pieces of outside infrastructure: Perseus Vault and Unstructured (both
evaluated and passed over), and — more substantively — Google ADK as the framework for
the whole Knowledge Bank feature. ADK was dropped specifically because there was no
clean way to bridge Adiyan's own permission-token model into ADK's own request-handling
path, and that gap was *confirmed*, not assumed, before the decision was made to drop
it in favor of extending the existing Memory Agent instead. Worth recording as the
correct order of operations: find the actual integration seam and check whether it
closes, rather than deciding "this looks like it won't fit" and moving on without
checking.

**The PPTX/OCR bug — the most dangerous class of bug in this project so far.** Docling's
PPTX backend only extracts native text shapes; it does not run OCR on embedded pictures
the way its image/PDF pipeline does. A 13-slide, all-picture presentation (a common
export shape from slide-design tools) ingested as twelve literal `<!-- image -->`
placeholder lines and nothing else — confirmed live, documented in
`mesh/memory/memory_index.py`'s `_extract_pptx_markdown` docstring. Asked to find
spelling mistakes in it, Adiyan confidently reported finding none. That answer was
wrong, but it wasn't visibly wrong — no crash, no error, no low-confidence hedge. The
system had nothing to actually analyze and didn't know it. This is worth naming as its
own category of risk, distinct from a visible failure: **a confidently wrong answer
produced from empty or near-empty context is worse than an error**, because nothing
about the interaction signals that anything went wrong. The fix
(`_extract_pptx_markdown`) handles PPTX specially: use a slide's real text shapes when
they exist, and when a slide has none (the all-picture case), OCR its picture shapes
individually through Docling's own image pipeline — the same path already proven
against a standalone photo. When OCR genuinely finds nothing readable on a slide (which
happens even for a slide with a legible title — an inherent OCR limitation, not a bug
in this code), that's logged, not silently dropped, so a coach reviewing a suspiciously
thin analysis can find out why.

**No delete-before-insert.** `ingest_pdf` only ever added chunks; it never removed the
old ones for the same document first. Re-ingesting a document — including re-running
the fix above to verify it worked — would leave old, possibly near-empty (the PPTX bug's
own stale chunks) chunks sitting alongside the new ones forever, with colliding
`chunk_index` values confusing `get_document_text()`'s ordering. This one didn't reach
the user: it was caught and fixed proactively while re-ingesting a document specifically
to verify the OCR fix, before ever hitting it live. The fix deletes any existing chunks
for the same `source_key` before inserting fresh ones (`memory_index.py`'s `ingest_pdf`,
the `self._qdrant_client.delete(...)` call right before the chunking loop).

**A limitation rediscovered, not carried forward.** A self-chat document upload can
never be downloaded through OpenWA's media-archive endpoint, because that archive only
ever archives *inbound* media — an outbound (self-chat, `fromMe=true`) message is
explicitly excluded by OpenWA's own design, regardless of archive settings. This was
discovered via a live 404 during the mesh rewrite. The same limitation had already been
documented once before, in the legacy `services/kb_ingestion_poller.py`'s own comments
(`"OpenWA's media archive only archives INBOUND media"`). It had to be rediscovered
from scratch rather than being carried forward automatically when the code was ported —
`mesh/mcp/whatsapp/server.py`'s `_resolve_outbound_media` docstring explicitly cites the
legacy file's comment as "the exact thing already documented" for the old pipeline. The
fix re-fetches the message via `get_messages()` instead, whose response carries the
full media inline regardless of the archive's inbound-only restriction.

**Durable lesson:** a documented limitation lives with the code that hit it, not with
the person who fixed it — porting the code without porting the comment loses the
lesson, and the next person (which can be you, on a later rewrite) re-discovers it the
hard way. If a rewrite is ever done again, comments documenting a real, confirmed
platform limitation should be checked for and carried forward explicitly, not left to
be re-found.

---

## 7. The WhatsApp-reply-honesty correction

A raw internal exception leaked directly into a real WhatsApp reply: *"Did you mean one
of: recall_contact_memory, search_knowledge_base? Please clarify which one."* — an
ambiguous-classify `RuntimeError` straight out of `mesh/lib/a2a_client.py`'s
`_extract_result`, sent to the user verbatim. The correction was explicit and sharp:
never send internal/technical error text to WhatsApp; stay silent on failure instead.
That's now encoded directly in `mesh/orchestrator/skills/handle_message.py`'s `run()`,
in the `except` block around the routing/call/humanize path, with the incident cited
directly in the comment.

What makes this worth its own section rather than folding it into Section 4: it
directly reverses an *earlier*, deliberate design decision in a related part of the
same delivery path. A comment elsewhere in the delivery flow used to argue that a
failure "should still produce SOME message back, not silence" — reasoning that a
user left with total silence has no idea whether their message even arrived. That
principle isn't wrong in general (see the *delivery*-failure branch further down in
`handle_message.py`'s `run()`, which still appends `"(delivery failed: {e})"` rather
than going silent — a delivery failure is visible and recoverable information the user
can act on). It was wrong specifically for *this* class of failure: an internal
classification/routing exception has no actionable content for a WhatsApp user, and the
options are exactly two — leak internals, or say nothing — with no honest middle
ground. Silence turned out to be the right choice for internal failures precisely
*because* the alternative wasn't a clean apology, it was raw exception text.

**Durable lesson:** "always say something, never go silent" is not a universal
principle — it's the right call when there's something genuinely useful to say, and the
wrong call when the only two options are "expose internals" and "say nothing." The
right response depends on what kind of failure it is, not on applying the same rule to
every failure uniformly. (This same reasoning is now also captured outside the code, in
this user's own standing note: WhatsApp should stay silent on internal failure, never
send raw error/apology text.)

---

## 8. Routing brittleness, and what it's driving

**The recurring pattern.** Across many live tests, the free-text routing/classification
step (`mesh/lib/skill_router.py`'s `classify`, deciding which skill/agent a message
means) kept failing not because retrieval or the underlying data was wrong, but because
its curated few-shot examples didn't anticipate the caller's actual phrasing — "what is
my aadhar number," "guess the salary of Bharani," "check my memory and find X" landing
ambiguously between skills. `classify()`'s own docstring documents two confirmed local-
model failure modes on top of this: an empty string instead of JSON null for "no match,"
and outright hallucinated `skill_id` values that were never offered at all (confirmed
live: "How to run a routine" as a returned id). Each phrasing gap has so far been
patched individually, by broadening the relevant skill's examples. That's explicitly
understood as treating the symptom, not the disease: a static, curated-few-shot
classifier structurally cannot anticipate every future way a person might phrase a
request. This is the actual motivation for the Analysis Agent's design — not "we want a
smarter classifier," but "a one-shot classify step doesn't scale to open-ended
phrasing, so the agent needs to investigate instead of guessing once."

**The Analysis Agent's design, as it stands, is itself the current shape of that
motivation — and it was arrived at through visible, multi-round iteration, not decided
once.** What's actually in the repo right now (`mesh/analysis/skills/analyze.py`) is a
fixed map-reduce pipeline: split the fetched document into `WINDOW_CHARS`-sized windows
(sized to the analysis model's real context budget, not the much smaller ~800-char
chunks `memory_index.py` uses for embedding precision), run the instruction against
each window once, then synthesize the partial findings into one final answer. Its own
module docstring is candid that this has a real ceiling: fixed windows and one pass
each can't "leave no stone unturned" the way genuine investigation can — a single-pass
pipeline either checks everything shallowly or exhaustively re-checks everything, with
no way to go deeper only where it matters. The direction under active consideration is
a pivot toward a ReAct-style loop instead: search/read/list tools, with the model
deciding its own next step rather than following a fixed split-analyze-synthesize
shape. Pressure-testing that idea with a direct question about local-model context
limits surfaced a real problem before any code was built on it: a naive version of that
loop would accumulate every raw tool result into one ever-growing conversation and
eventually overflow a 16k-token local model's context window — the same context-budget
constraint `analyze.py`'s `WINDOW_CHARS` comment already names as a hard limit
today. The design under consideration now is a loop backed by a compacting, *structured*
scratchpad (typed fields, not free prose) specifically so context size stays roughly
constant regardless of how many steps or documents get investigated, with scratchpad
size, compaction thresholds, and step ceiling meant to be independently tunable rather
than baked in. As of this writing this is a design in progress, not yet the code in
`mesh/analysis/` — worth being explicit about that distinction rather than writing it up
as already shipped.

**Banyan, evaluated and correctly declined.** Banyan — a deterministic-rules DSL/runtime
this user has elsewhere — was considered for reuse as the Analysis Agent's scratchpad
mechanism, and rejected on the grounds that it solves a genuinely different problem:
deterministic rule evaluation over evidence of known shape, versus what Analysis Agent
actually needs, which is LLM reasoning over unstructured, unpredictable document
content. Recorded here as a second example (alongside the ADK rejection in Section 6)
of declining to force-fit existing infrastructure onto a problem it wasn't built for,
once the mismatch was actually examined rather than assumed either way.

**A decision about how, not just what.** Both the Agent Registry (Section 5) and the
Analysis Agent's current design went through explicit rounds of skeptical, provoking
design questions before any code was written, rather than a single upfront spec
implemented in one pass. That's a decision about *how* Adiyan gets built, not *what*
gets built — and it's earned its place here as a working method that's proven itself
across at least these two cases, worth continuing deliberately rather than by accident.

---

## 9. Durable lessons, going forward

Collected from the sections above, as a standalone checklist to check new work against:

- **Ownership/identity checks answer "who," not "was this meant for you."** A broad
  signal like `is_owner()` needs a second, independent eligibility/intent check before
  it's allowed to act — especially when the broad signal also covers the system's own
  ordinary background activity. (Section 2)
- **WhatsApp's own identity fields are not consistently shaped.** Verify any JID/field
  comparison against real, live payloads — not one captured example — and when the same
  symptom recurs after a fix, re-check the raw fields rather than assuming the same
  root cause. (Section 3)
- **A confidently wrong answer from empty or near-empty context is a worse failure mode
  than a visible error.** No crash, no hedge, just a wrong answer stated plainly. Treat
  "did we actually have anything to work with" as its own thing to verify, separate from
  "did the pipeline run without error." (Section 6)
- **Never let internal/technical error text reach a user-facing channel.** When the only
  two options for a failure are "expose internals" or "say nothing," silence is correct
  — this is not in tension with delivery-failure messages that carry real, actionable
  information; the right response depends on what kind of failure it is. (Section 7)
- **A static, curated-example classifier does not scale to open-ended phrasing.**
  Patching individual misses is a valid short-term fix but is treating the symptom;
  the durable fix is giving the system room to investigate instead of guessing once.
  (Section 8)
- **A port is not guaranteed to carry forward behavior that isn't tested for.** Group
  exclusion and the OpenWA inbound-only media-archive limitation were both already
  correctly handled or documented once, and both had to be rediscovered after a rewrite.
  If a comment documents a confirmed platform limitation, carry the comment forward
  explicitly, not just the code around it. (Sections 4, 6)
- **A first design instinct is worth interrogating before it's built**, especially when
  it sounds obviously right — content-hash dedup would have quietly eaten a legitimate
  repeated message; the reasoning that caught it belongs in the record, not just the
  final choice. (Section 4)
- **A retry/timeout budget that wasn't measured against the real thing it's waiting on
  is a bug waiting for the system to grow past whatever was true when it was picked.**
  (Section 5)
- **Reproduce and read the actual traceback before fixing.** Both Agent Registry bugs,
  and the two `is_self_chat` bugs, were root-caused from a live repro and a real error
  message, not guessed from the general shape of the failure. (Sections 3, 5)
- **Don't force-fit existing infrastructure onto a new problem** — check the actual
  integration seam (a permission model that doesn't bridge, a rules engine built for a
  different shape of evidence) before deciding it fits or doesn't. (Sections 6, 8)
