"""
analyse_this's real body - a ReAct loop (Reason, Act, Observe, repeat),
not a fixed pipeline. Given an instruction, the model decides its own next
move at each step - search documents, read one, check what's known about
the person from past conversations, or ask another registered agent for
help - rather than committing to one predetermined plan up front. Ends
when it calls finish() with an answer, or after MAX_STEPS if it never does
(forced to answer from whatever it's gathered, never erroring out).

Context stays bounded regardless of how many steps or documents get
investigated: the decide-step never sees the raw history of every prior
tool call, only a compact "scratchpad" - a small structured record of
findings, not a growing transcript. After each tool call, a separate
compaction step folds the new observation into an UPDATED scratchpad
(merged, not appended) before the next decision is made, and the raw
observation is discarded. This is what makes a 10-step investigation with
a local 16k-token model actually work - a naive loop that just appends
every tool result to one growing conversation would eventually overflow
it. (Simplification from the original design: every tool result goes
through compaction here, not just the large ones - a real cost/latency
tradeoff for a simpler, easier-to-verify first version.)

Tools span three categories: this agent's own knowledge-base access
(search_documents/read_document/list_documents, via Memory Agent),
conversation memory (recall_memory, via Memory Agent's Mem0-backed
recall_contact_memory), and the wider mesh itself (discover_agents/
consult_agent, via the Agent Registry) - so a business-vertical agent
installed later becomes something this loop can lean on automatically,
with no code change here. See mesh/ANALYSIS_AGENT_PLAN.md for the full
design discussion, including what's deliberately deferred (internet
search, Gmail) and why.
"""
import base64
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field

from mesh.analysis.constants import AGENT_ID, MEMORY_AGENT_URL
from mesh.lib import config_sdk, permissions
from mesh.lib.a2a_client import call_agent, call_agent_with_text
from mesh.lib.config import load_runtime_config
from mesh.lib.registry_client import list_agents

AGENT_CODE_DIR = Path(__file__).parent.parent
logger = logging.getLogger('AnalyzeDocument')

# Configurable via config_sdk (mesh/lib/config_sdk.py), agent_id='analysis',
# constant key 'strict_grounding' - toggles between two real prompt
# behaviors (see _decide_next_step()/_final_answer()), not a cosmetic flag:
#   True  (default): a question with no relevant tool-verified evidence
#         gets an honest "nothing relevant found," even for ordinary
#         general-knowledge questions - never answers from the model's own
#         unverified knowledge.
#   False: the same case falls back to answering from general knowledge
#         (diet advice, packing tips, how something works) - only a
#         specific, verifiable real-world detail (an event, an exact date,
#         a price, current availability) still requires actual tool
#         evidence either way; that guard is never relaxed by this toggle.
# To flip it: config_sdk.set_constant('analysis', 'strict_grounding', False)
DEFAULT_STRICT_GROUNDING = True

MAX_STEPS = 10

# Past this length, the synthesized result is delivered as a file instead
# of a WhatsApp text message - see handle_message.py's content_b64
# delivery convention (any skill result carrying content_b64 is understood
# as "deliver a file," not specific to this skill_id).
FILE_DELIVERY_THRESHOLD_CHARS = 1500

# A single observation folded into the scratchpad is capped before even
# reaching the compaction step - not the final answer's length, just how
# much of one raw tool result gets read at once. This is the fallback/seed
# default only - config_sdk.get_constant(AGENT_ID, 'observation_char_cap', ...)
# is the live value run() actually uses (see run()'s own top). Confirmed
# live this default was too small for anything book-length: read_document()
# on a 167-chunk book got capped down to just its cover page and table of
# contents before the ReAct loop ever reasoned about it - the actual
# passage asked about, deep in the middle, was never seen, and the loop
# answered anyway rather than admitting it hadn't reached that part. Not
# bumped past ~50000 by default even so - the model's own context window
# (qwen3:8b-16k = 16384 tokens, confirmed live) is the real ceiling
# regardless of this value; see analyse_this's own docstring on the
# document-too-big-to-fit limit this doesn't fully solve.
OBSERVATION_CHAR_CAP = 6000

# search_within_document()'s own fallback/seed default - config_sdk.get_constant
# (AGENT_ID, 'doc_search_top_k', ...) is the live value run() actually uses, same
# pattern as observation_char_cap above. Deliberately analysis's own config key,
# not a shared read of memory_index.py's DOC_SEARCH_DEFAULT_TOP_K - what Analysis
# Agent asks for and what Memory Agent defaults to when asked for nothing in
# particular are two separate tunables that happen to start at the same value.
DEFAULT_DOC_SEARCH_TOP_K = 5


class Finding(BaseModel):
    claim: str = Field(description='One specific fact or piece of evidence found so far.')
    source: str = Field(description="Where this came from - a document's filename, 'conversation memory', or an agent's name.")
    quote: str = Field(default='', description='The actual supporting text, if short enough to quote directly.')


class Scratchpad(BaseModel):
    """The loop's entire working memory between steps - deliberately a
    small typed record, not a transcript. See this module's own docstring
    for why bounding this matters."""
    findings: List[Finding] = Field(default_factory=list)
    documents_checked: List[str] = Field(default_factory=list)
    documents_known: List[str] = Field(default_factory=list)
    agents_consulted: List[str] = Field(default_factory=list)
    open_questions: List[str] = Field(default_factory=list)


class _FinalAnswer(BaseModel):
    answer: str


def _cap(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + f'\n\n[...truncated, {len(text) - cap} more characters not shown]'


async def _get_message(key: str, default: str, description: str = '', **kwargs: str) -> str:
    """Mongo-backed user-facing/tool-observation copy - a fallback or
    "nothing found" message, not the ReAct loop's own reasoning prompts
    (those stay hardcoded for now, a separate, not-yet-agreed piece of
    scope). Falls back to `default` (formatted) if the on-file template is
    malformed - confirmed live once already this session
    (orchestrator/humanize.py's own prompt) that a template missing an
    expected placeholder must not break the caller, especially here: these
    strings often feed straight back into the ReAct loop's own reasoning
    as a tool observation, not just a WhatsApp reply."""
    template = await config_sdk.get_constant(AGENT_ID, key, default, description=description or None)
    try:
        return template.format(**kwargs)
    except Exception as e:
        logger.warning(f'Message template {key!r} on file is malformed, using default: {e}')
        return default.format(**kwargs)


def _make_connection_interceptor(connection_string: str, needs_connection_id: set):
    """Auto-manages an MCP session's connectionId, so the ReAct loop's model
    never has to reason about connect/disconnect lifecycle or carry an
    opaque UUID from one step to the next itself.

    Confirmed live this session: this loop's own scratchpad compaction step
    (_compact(), which distills every tool observation into structured
    "findings" via its own LLM call) has nowhere natural to preserve an
    exact opaque session token - a real connectionId returned by connect()
    got paraphrased/lost by the time the model reached its next decide
    step, so it never successfully called find() with a real id even after
    a prompt hint telling it these tools existed. That's a mismatch between
    this loop's whole architecture (compact everything into facts) and a
    stateful multi-call session protocol, not a prompting problem - so this
    sidesteps it entirely instead of trying to prompt around it: the first
    call to any tool in needs_connection_id gets connect() invoked
    automatically, its real connectionId cached, and silently injected -
    the model only ever sees data-operation tools, never connection
    lifecycle at all.

    needs_connection_id (the RAW, un-prefixed MCP tool names, e.g. "find")
    has to be pre-computed from each tool's own args_schema, not guessed
    from request.args - confirmed live that a caller (model or otherwise)
    who never provides connectionId doesn't leave an empty key in
    request.args, it simply omits the key entirely, so "is this key
    present" can't distinguish "doesn't need one" from "needs one but
    didn't have it." Also confirmed live: some of this exact server's own
    tools (list-connections, search-knowledge) have NO connectionId field
    at all - blindly injecting into every call breaks those with an
    unexpected extra property."""
    cached_id: Optional[str] = None

    async def interceptor(request, handler):
        nonlocal cached_id
        if request.name not in needs_connection_id or request.args.get('connectionId'):
            return await handler(request)
        if cached_id is None:
            connect_result = await handler(
                request.override(name='connect', args={'connectionString': connection_string})
            )
            cached_id = connect_result.structuredContent['connectionId']
        request = request.override(args={**request.args, 'connectionId': cached_id})
        return await handler(request)

    return interceptor


async def _load_mcp_tools(server_urls: List[str]) -> List[Any]:
    """Dynamically turns each URL in server_urls into that MCP server's own
    real tool set, merged into this ReAct loop's tools at runtime - the
    consumer half of the mcp_servers config field that's existed since the
    config_sdk migration but was never actually read by any code (confirmed
    live: server.py's own _load_startup_config() fetches it only for the
    agent card, mcp_config.json's own history shows it seeded empty and
    never consumed). Adding a URL to mcp_servers via the config dashboard
    and restarting is now enough to grant this agent a brand new tool, no
    code change here required for the next one.

    Uses langchain_mcp_adapters (already a requirements.txt dependency,
    unused anywhere in this codebase until now) rather than hand-rolling
    MCP-tool-to-LangChain-tool conversion - MultiServerMCPClient.get_tools()
    returns real, directly ainvoke()-able BaseTool objects, the same
    interface every other tool in _make_tools() already has.

    tool_name_prefix=True: each tool's name gets prefixed with its own
    server's slot name (mcp_0, mcp_1, ...) so two different MCP servers
    exposing a same-named tool (e.g. both have a "find") can't collide in
    tools_by_name.

    Best-effort, not all-or-nothing: one unreachable or misbehaving MCP
    server logs a warning and contributes zero tools, rather than taking
    down the whole ReAct loop - same tolerance this mesh already gives
    every other soft dependency (mesh/memory/memory_index.py's
    get_memory_index()).

    connect/disconnect tools are filtered out of what's returned - see
    _make_connection_interceptor()'s own docstring for why the model
    should never need to call them itself. connection_string is currently
    hardcoded for the one real server tested this session (Mongo's
    adiyan_config) - a real limitation if a second, differently-shaped MCP
    server is ever added; worth promoting to a per-server config_sdk value
    at that point, not invented generically now for a server that doesn't
    exist yet."""
    if not server_urls:
        return []
    from langchain_mcp_adapters.client import MultiServerMCPClient

    connections = {
        f'mcp_{i}': {'transport': 'streamable_http', 'url': url}
        for i, url in enumerate(server_urls)
    }
    connection_string = 'mongodb://localhost:27017/adiyan_config'

    try:
        # First pass, no interceptor: just enough to read each tool's own
        # args_schema and learn which raw MCP tool names actually declare a
        # connectionId field - see _make_connection_interceptor()'s own
        # docstring for why this can't be guessed at call time.
        probe_client = MultiServerMCPClient(connections, tool_name_prefix=True)
        probe_tools = await probe_client.get_tools()
        needs_connection_id = {
            t.name.split('_', 2)[-1]
            for t in probe_tools
            if 'connectionId' in (t.args_schema or {}).get('properties', {})
        }

        client = MultiServerMCPClient(
            connections, tool_name_prefix=True,
            tool_interceptors=[_make_connection_interceptor(connection_string, needs_connection_id)],
        )
        tools = await client.get_tools()
    except Exception as e:
        logger.warning(f'Failed to load tools from mcp_servers {server_urls}: {e}')
        return []
    return [t for t in tools if not (t.name.endswith('_connect') or t.name.endswith('_disconnect'))]


def _make_tools(contact_name: Optional[str], observation_char_cap: int, doc_search_top_k: int):
    """Tool functions as closures, not module-level - contact_name varies
    per call, and multiple analyse_this calls can run concurrently with
    different callers; module-level shared state would let them corrupt
    each other. observation_char_cap/doc_search_top_k are likewise per-call
    (fetched once in run() from config_sdk) rather than the module-level
    defaults, so a live config change takes effect on the next call without
    a restart."""

    @tool
    async def search_documents(query: str) -> str:
        """Find the single best-matching document in the knowledge base for
        a topic. Returns its exact filename, ready to pass to read_document -
        or says nothing matched."""
        token = permissions.mint_token('analysis', 'service')
        try:
            result = await call_agent(MEMORY_AGENT_URL, 'resolve_document', {'query': query}, token=token)
        except Exception as e:
            return f'search_documents failed: {e}'
        if not result.get('found'):
            return await _get_message(
                'msg_no_matching_document', 'No matching document found in the knowledge base.',
                description="Shown when search_documents finds no document confident enough to match a query.",
            )
        return f"Best match: {result['source_filename']}"

    @tool
    async def read_document(source_filename: str) -> str:
        """Read a specific document's full text, by its exact filename - get
        this from search_documents or list_documents first, don't guess one.
        Best for a short document, or when you genuinely need the whole
        thing (e.g. summarizing it) rather than searching for something
        specific in it - for a long document, its full text gets truncated
        before you'd ever see anything past its early pages. If you have a
        specific question to answer within a document that might be long,
        use search_within_document instead."""
        token = permissions.mint_token('analysis', 'service')
        try:
            result = await call_agent(MEMORY_AGENT_URL, 'get_document_text', {'source_filename': source_filename}, token=token)
        except Exception as e:
            return f'read_document failed: {e}'
        if not result.get('found'):
            return await _get_message(
                'msg_document_not_found', "No document found with filename '{source_filename}'.",
                description="Shown when read_document is called with a filename that isn't in the knowledge base.",
                source_filename=source_filename,
            )
        return _cap(result['text'], observation_char_cap)

    @tool
    async def search_within_document(source_filename: str, query: str) -> str:
        """Find and return the most relevant passages within one already-
        known document for a specific question - not the whole document.
        Prefer this over read_document whenever you have a specific thing
        to find within a document that might be long (a book, a lengthy
        report) - read_document dumps the ENTIRE document as one block of
        text, and for a long document that gets truncated from the front
        before you ever reach the relevant part. Confirmed live: a
        167-chunk book's read_document result never got past its own cover
        page and table of contents. Get source_filename from search_documents
        or list_documents first, don't guess one. Only use read_document
        instead when you need the document's whole content or a general
        overview rather than something specific in it.

        Returns each matching passage's own real text together with its
        position in the document (chunk_index) and a relevance score, for
        citing exactly where an answer came from - not a summary or
        paraphrase."""
        token = permissions.mint_token('analysis', 'service')
        try:
            result = await call_agent(MEMORY_AGENT_URL, 'search_document_chunks', {
                'source_filename': source_filename, 'query': query, 'top_k': doc_search_top_k,
            }, token=token)
        except Exception as e:
            return f'search_within_document failed: {e}'
        if not result.get('found'):
            return await _get_message(
                'msg_no_relevant_chunks', "No relevant passages found in '{source_filename}' for that question.",
                description="Shown when search_within_document finds nothing relevant enough inside an already-known document.",
                source_filename=source_filename,
            )
        chunks = result['chunks']
        parts = [f"[chunk {c['chunk_index']}, relevance {c['score']:.2f}]\n{c['text']}" for c in chunks]
        return '\n\n---\n\n'.join(parts)

    @tool
    async def list_documents() -> str:
        """List every document currently in the knowledge base, by filename
        only - so you can see what's available without guessing or relying
        on a search query happening to match one. A filename alone tells you
        nothing about a document's actual content or relevance - you still
        need read_document on a specific one before treating anything about
        it as evidence for your answer."""
        token = permissions.mint_token('analysis', 'service')
        try:
            result = await call_agent(MEMORY_AGENT_URL, 'list_documents', {}, token=token)
        except Exception as e:
            return f'list_documents failed: {e}'
        docs = result.get('documents', [])
        if not docs:
            return await _get_message(
                'msg_kb_empty', 'The knowledge base is empty - no documents have been uploaded.',
                description="Shown when list_documents is called but nothing has ever been uploaded to the knowledge base.",
            )
        return '\n'.join(docs)

    @tool
    async def recall_memory(query: str) -> str:
        """Recall what's known about the specific person asking, from past
        conversations - moods, goals, things they've mentioned before. Not
        for document content, that's search_documents' job."""
        if not contact_name:
            return await _get_message(
                'msg_no_contact', 'No specific person is associated with this request - nothing to recall.',
                description="Shown when recall_memory is called but no contact_name was ever attached to this conversation.",
            )
        token = permissions.mint_token('analysis', 'service')
        try:
            result = await call_agent(MEMORY_AGENT_URL, 'recall_contact_memory', {
                'contact_name': contact_name, 'query': query, 'top_k': 5,
            }, token=token)
        except Exception as e:
            return f'recall_memory failed: {e}'
        snippets = result.get('snippets', [])
        if not snippets:
            return await _get_message(
                'msg_nothing_in_memory', 'Nothing relevant found in conversation memory.',
                description="Shown when recall_memory finds a contact but no past conversation snippets relevant to the query.",
            )
        return '\n'.join(f'- {s}' for s in snippets)

    @tool
    async def discover_agents() -> str:
        """List every other agent currently available in the mesh and what
        each one can do - use this to find an agent worth asking for help
        with something outside your own tools, e.g. a business-specific
        agent installed for this deployment."""
        agents = await list_agents()
        no_agents_msg = await _get_message(
            'msg_no_agents_discoverable', 'No other agents are currently discoverable.',
            description="Shown when discover_agents finds no other agents registered in the mesh.",
        )
        if not agents:
            return no_agents_msg
        lines = []
        for entry in agents:
            if entry.get('agent_id') == 'analysis':
                continue
            skill_names = ', '.join(s.get('name', s.get('id', '?')) for s in entry.get('skills', []))
            lines.append(f"{entry['agent_id']}: {skill_names or '(no skills advertised)'}")
        return '\n'.join(lines) if lines else no_agents_msg

    @tool
    async def consult_agent(agent_id: str, request: str) -> str:
        """Consult another registered agent to handle something in free
        text - get agent_id from discover_agents first, don't guess one.
        The target agent interprets the request itself, same as any normal
        message would be routed to it."""
        agents = await list_agents()
        match = next((a for a in agents if a.get('agent_id') == agent_id), None)
        if match is None:
            return await _get_message(
                'msg_agent_not_registered', "No agent registered with id '{agent_id}' - call discover_agents first.",
                description="Shown when consult_agent is called with an agent_id that isn't actually registered in the mesh.",
                agent_id=agent_id,
            )
        token = permissions.mint_token('analysis', 'service')
        try:
            result = await call_agent_with_text(match['url'], request, token=token)
        except Exception as e:
            return f'consult_agent failed: {e}'
        return _cap(str(result), observation_char_cap)

    @tool
    def finish(answer: str) -> str:
        """Call this once you have enough evidence to give a final answer -
        pass the complete answer as the argument. This ends the
        investigation. If you genuinely found nothing relevant after
        checking the reasonable places, call this and say so plainly rather
        than inventing an answer. The same applies to any specific detail
        within an otherwise-good answer, not just "nothing found" as a
        whole: a real-world specific (an event name, an exact date, a
        price, current availability, anything that could be true or false
        right now) that you didn't actually find via a tool must not be
        stated as fact - say you're not certain, or leave it out, rather
        than filling the gap with something plausible-sounding.

        Before calling this, check that your answer actually addresses the
        original instruction - not just that the scratchpad has findings in
        it. A tool can return real, true content that still has nothing to
        do with what was asked (e.g. a document search returning the
        closest available match, which may be about something else
        entirely). Summarizing an irrelevant finding is not an answer -
        if nothing you found actually bears on the instruction, say that
        plainly instead of reporting on what you found anyway."""
        return answer  # never actually executed - the loop intercepts this call

    tools = [
        search_documents, read_document, search_within_document, list_documents,
        recall_memory, discover_agents, consult_agent, finish,
    ]
    return tools, {t.name: t for t in tools}


async def _decide_next_step(instruction: str, scratchpad: Scratchpad, tools, cfg: Dict[str, Any], strict: bool):
    model = ChatOllama(
        model=cfg['model'], base_url=cfg.get('base_url', 'http://localhost:11434'), temperature=cfg['temperature'],
    ).bind_tools(tools)
    grounding_rule = (
        "If you genuinely find nothing relevant after checking the reasonable "
        "places - including an ordinary general-knowledge question (diet/"
        "nutrition advice, packing tips, how something works) - say so plainly "
        "rather than answering from your own general knowledge; strict grounding "
        "is on, so only tool-verified evidence counts as a basis for an answer."
        if strict else
        "This does NOT mean refusing to answer a general-knowledge question "
        "(diet/nutrition advice, packing tips, how something works) just "
        "because no document/memory/agent had anything relevant - general "
        "knowledge you already have is a legitimate basis for an answer on "
        "its own. 'No relevant document was found' is a reason to rely on "
        'general knowledge instead, not a reason to give up and tell the '
        'person to go ask someone else - only do that if the question '
        'genuinely needs a specific, verifiable, real-world fact you have '
        'no way to confirm (see below), not for an ordinary advice question.'
    )
    prompt = (
        f'Instruction: {instruction}\n\n'
        f'What you have found so far:\n{scratchpad.model_dump_json(indent=2)}\n\n'
        'Decide your next action. Use a tool to keep investigating, or call '
        'finish with your complete answer once you have enough evidence. '
        'If the instruction refers to something ambiguously - "the game", '
        '"it", "that", "like I mentioned" - and the scratchpad has not '
        'already checked conversation memory, call recall_memory first to '
        'see if what the person is referring to is already known from '
        'earlier conversation, before asking them to clarify or giving up. '
        'Confirmed live: a person who said earlier they enjoy tennis, then '
        'later asked to "explain the rules of the game", got asked "which '
        'game?" instead of getting tennis rules - recall_memory would have '
        'resolved that on its own, it was just never called. '
        'search_documents only ever returns a filename, never document '
        'content - if the scratchpad shows a document under documents_known '
        'that has not yet been read, investigate it (read_document or '
        'search_within_document) before treating anything about that '
        'document as a finding or citing it in your answer. '
        'If a document is already known to be relevant (in documents_known) '
        'and you have a specific thing to find within it, call '
        'search_within_document with that specific question as the query - '
        'not read_document, which dumps the whole document and, for a long '
        'one, gets truncated before reaching the relevant part. Only use '
        "read_document when you need the document's full content or a "
        'general overview rather than something specific in it. '
        'Never state a specific real-world detail (an event, an exact date, '
        'a price, current availability) as fact unless a tool actually gave '
        'it to you - if you are unsure, say so or leave it out, do not assume. '
        + grounding_rule
    )
    mcp_tool_names = [t.name for t in tools if t.name.startswith('mcp_')]
    if mcp_tool_names:
        # Confirmed live this session: a question about this deployment's
        # OWN internal state ("what voice is AdiyanReader set to") got no
        # relevant answer from search_documents (correctly - that's not a
        # user document) and the loop gave up, never trying the newly-loaded
        # mcp_servers tools at all. Their own tool descriptions (generic
        # "run a find query against a MongoDB collection") give no hint they
        # hold THIS deployment's own configuration data specifically - this
        # is the missing context, not a prompt rewrite of every possible
        # question shape.
        prompt += (
            '\n\nYou also have database tools (' + ', '.join(mcp_tool_names) + ') connected to this '
            "deployment's own internal database - use these for a question about the system's own "
            "configuration, settings, or internal state (e.g. which voice/model an agent is set to use, "
            "what a stage's parameters are), not for questions about a user's uploaded documents."
        )
    return await model.ainvoke(prompt)


async def _compact(instruction: str, scratchpad: Scratchpad, tool_name: str, observation: str, cfg: Dict[str, Any]) -> Scratchpad:
    """Folds one new observation into an updated scratchpad - merged, not
    appended. This is the step that keeps the decide-step's own input
    bounded: it never sees raw tool output, only this compact result."""
    model = ChatOllama(
        model=cfg['model'], base_url=cfg.get('base_url', 'http://localhost:11434'), temperature=0.2,
    ).with_structured_output(Scratchpad)
    prompt = (
        f'Instruction: {instruction}\n\n'
        f'Current scratchpad:\n{scratchpad.model_dump_json(indent=2)}\n\n'
        f'New observation, from calling {tool_name}:\n{observation}\n\n'
        'Produce the UPDATED scratchpad. Merge genuinely new, relevant findings from '
        'the observation into the findings list - do not just append the raw text, '
        'extract what actually matters for the instruction. Relevant means it helps '
        'answer THIS instruction specifically, not merely that a tool returned it - a '
        "document being the closest available match doesn't make its content "
        'relevant if the document itself is actually about something unrelated (e.g. '
        'a habit-formation guide, when asked about trip planning). If an observation '
        "doesn't genuinely bear on the instruction, don't add it as a finding - note "
        'in open_questions that this avenue turned up nothing relevant instead. Keep '
        'findings focused: at most 8 entries, dropping the least relevant if you '
        'would exceed that. Track documents_checked/agents_consulted if this '
        'observation came from reading one. Carry forward everything from the '
        'current scratchpad that is still relevant - do not silently drop prior '
        'findings unless this observation supersedes them.'
    )
    try:
        return await model.ainvoke(prompt)
    except Exception as e:
        logger.warning(f'Compaction failed, keeping prior scratchpad unchanged: {e}')
        return scratchpad


async def _final_answer(instruction: str, scratchpad: Scratchpad, cfg: Dict[str, Any], strict: bool) -> str:
    """Reached the step cap without finish() being called - forces a real
    answer from whatever the scratchpad holds, rather than erroring out."""
    model = ChatOllama(
        model=cfg['model'], base_url=cfg.get('base_url', 'http://localhost:11434'), temperature=0.3,
    ).with_structured_output(_FinalAnswer)
    grounding_rule = (
        "If the scratchpad has nothing relevant - including for an ordinary "
        "general-knowledge question (diet/nutrition advice, packing tips, how "
        "something works) - say so plainly rather than answering from your own "
        "general knowledge; strict grounding is on, so only what is actually in "
        "the scratchpad above counts as a basis for an answer."
        if strict else
        'But for an ordinary general-knowledge question (diet/nutrition '
        'advice, packing tips, how something works), answer it using what you '
        'already know even if the scratchpad has nothing relevant - a scratchpad '
        'empty of relevant findings means rely on general knowledge instead, not '
        'give up and tell the person to ask someone else.'
    )
    prompt = (
        f'Instruction: {instruction}\n\n'
        f'Everything found during the investigation:\n{scratchpad.model_dump_json(indent=2)}\n\n'
        'You are out of further investigation steps. Answer the instruction now. '
        'For any specific, verifiable real-world detail (an event, an exact date, '
        'a price, current availability), use only what is actually in the '
        'scratchpad above - if it is not there, say you are unsure or leave it '
        'out, do not assume. That includes findings that are real but irrelevant '
        "to what was actually asked (e.g. a document that turned out to be about "
        "something unrelated) - reporting on an irrelevant finding is not an "
        'answer. ' + grounding_rule
    )
    result = await model.ainvoke(prompt)
    return result.answer


def _merge_document_list(scratchpad: Scratchpad, observation: str) -> Scratchpad:
    """list_documents() observations never go through _compact()'s LLM call
    - confirmed live that letting the model 'extract findings' from a bare
    filename listing produces exactly what it sounds like it would: a
    fabricated summary of each file's contents, never actually read, none
    of it relevant to the instruction, presented as real evidence anyway
    (a Vizag-trip question got answered with an unrelated payment-processing
    deck and someone else's Aadhar photo, described as if both had been
    opened and were about the trip). A tool that only ever returns filenames
    has nothing in its output an LLM could correctly call a 'finding' -
    enforcing that in code, not hoping a prompt instruction holds, is the
    actual fix; see analyse_this's own docstring on why prompt-only guards
    against this already proved insufficient twice."""
    for line in observation.splitlines():
        name = line.strip()
        if name and name not in scratchpad.documents_known:
            scratchpad.documents_known.append(name)
    return scratchpad


def _merge_search_result(scratchpad: Scratchpad, observation: str) -> Scratchpad:
    """search_documents() observations that matched something never go
    through _compact()'s LLM call either - confirmed live (via a real
    WhatsApp message and its Phoenix trace) that the same failure as
    _merge_document_list() documents happens here too: the tool's own
    docstring says it returns nothing but a filename ("Best match:
    <filename>"), yet handing that bare string to _compact() as an
    "observation" produced a confident, specific "finding" - a fabricated
    quote and fabricated facts (a wrong port number, a wrong justification)
    attributed to that file - with read_document never actually called to
    verify any of it, because finish() looked satisfied and the loop never
    took another step. Same fix as _merge_document_list(): code-enforced,
    not LLM-judged - a "Best match: <filename>" string has nothing in it an
    LLM could correctly call a finding, so extract the filename in plain
    Python and nudge the loop to read it, exactly like the
    source_filename-seeding at the top of run() already does. The "nothing
    matched" case is a safe, already-canned message with no fabrication
    risk - see run()'s dispatch, which only routes here when the "Best
    match:" prefix is present, and lets the canned message go through the
    normal _compact() path unchanged."""
    filename = observation[len('Best match: '):].strip()
    if filename and filename not in scratchpad.documents_known:
        scratchpad.documents_known.append(filename)
    nudge = (
        f'The document {filename!r} is already known to be relevant - investigate it '
        '(read_document or search_within_document) before treating anything about it as a finding.'
    )
    if filename and filename not in scratchpad.documents_checked and nudge not in scratchpad.open_questions:
        scratchpad.open_questions.append(nudge)
    return scratchpad


def _package_result(text: str, source_filename: Optional[str]) -> Dict[str, Any]:
    if len(text) <= FILE_DELIVERY_THRESHOLD_CHARS:
        return {'found': True, 'result': text}
    base_name = (source_filename or 'analysis').rsplit('/', 1)[-1].rsplit('.', 1)[0]
    return {
        'found': True,
        'result': text[:200] + '...' if len(text) > 200 else text,
        'filename': f'{base_name}_analysis.md',
        'mimetype': 'text/markdown',
        'content_b64': base64.b64encode(text.encode('utf-8')).decode('ascii'),
    }


async def run(instruction: str, source_filename: Optional[str] = None, contact_name: Optional[str] = None) -> Dict[str, Any]:
    cfg = await config_sdk.get_stage_config(AGENT_ID, 'react', load_runtime_config(AGENT_CODE_DIR)['react'])
    strict = await config_sdk.get_constant(
        AGENT_ID, 'strict_grounding', DEFAULT_STRICT_GROUNDING,
        description="If true, only tool-verified evidence counts as a basis for an answer - even an ordinary "
                    "general-knowledge question gets an honest \"nothing relevant found\" instead of an answer "
                    "from the model's own training data. If false, general knowledge is a legitimate fallback "
                    "when no document/memory/agent had anything relevant, but a specific verifiable detail "
                    "(a date, a price, a port number) still requires real tool-verified evidence.",
    )
    observation_char_cap = await config_sdk.get_constant(
        AGENT_ID, 'observation_char_cap', OBSERVATION_CHAR_CAP,
        description="Max characters kept from any single tool result before it's truncated - protects the "
                    "ReAct loop's own prompt from being blown out by one huge document dump.",
    )
    doc_search_top_k = await config_sdk.get_constant(
        AGENT_ID, 'doc_search_top_k', DEFAULT_DOC_SEARCH_TOP_K,
        description="How many passages search_within_document returns per query - more raises the odds the "
                    "actually-relevant one is included, at the cost of a longer observation to read.",
    )
    tools, tools_by_name = _make_tools(contact_name, observation_char_cap, doc_search_top_k)

    mcp_servers = await config_sdk.get_constant(
        AGENT_ID, 'mcp_servers', [],
        description='URLs of MCP servers whose tools get dynamically added to this ReAct loop at startup '
                    '(see _load_mcp_tools() in this skill\'s own module) - add a URL here and restart to grant '
                    'this agent a new tool with no code change. Empty by default.',
    )
    mcp_tools = await _load_mcp_tools(mcp_servers)
    if mcp_tools:
        tools = tools + mcp_tools
        tools_by_name = {**tools_by_name, **{t.name: t for t in mcp_tools}}

    scratchpad = Scratchpad()
    if source_filename:
        # Already known which document this is about (the upload+instruct
        # combined flow) - seed the scratchpad so the loop reads it first
        # instead of re-discovering something it was already handed, while
        # still being free to check elsewhere if that turns out not to be enough.
        scratchpad.documents_known.append(source_filename)
        scratchpad.open_questions.append(
            f'The document {source_filename!r} is already known to be relevant - investigate it '
            '(read_document or search_within_document) before treating anything about it as a finding.'
        )

    for _ in range(MAX_STEPS):
        response = await _decide_next_step(instruction, scratchpad, tools, cfg, strict)

        if not response.tool_calls:
            # Answered directly without calling finish - treat its own text
            # as the answer, a graceful outcome, not an error.
            fallback = await _get_message(
                'msg_no_clear_answer', "I wasn't able to find a clear answer.",
                description="Fallback used if the ReAct loop's own final response has no text at all to fall back on.",
            )
            return _package_result(response.content or fallback, source_filename)

        call = response.tool_calls[0]
        if call['name'] == 'finish':
            return _package_result(call['args'].get('answer', ''), source_filename)

        tool_obj = tools_by_name.get(call['name'])
        if tool_obj is None:
            observation = await _get_message(
                'msg_unknown_tool', 'Unknown tool: {tool_name}',
                description="Shown when the model calls a tool name that doesn't exist - should never happen in practice.",
                tool_name=call['name'],
            )
        else:
            try:
                observation = await tool_obj.ainvoke(call['args'])
            except Exception as e:
                observation = f"Tool call failed: {e}"

        if call['name'] == 'list_documents':
            # Code-enforced, not LLM-judged - see _merge_document_list()'s
            # own docstring for why this tool's output specifically must
            # never reach the 'extract findings' compaction step.
            scratchpad = _merge_document_list(scratchpad, str(observation))
        elif call['name'] == 'search_documents' and str(observation).startswith('Best match: '):
            # Same reasoning, same fix - see _merge_search_result()'s own
            # docstring. Only the "matched" case is bypassed; the "nothing
            # matched" case is a canned message and falls through below.
            scratchpad = _merge_search_result(scratchpad, str(observation))
        else:
            scratchpad = await _compact(instruction, scratchpad, call['name'], _cap(str(observation), observation_char_cap), cfg)

    # Hit MAX_STEPS without finish() - never error out, answer from
    # whatever was gathered.
    final = await _final_answer(instruction, scratchpad, cfg, strict)
    return _package_result(final, source_filename)
