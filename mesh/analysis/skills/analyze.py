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
# much of one raw tool result gets read at once. Generous enough for a
# realistic document/memory chunk; a document too big to fit is a known,
# separate limit (see mesh/ANALYSIS_AGENT_PLAN.md), not solved here.
OBSERVATION_CHAR_CAP = 6000


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


def _cap(text: str) -> str:
    if len(text) <= OBSERVATION_CHAR_CAP:
        return text
    return text[:OBSERVATION_CHAR_CAP] + f'\n\n[...truncated, {len(text) - OBSERVATION_CHAR_CAP} more characters not shown]'


async def _get_message(key: str, default: str, **kwargs: str) -> str:
    """Mongo-backed user-facing/tool-observation copy - a fallback or
    "nothing found" message, not the ReAct loop's own reasoning prompts
    (those stay hardcoded for now, a separate, not-yet-agreed piece of
    scope). Falls back to `default` (formatted) if the on-file template is
    malformed - confirmed live once already this session
    (orchestrator/humanize.py's own prompt) that a template missing an
    expected placeholder must not break the caller, especially here: these
    strings often feed straight back into the ReAct loop's own reasoning
    as a tool observation, not just a WhatsApp reply."""
    template = await config_sdk.get_constant(AGENT_ID, key, default)
    try:
        return template.format(**kwargs)
    except Exception as e:
        logger.warning(f'Message template {key!r} on file is malformed, using default: {e}')
        return default.format(**kwargs)


def _make_tools(contact_name: Optional[str]):
    """Tool functions as closures, not module-level - contact_name varies
    per call, and multiple analyse_this calls can run concurrently with
    different callers; module-level shared state would let them corrupt
    each other."""

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
            return await _get_message('msg_no_matching_document', 'No matching document found in the knowledge base.')
        return f"Best match: {result['source_filename']}"

    @tool
    async def read_document(source_filename: str) -> str:
        """Read a specific document's full text, by its exact filename - get
        this from search_documents or list_documents first, don't guess one."""
        token = permissions.mint_token('analysis', 'service')
        try:
            result = await call_agent(MEMORY_AGENT_URL, 'get_document_text', {'source_filename': source_filename}, token=token)
        except Exception as e:
            return f'read_document failed: {e}'
        if not result.get('found'):
            return await _get_message(
                'msg_document_not_found', "No document found with filename '{source_filename}'.",
                source_filename=source_filename,
            )
        return _cap(result['text'])

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
            return await _get_message('msg_kb_empty', 'The knowledge base is empty - no documents have been uploaded.')
        return '\n'.join(docs)

    @tool
    async def recall_memory(query: str) -> str:
        """Recall what's known about the specific person asking, from past
        conversations - moods, goals, things they've mentioned before. Not
        for document content, that's search_documents' job."""
        if not contact_name:
            return await _get_message(
                'msg_no_contact', 'No specific person is associated with this request - nothing to recall.',
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
            return await _get_message('msg_nothing_in_memory', 'Nothing relevant found in conversation memory.')
        return '\n'.join(f'- {s}' for s in snippets)

    @tool
    async def discover_agents() -> str:
        """List every other agent currently available in the mesh and what
        each one can do - use this to find an agent worth asking for help
        with something outside your own tools, e.g. a business-specific
        agent installed for this deployment."""
        agents = await list_agents()
        no_agents_msg = await _get_message('msg_no_agents_discoverable', 'No other agents are currently discoverable.')
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
                agent_id=agent_id,
            )
        token = permissions.mint_token('analysis', 'service')
        try:
            result = await call_agent_with_text(match['url'], request, token=token)
        except Exception as e:
            return f'consult_agent failed: {e}'
        return _cap(str(result))

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

    tools = [search_documents, read_document, list_documents, recall_memory, discover_agents, consult_agent, finish]
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
        'Never state a specific real-world detail (an event, an exact date, '
        'a price, current availability) as fact unless a tool actually gave '
        'it to you - if you are unsure, say so or leave it out, do not assume. '
        + grounding_rule
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
    strict = await config_sdk.get_constant(AGENT_ID, 'strict_grounding', DEFAULT_STRICT_GROUNDING)
    tools, tools_by_name = _make_tools(contact_name)

    scratchpad = Scratchpad()
    if source_filename:
        # Already known which document this is about (the upload+instruct
        # combined flow) - seed the scratchpad so the loop reads it first
        # instead of re-discovering something it was already handed, while
        # still being free to check elsewhere if that turns out not to be enough.
        scratchpad.documents_known.append(source_filename)
        scratchpad.open_questions.append(f'The document {source_filename!r} is already known to be relevant - read it first.')

    for _ in range(MAX_STEPS):
        response = await _decide_next_step(instruction, scratchpad, tools, cfg, strict)

        if not response.tool_calls:
            # Answered directly without calling finish - treat its own text
            # as the answer, a graceful outcome, not an error.
            fallback = await _get_message('msg_no_clear_answer', "I wasn't able to find a clear answer.")
            return _package_result(response.content or fallback, source_filename)

        call = response.tool_calls[0]
        if call['name'] == 'finish':
            return _package_result(call['args'].get('answer', ''), source_filename)

        tool_obj = tools_by_name.get(call['name'])
        if tool_obj is None:
            observation = await _get_message('msg_unknown_tool', 'Unknown tool: {tool_name}', tool_name=call['name'])
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
        else:
            scratchpad = await _compact(instruction, scratchpad, call['name'], _cap(str(observation)), cfg)

    # Hit MAX_STEPS without finish() - never error out, answer from
    # whatever was gathered.
    final = await _final_answer(instruction, scratchpad, cfg, strict)
    return _package_result(final, source_filename)
