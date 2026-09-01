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
with no code change here. See docs/ANALYSIS_AGENT_PLAN.md for the full
design discussion, including what's deliberately deferred (internet
search, Gmail) and why.
"""
import base64
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from mesh.analysis.constants import AGENT_ID, MEMORY_AGENT_URL
from mesh.lib import config_sdk, permissions, tool_resolution
from mesh.lib.a2a_client import call_agent, call_agent_with_text
from mesh.lib.agent_sdk import AdiyanAgent
from mesh.lib.config import load_runtime_config, load_seed_config
from mesh.lib.errors import describe_exception
from mesh.lib.registry_client import list_agents

AGENT_CODE_DIR = Path(__file__).parent.parent
logger = logging.getLogger('AnalyzeDocument')

_agent = AdiyanAgent(AGENT_ID)

# This agent's own constant/prompt-template defaults, declared in
# seed_config.json rather than hardcoded here - see mesh/lib/config_sdk.py's
# seed_from_file() and docs/TOOL_RESOLUTION_DESIGN.md's sibling design note.
# Read once at import time, not per-call - the file only changes when this
# agent's code is deployed, same lifetime as any other module-level
# constant. A key missing from the file (shouldn't happen once seeded, but
# defensively) falls back to an empty template rather than a KeyError.
_SEED = load_seed_config(AGENT_CODE_DIR)


def _seeded(key: str) -> Dict[str, Any]:
    return _SEED.get(key, {'value': '', 'description': ''})

MAX_STEPS = 10

# Past this length, the synthesized result is delivered as a file instead
# of a WhatsApp text message - see handle_message.py's content_b64
# delivery convention (any skill result carrying content_b64 is understood
# as "deliver a file," not specific to this skill_id).
FILE_DELIVERY_THRESHOLD_CHARS = 1500

# strict_grounding, observation_char_cap, and doc_search_top_k's own
# defaults + descriptions all live in seed_config.json now (see _seeded()
# above) - not here. Confirmed live once already: observation_char_cap's
# original hardcoded default (a few thousand characters) was too small for
# anything book-length - read_document() on a 167-chunk book got capped
# down to just its cover page and table of contents before the ReAct loop
# ever reasoned about it. That history lives in the seed file's own
# description field now, not a code comment next to a constant nothing
# reads anymore.


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


async def _get_message(key: str, **kwargs: str) -> str:
    """Mongo-backed user-facing/tool-observation copy - a fallback or
    "nothing found" message, not the ReAct loop's own reasoning prompts
    (which follow this exact same seed_config.json pattern too, see
    _decide_next_step/_compact/_final_answer). No default/description
    passed in by the caller anymore - every message key's value+description
    lives in seed_config.json, looked up here via _seeded(), so nothing
    calling this needs to know or repeat that text. Falls back to the seed
    value (formatted) if the on-file template is malformed - confirmed
    live once already this session (orchestrator/humanize.py's own
    prompt) that a template missing an expected placeholder must not
    break the caller, especially here: these strings often feed straight
    back into the ReAct loop's own reasoning as a tool observation, not
    just a WhatsApp reply."""
    seeded = _seeded(key)
    template = await config_sdk.get_constant(AGENT_ID, key, seeded['value'], description=seeded['description'] or None)
    try:
        return template.format(**kwargs)
    except Exception as e:
        logger.warning(f'Message template {key!r} on file is malformed, using seed default: {e}')
        return seeded['value'].format(**kwargs)


# MCP tool loading + connection lifecycle moved to mesh/lib/tool_resolution.py
# and mesh/lib/mcp_registry.py - see docs/TOOL_RESOLUTION_DESIGN.md.


def _make_tools(contact_name: Optional[str], observation_char_cap: int, doc_search_top_k: int, react_cfg: Dict[str, Any]):
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
            return f'search_documents failed: {describe_exception(e)}'
        if not result.get('found'):
            return await _get_message('msg_no_matching_document')
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
            return f'read_document failed: {describe_exception(e)}'
        if not result.get('found'):
            return await _get_message('msg_document_not_found', source_filename=source_filename)
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
            return f'search_within_document failed: {describe_exception(e)}'
        if not result.get('found'):
            return await _get_message('msg_no_relevant_chunks', source_filename=source_filename)
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
            return f'list_documents failed: {describe_exception(e)}'
        docs = result.get('documents', [])
        if not docs:
            return await _get_message('msg_kb_empty')
        return '\n'.join(docs)

    @tool
    async def recall_memory(query: str) -> str:
        """Recall what's known about the specific person asking,everything from their past
        conversations they've mentioned before."""
        if not contact_name:
            return await _get_message('msg_no_contact')
        token = permissions.mint_token('analysis', 'service')
        try:
            result = await call_agent(MEMORY_AGENT_URL, 'recall_contact_memory', {
                'contact_name': contact_name, 'query': query, 'top_k': 5,
            }, token=token)
        except Exception as e:
            return f'recall_memory failed: {describe_exception(e)}'
        snippets = result.get('snippets', [])
        if not snippets:
            return await _get_message('msg_nothing_in_memory')
        return '\n'.join(f'- {s}' for s in snippets)

    @tool
    async def discover_agents() -> str:
        """List every other agent currently available in the mesh and what
        each one can do - use this to find an agent worth asking for help
        with something outside your own tools."""
        agents = await list_agents()
        no_agents_msg = await _get_message('msg_no_agents_discoverable')
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
            return await _get_message('msg_agent_not_registered', agent_id=agent_id)
        token = permissions.mint_token('analysis', 'service')
        try:
            result = await call_agent_with_text(match['url'], request, token=token)
        except Exception as e:
            return f'consult_agent failed: {describe_exception(e)}'
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

    @tool
    async def resolve_and_execute(question: str) -> str:
        """Look up something about this deployment's own configuration,
        settings, or internal state - e.g. which voice/model an agent
        uses, how many pages have been read, a stage's parameters.
        Describe what you need in plain language. Not for questions about
        a user's uploaded documents - that's search_documents' job."""
        return await tool_resolution.resolve_and_execute(question, react_cfg)

    tools = [
        search_documents, read_document, search_within_document, list_documents,
        recall_memory, discover_agents, consult_agent, resolve_and_execute, finish,
    ]
    return tools, {t.name: t for t in tools}


async def _decide_next_step(instruction: str, scratchpad: Scratchpad, tools, cfg: Dict[str, Any], strict: bool):
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
    seeded = _seeded('decide_next_step_prompt_template')
    template = await config_sdk.get_constant(
        AGENT_ID, 'decide_next_step_prompt_template', seeded['value'], description=seeded['description'],
    )
    scratchpad_json = scratchpad.model_dump_json(indent=2)
    try:
        prompt = template.format(instruction=instruction, scratchpad=scratchpad_json, grounding_rule=grounding_rule)
    except Exception as e:
        logger.warning(f'decide_next_step_prompt_template on file is malformed, using seed default: {e}')
        prompt = seeded['value'].format(
            instruction=instruction, scratchpad=scratchpad_json, grounding_rule=grounding_rule,
        )
    return await _agent.ask(
        prompt, stage='decide_next_step', model=cfg['model'], temperature=cfg['temperature'], tools=tools,
    )


async def _compact(instruction: str, scratchpad: Scratchpad, tool_name: str, observation: str, cfg: Dict[str, Any]) -> Scratchpad:
    """Folds one new observation into an updated scratchpad - merged, not
    appended. This is the step that keeps the decide-step's own input
    bounded: it never sees raw tool output, only this compact result."""
    seeded = _seeded('compact_prompt_template')
    template = await config_sdk.get_constant(
        AGENT_ID, 'compact_prompt_template', seeded['value'], description=seeded['description'],
    )
    scratchpad_json = scratchpad.model_dump_json(indent=2)
    try:
        prompt = template.format(instruction=instruction, scratchpad=scratchpad_json, tool_name=tool_name, observation=observation)
    except Exception as e:
        logger.warning(f'compact_prompt_template on file is malformed, using seed default: {e}')
        prompt = seeded['value'].format(
            instruction=instruction, scratchpad=scratchpad_json, tool_name=tool_name, observation=observation,
        )
    try:
        return await _agent.ask(prompt, stage='compact', model=cfg['model'], temperature=0.2, schema=Scratchpad)
    except Exception as e:
        logger.warning(f'Compaction failed, keeping prior scratchpad unchanged: {e}')
        return scratchpad


async def _final_answer(instruction: str, scratchpad: Scratchpad, cfg: Dict[str, Any], strict: bool) -> str:
    """Reached the step cap without finish() being called - forces a real
    answer from whatever the scratchpad holds, rather than erroring out."""
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
    seeded = _seeded('final_answer_prompt_template')
    template = await config_sdk.get_constant(
        AGENT_ID, 'final_answer_prompt_template', seeded['value'], description=seeded['description'],
    )
    scratchpad_json = scratchpad.model_dump_json(indent=2)
    try:
        prompt = template.format(instruction=instruction, scratchpad=scratchpad_json, grounding_rule=grounding_rule)
    except Exception as e:
        logger.warning(f'final_answer_prompt_template on file is malformed, using seed default: {e}')
        prompt = seeded['value'].format(
            instruction=instruction, scratchpad=scratchpad_json, grounding_rule=grounding_rule,
        )
    result = await _agent.ask(prompt, stage='final_answer', model=cfg['model'], temperature=0.3, schema=_FinalAnswer)
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
    strict_seed = _seeded('strict_grounding')
    strict = await config_sdk.get_constant(AGENT_ID, 'strict_grounding', strict_seed['value'], description=strict_seed['description'])
    cap_seed = _seeded('observation_char_cap')
    observation_char_cap = await config_sdk.get_constant(AGENT_ID, 'observation_char_cap', cap_seed['value'], description=cap_seed['description'])
    top_k_seed = _seeded('doc_search_top_k')
    doc_search_top_k = await config_sdk.get_constant(AGENT_ID, 'doc_search_top_k', top_k_seed['value'], description=top_k_seed['description'])
    tools, tools_by_name = _make_tools(contact_name, observation_char_cap, doc_search_top_k, cfg)

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
            fallback = await _get_message('msg_no_clear_answer')
            return _package_result(response.content or fallback, source_filename)

        call = response.tool_calls[0]
        if call['name'] == 'finish':
            return _package_result(call['args'].get('answer', ''), source_filename)

        tool_obj = tools_by_name.get(call['name'])
        if tool_obj is None:
            observation = await _get_message('msg_unknown_tool', tool_name=call['name'])
        else:
            try:
                observation = await tool_obj.ainvoke(call['args'])
            except Exception as e:
                observation = f"Tool call failed: {e}"

        list_documents_kb_empty_msg = await _get_message('msg_kb_empty') if call['name'] == 'list_documents' else None
        if call['name'] == 'list_documents' and not str(observation).startswith('list_documents failed:') \
                and str(observation) != list_documents_kb_empty_msg:
            # Code-enforced, not LLM-judged - see _merge_document_list()'s
            # own docstring for why this tool's output specifically must
            # never reach the 'extract findings' compaction step. The two
            # guards above matter for the same reason that fix exists:
            # confirmed live, a real Phoenix trace showed the tool's own
            # "list_documents failed: ... Not authorized for this" error
            # string getting split-by-line and appended into
            # documents_known as if it were itself a filename - and it then
            # got carried forward by every subsequent compact() step for
            # the rest of the run, since nothing ever recognized it as
            # garbage rather than a real document. An empty-KB message
            # would fail the exact same way for the exact same reason.
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
