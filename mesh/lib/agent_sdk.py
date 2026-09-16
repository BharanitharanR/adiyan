"""
The one surface a new agent is expected to know about to reach anything
outside itself - another agent, an MCP tool, WhatsApp, the LLM. Tokens,
tiers, mint_token, and ChatOllama construction are all platform plumbing
underneath this, never something agent code imports or calls directly.
Mirrors mesh/lib/config_sdk.py's role for config: a clean SDK an agent
imports, not an internal it reaches around.

    from mesh.lib.agent_sdk import AdiyanAgent
    agent = AdiyanAgent('my_agent_id')

    await agent.notify_owner("some processed result")
    result = await agent.search_knowledge_base("refund policy")
    answer = await agent.ask("Summarize this: " + text)

Named methods (search_knowledge_base, recall_contact_memory, schedule, ...)
exist for every documented platform use case - see the Developer Guide's
platform benefits menu. call_agent()/call_tool() stay available underneath
for anything not yet covered by a named method, but reaching for a named
one first means the caller never sees a raw skill_id string or another
agent's URL either.

Deliberately not a thin re-export of mint_token/call_agent/call_tool/
ChatOllama for skill code to call directly - every real skill in this
mesh minted its own token and constructed its own ChatOllama by hand
before this existed, which is exactly the "every caller re-implements
the same plumbing, one gets it wrong eventually" shape this class exists
to close off. One place mints; every agent gets it by construction.

The only thing agent code still has to do by hand is add its own tier to
mesh/lib/permissions_config.json (see TIER_SUFFIX below for the exact
name) - that's a deliberate, explicit, human-reviewed grant, not
something this class auto-creates at runtime.
"""
import base64
import json
import logging
from typing import Any, Dict, List, Optional, Type

import httpx
from langchain_ollama import ChatOllama
from pydantic import BaseModel

from mesh.lib import config_sdk, llm_log, memory_hook, permissions, persona_hook
from mesh.lib.a2a_client import call_agent as _call_agent
from mesh.lib.mcp_client import call_tool as _call_tool
from mesh.lib.utilities.whatsapp.notify_owner import WHATSAPP_MCP_URL, notify_owner as _notify_owner

logger = logging.getLogger('AgentSDK')

# The tier-naming convention every *_service tier added tonight already
# follows (adiyan_reader_service, and this file's own docstring example) -
# centralized here once so "what does my agent's tier need to be called"
# has exactly one answer, not one invented per agent.
TIER_SUFFIX = '_service'

# Orpheus's own token ceiling (mesh/adiyan_reader/tts.py's own
# ORPHEUS_MAX_TOKENS, moved here as this call shape's default) - a fixed,
# generously-sized cap that every successful generation this session
# finished well under, not a per-chunk estimate that could (and did)
# undershoot for a short chunk. Callers with a different real budget pass
# their own num_predict; this is only the fallback.
_RAW_DEFAULT_MAX_TOKENS = 6000

# Every agent that calls Ollama directly hardcodes this same URL - centralized
# here for the same reason WHATSAPP_MCP_URL was in notify_owner.py: one
# constant, not one copy-pasted into every agent's own constants.py.
OLLAMA_URL = 'http://localhost:11434'

# Memory Agent and cron_trigger's real addresses - every existing agent
# that calls either one hardcodes the exact same value in its own
# constants.py today. Centralized here so the named methods below
# (search_knowledge_base, schedule, etc.) don't ask the caller to supply a
# URL for a well-known platform service they didn't build.
MEMORY_AGENT_URL = 'http://127.0.0.1:8423'
CRON_TRIGGER_URL = 'http://127.0.0.1:8421/mcp'

# Where ask() sends every plain-text call - see mesh/inference_router/
# for the actual "run locally or offload to a peer" decision, which
# lives there now, not inline in this file.
INFERENCE_ROUTER_URL = 'http://127.0.0.1:8441'


async def _raw_ollama_stream(
    prompt: str, model: str, max_tokens: int, temperature: float,
    top_p: float, repetition_penalty: float, think: Optional[bool] = None,
    stop: Optional[List[str]] = None,
) -> List[str]:
    """Streams a raw (non-chat-templated) completion from Ollama and
    returns every token piece as its own string, in order - moved
    verbatim from mesh/adiyan_reader/tts.py's own _generate_tokens() (see
    that module's docstring for why raw=True is non-negotiable: Ollama's
    default chat-templating breaks Orpheus's expected
    "<|audio|>voice: text<|eot_id|>" format entirely, and the model
    replies with ordinary conversational text instead of the
    <custom_token_N> audio codes SNAC needs).

    Module-level, not a method - this has no need of self.agent_id or any
    other AdiyanAgent state; ask()'s raw=True branch is the only caller."""
    payload = {
        'model': model,
        'prompt': prompt,
        'raw': True,
        'stream': True,
        'options': {
            'num_predict': max_tokens,
            'temperature': temperature,
            'top_p': top_p,
            'repeat_penalty': repetition_penalty,
        },
    }
    if stop:
        # Inside 'options', not top-level - matches Ollama's own Options
        # shape (confirmed against the installed ollama client's Options
        # type, which carries 'stop' alongside temperature/top_p/etc., not
        # as a sibling of 'model'/'prompt' the way 'think' is). Needed for
        # any raw-completion model that doesn't reliably stop on its own -
        # confirmed live this session with sarvam-1 (mashriram/sarvam-1),
        # which kept generating a fabricated follow-up exchange past its
        # real answer without one.
        payload['options']['stop'] = stop
    if think is not None:
        # Top-level, not inside 'options' - matches Ollama's own /api/
        # generate request shape (confirmed against the installed ollama
        # client's generate() signature). Irrelevant for Orpheus itself
        # (not a thinking model), but this helper has no caller-specific
        # logic, so there's no reason to special-case it out.
        payload['think'] = think
    tokens: List[str] = []
    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream('POST', f'{OLLAMA_URL}/api/generate', json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                data = json.loads(line)
                if data.get('response'):
                    tokens.append(data['response'])
                if data.get('done'):
                    if data.get('done_reason') == 'length':
                        # max_tokens is already a generous, proven-sufficient
                        # ceiling for any chunk this size ever needs (see
                        # ask()'s own _RAW_DEFAULT_MAX_TOKENS docstring) -
                        # hitting it anyway points to a genuine repetition
                        # loop, not an undersized budget. A plain warning,
                        # not a "raise the cap" instruction to act on.
                        logger.warning(
                            f'Raw generation hit max_tokens={max_tokens} before finishing '
                            f'({len(prompt)}-char prompt) - likely a repetition loop, not an '
                            'undersized budget. Output for this call is cut off.'
                        )
                    break
    return tokens


class AdiyanAgent:
    """One instance per agent, constructed once with that agent's own id -
    every method call mints a token against `<agent_id>_service`
    internally, never asking the caller to know that tier, a token, or
    ChatOllama exist."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self._tier = f'{agent_id}{TIER_SUFFIX}'

    def _mint(self) -> str:
        return permissions.mint_token(self.agent_id, self._tier)

    async def call_agent(self, url: str, skill_id: str, params: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        """Calls another agent's skill. Requires `<agent_id>.<skill_id>` in
        your own tier's allow list (mesh/lib/permissions_config.json) - see
        the Developer Guide's platform benefits menu for which key each
        capability needs. Raises RuntimeError on a failed/rejected/empty
        task, same as the underlying mesh.lib.a2a_client.call_agent this
        wraps - not swallowed here, since a caller asking for a result
        needs to know when one didn't come back."""
        kwargs = {'token': self._mint()}
        if timeout is not None:
            kwargs['timeout'] = timeout
        return await _call_agent(url, skill_id, params, **kwargs)

    async def call_tool(self, url: str, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Calls an MCP tool. Requires `mcp.<server_name>.<tool_name>` in
        your own tier's allow list. Raises on failure, same as the
        underlying mesh.lib.mcp_client.call_tool this wraps."""
        return await _call_tool(url, tool_name, arguments, token=self._mint())

    async def ask(
        self, prompt: str, *, stage: str = 'default', model: str = 'qwen3:8b-16k',
        temperature: float = 0.4, schema: Optional[Type[BaseModel]] = None,
        tools: Optional[list] = None, image_b64: Optional[str] = None,
        image_mimetype: Optional[str] = None, community: Optional[str] = None,
        raw: bool = False, num_predict: Optional[int] = None,
        top_p: float = 0.9, repetition_penalty: float = 1.1,
        think: Optional[bool] = None, stop: Optional[List[str]] = None,
    ) -> Any:
        """Calls the mesh's LLM. No permission key needed - every agent
        reaches it the same way, same as config/storage access.

        For a plain-text call (schema=None), this is a thin client to
        Inference Router's own complete skill (mesh/inference_router/) -
        the actual "run this locally or offload it" decision, including
        whether this machine is currently busy, lives entirely in that
        one place, not duplicated inline in every calling agent's own
        process. Nothing in this signature says "Ollama" or
        "inference_router" anywhere, on purpose - which backend answers,
        and how that decision gets made, can change without touching a
        single caller.

        A schema call (structured output) always runs against local
        Ollama directly, in this process, never through Inference Router
        and never offloadable - see `schema` below for why.

        stage/model/temperature: `stage` is a per-agent, per-pipeline-step
        name (e.g. 'craft_reply', 'classify_intent') - resolved through
        config_sdk.get_stage_config() so the actual model/temperature used
        become dashboard-editable later without a code change, seeded from
        the model/temperature given here the first time this stage is ever
        called. Two different `stage` names on the same agent get
        independently tunable configs; reusing 'default' for everything
        works too if you don't need that. For a plain-text call this
        resolution actually happens inside Inference Router, keyed on
        your own agent_id - which process does the lookup doesn't change
        what config_sdk returns, since it's stored centrally, keyed by
        agent_id either way.

        schema: a pydantic BaseModel - if given, the response is parsed
        into it (structured output) and that instance is returned. If
        omitted, the model's plain text reply is returned as a str.
        Never raises for a bad response shape when schema is given -
        langchain's own with_structured_output() retries/repairs that
        internally; this method doesn't add a second layer of retry.
        Always local, never offloadable, regardless of `community`: a
        pydantic model class can't cross an A2A call to another process,
        so a peer's raw completion could never honor this contract the
        way local with_structured_output() does.

        community: the ENTIRE opt-in to compute_share's peer-sharing
        network for this one call. Whatever prompt you build IS what
        leaves this machine if Inference Router decides to offload it -
        see mesh/compute_share/README.md's own trust-boundary section.
        Deliberately not a boolean a careless `True` could flip by
        accident: pass the literal string 'communitySearch' to opt in.
        Omitted (the default): local-only, always - Inference Router
        never even considers a peer for this call, and a local failure
        raises normally, exactly as if compute_share didn't exist.

        tools: a list of langchain @tool-decorated callables to bind for
        this call - the model returns tool-call decisions
        (response.tool_calls) instead of plain text; the caller executes
        whichever tool was chosen itself and continues its own loop, same
        shape every ReAct-style caller already used directly against
        ChatOllama.bind_tools() before this existed. Always local, never
        offloadable, regardless of `community`, for the same reason as
        schema: a live Python tool object can't cross an A2A call to
        another process, so a peer has no way to even see what tools were
        offered, let alone honor a tool-call contract against them. Mutually
        exclusive with `schema` - pass one or neither, not both.

        image_b64/image_mimetype: attach one image (vision input) to this
        call - pass both together or neither. Always local, never
        offloadable, regardless of `community`: a bigger trust jump than
        text alone (a user's actual uploaded photo, not just a prompt
        string), and combines with `schema` if both are given (a
        structured read of an image), but never with `tools`.

        raw/num_predict/top_p/repetition_penalty: a raw Ollama completion
        (raw=True on the API call, bypassing chat-templating entirely) -
        built for mesh/adiyan_reader/tts.py's Orpheus calls, which need
        the model's own custom_token_N stream verbatim, not a parsed
        completion. Returns the raw list of streamed token strings, not
        text - SNAC decoding is the caller's job, not ask()'s. Always
        local, never offloadable, for the same reason as schema/tools/
        image: a raw token stream has no meaning to a peer that isn't
        running the exact same model. Mutually exclusive with schema/
        tools/image_b64 - pass at most one call shape per call.

        Memory read-side hook (Phase 5): if mesh/lib/bootstrap.py's
        executor wrapper resolved an identity for the caller of this whole
        A2A request, that identity's known facts are prepended to `prompt`
        automatically here - no caller of ask() changes. Deliberately NOT
        applied when schema, image_b64, or raw is given: schema calls
        (classify_skill/extract_parameters in skill_router.py, the two
        highest-volume callers of this method) expect the model to parse
        structured output strictly from `prompt` alone, and a stray
        "what you know about this person" block ahead of that instruction
        risks degrading extraction accuracy for no benefit. raw is
        excluded for a sharper reason: it would inject that same block
        straight into an Orpheus synthesis prompt, and Orpheus - having no
        idea that text isn't meant to be spoken - would literally
        vocalize your own stored facts at the start of a voice note. The
        `tools` (ReAct) and plain-text paths below are genuine answer-
        generation, where this is exactly the fix for the gap named
        earlier tonight: recall today only happens if the model chooses
        to call an optional tool; this happens unconditionally.

        Business-persona hook (mesh/lib/persona_hook.py): same shape and
        same exclusions as the memory hook just above, for the same
        reasons - if mesh/lib/bootstrap.py's executor wrapper resolved an
        active business persona for the caller of this whole A2A request
        (never for the owner's own messages - see persona_hook.py's own
        docstring), it's prepended to `prompt` right alongside the memory
        block, automatically, no caller of ask() changes. This is what
        makes a brand-new agent business-persona-aware the moment it calls
        bootstrap.serve() like every other agent already must - adding its
        own `business_persona_context` seed constant is the only step it
        ever needs; reading it back happens here, once, forever.

        think: explicit thinking-mode toggle for models that support it
        (confirmed live this session: qwen3:8b-16k reports 'thinking' in
        its own /api/show capabilities) - True forces it on, False forces
        it off, None (the default) leaves whatever the model does by
        default untouched, so every existing caller is byte-for-byte
        unaffected by this parameter's mere existence. Applies to every
        call shape (plain-text, schema, tools, image, raw) since it's a
        model-level generation setting, not tied to any one of them.
        Thinking content, when the model produces it, is captured
        separately from the actual answer (langchain-ollama's own
        additional_kwargs['reasoning_content'], never mixed into the
        returned text/object) and included in this call's line in
        ~/.Adiyan/logs/llm_calls.log for the text/tools/image paths - not
        guaranteed for schema calls, since with_structured_output's own
        parsing doesn't reliably preserve it.

        stop: only meaningful with raw=True - a list of strings that halt
        generation the moment Ollama produces one of them (the match
        itself is not included in the returned tokens). Built for raw-
        completion models with no reliable stopping point of their own -
        confirmed live this session with sarvam-1 (mashriram/sarvam-1),
        few-shot-prompted to answer a question, which kept generating a
        second, fabricated question-and-answer exchange past its real
        answer with nothing to stop it. Ignored (never sent to Ollama) for
        any other call shape."""
        # Same exclusion for both blocks, same reasoning: skill_router.py's
        # classify()/extract() (stage='classify'/'extract', the two
        # highest-volume schema callers of this method) need the model
        # parsing structured output strictly from `prompt` alone - a
        # business persona paragraph ahead of a fixed skill list or an
        # exact-wording extraction risks degrading that decision for no
        # benefit. raw would literally vocalize either block at the start
        # of a synthesized voice note, since Orpheus has no idea that text
        # isn't meant to be spoken.
        #
        # Deliberately NOT "any schema call" - confirmed live this session
        # that this excluded far more than classify/extract without
        # anyone intending it to: Journal Agent's craft_reflection_prompt
        # (stage='craft_generic'/'craft_personalized') and Scheduler's
        # compose_generic/resolve_schedule all pass a schema too, purely to
        # get a clean {field: value} return shape, not because they're
        # picking from a fixed list or extracting exact wording - these are
        # genuine content-generation calls that should see both blocks the
        # same as any plain-text call. Scoped to the two real stage names
        # instead, so adding business_persona_context to more agents (see
        # persona_hook.py's own docstring) actually reaches their prompts
        # instead of being silently excluded by the mere presence of a
        # schema.
        _inject_context = (schema is None or stage not in ('classify', 'extract')) and image_b64 is None and not raw
        persona_context = persona_hook.CURRENT_PERSONA_CONTEXT.get() if _inject_context else ''
        memory_context = memory_hook.CURRENT_CONTEXT.get() if _inject_context else ''
        # Persona (who this agent is being, platform-wide for this whole
        # request) ahead of memory (what's specifically known about this
        # one person) - the frame comes before the specifics it's applied
        # to. join() over two plain string concatenations so neither block
        # being empty (the common case - most deployments never activate a
        # vertical) needs its own special-cased if/elif here.
        context_prefix = '\n\n'.join(block for block in (persona_context, memory_context) if block)
        if context_prefix:
            prompt = f'{context_prefix}\n\n{prompt}'

        # Central LLM logging (mesh/lib/llm_log.py): every branch below logs
        # its own prompt/response right at its own return point, since each
        # branch resolves a different model name and shapes its response
        # differently - a single log call after an if/elif chain would lose
        # which branch actually ran. See llm_log.py's own docstring for why
        # this lives outside agent_sdk.py and writes to one shared file
        # rather than each agent's own log.
        if image_b64 is not None:
            from langchain_core.messages import HumanMessage
            cfg = await config_sdk.get_stage_config(
                self.agent_id, stage, {'model': model, 'temperature': temperature},
            )
            llm = ChatOllama(model=cfg['model'], base_url=OLLAMA_URL, temperature=cfg['temperature'], reasoning=think)
            message = HumanMessage(content=[
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': f'data:{image_mimetype};base64,{image_b64}'},
            ])
            try:
                if schema is not None:
                    result = await llm.with_structured_output(schema).ainvoke([message])
                    llm_log.log_call(self.agent_id, stage, 'image+schema', cfg['model'], prompt, result)
                    return result
                result = await llm.ainvoke([message])
                # The full message, not result.content - _stringify() pulls
                # out reasoning_content too when think produced any, and
                # this call's own return value below is untouched either way.
                llm_log.log_call(self.agent_id, stage, 'image', cfg['model'], prompt, result)
                return result.content
            except Exception as e:
                llm_log.log_call(self.agent_id, stage, 'image', cfg['model'], prompt, None, error=str(e))
                raise

        if tools is not None:
            cfg = await config_sdk.get_stage_config(
                self.agent_id, stage, {'model': model, 'temperature': temperature},
            )
            llm = ChatOllama(
                model=cfg['model'], base_url=OLLAMA_URL, temperature=cfg['temperature'], reasoning=think,
            ).bind_tools(tools)
            try:
                result = await llm.ainvoke(prompt)
            except Exception as e:
                llm_log.log_call(self.agent_id, stage, 'tools', cfg['model'], prompt, None, error=str(e))
                raise
            llm_log.log_call(self.agent_id, stage, 'tools', cfg['model'], prompt, result)
            return result

        if schema is not None:
            cfg = await config_sdk.get_stage_config(
                self.agent_id, stage, {'model': model, 'temperature': temperature},
            )
            llm = ChatOllama(model=cfg['model'], base_url=OLLAMA_URL, temperature=cfg['temperature'], reasoning=think)
            structured = llm.with_structured_output(schema)
            try:
                result = await structured.ainvoke(prompt)
            except Exception as e:
                llm_log.log_call(self.agent_id, stage, 'schema', cfg['model'], prompt, None, error=str(e))
                raise
            llm_log.log_call(self.agent_id, stage, 'schema', cfg['model'], prompt, result)
            return result

        if raw:
            # Always local, never offloadable - same class as schema/tools/
            # image above. cfg['model'] still goes through the normal
            # dashboard-editable stage config, so Orpheus's own model name
            # becomes an ordinary config_sdk setting instead of a value
            # only changeable by editing tts.py.
            cfg = await config_sdk.get_stage_config(
                self.agent_id, stage, {'model': model, 'temperature': temperature},
            )
            effective_max_tokens = num_predict or _RAW_DEFAULT_MAX_TOKENS
            try:
                tokens = await _raw_ollama_stream(
                    prompt, cfg['model'], effective_max_tokens, cfg['temperature'], top_p, repetition_penalty, think,
                    stop,
                )
            except Exception as e:
                llm_log.log_call(self.agent_id, stage, 'raw', cfg['model'], prompt, None, error=str(e))
                raise
            # Joined, not the raw list - a human reading llm_calls.log wants
            # to see the actual <custom_token_N> sequence Orpheus produced
            # (to spot a repetition loop or an early stop at a glance), not
            # a Python list repr of a few hundred short strings.
            llm_log.log_call(self.agent_id, stage, 'raw', cfg['model'], prompt, ''.join(tokens))
            return tokens

        # A fixed platform identity, not self.agent_id/self._tier - ask()
        # itself never mints against the calling agent's own tier for
        # this call. Routing an LLM call is a platform capability every
        # agent gets by construction, not something threaded through
        # each agent's own permission grant (see platform_llm_client's
        # own description in permissions_config.json).
        token = permissions.mint_token('adiyan_platform', 'platform_llm_client')
        payload = {
            'caller_agent_id': self.agent_id, 'stage': stage, 'prompt': prompt,
            'model': model, 'temperature': temperature, 'community': community,
        }
        if think is not None:
            # Only sent when actually requested - a running inference_router
            # process started before this key existed would otherwise 500 on
            # EVERY plain-text call, not just ones that asked for think,
            # since Python raises on any unexpected kwarg regardless of its
            # value. Confirmed live this session: this is exactly what
            # happened before this guard was added, right up until that
            # process gets restarted onto the new code.
            payload['think'] = think
        try:
            result = await _call_agent(INFERENCE_ROUTER_URL, 'complete', payload, token=token)
        except Exception as e:
            llm_log.log_call(self.agent_id, stage, 'text', model, prompt, None, error=str(e))
            raise
        completion = result.get('completion')
        reasoning_content = result.get('reasoning_content')
        # Logged shape only - completion (the actual return value below)
        # never carries the thinking trace mixed in.
        log_value = f'[thinking]\n{reasoning_content}\n[/thinking]\n\n{completion}' if reasoning_content else completion
        llm_log.log_call(self.agent_id, stage, 'text', model, prompt, log_value)
        return completion

    async def search_knowledge_base(self, query: str, top_k: int = 3) -> Dict[str, Any]:
        """Long-term memory: searches documents the owner has uploaded
        (PDFs, notes, ID cards, etc.) for `query` - global, not scoped to
        one contact. Requires 'memory.search_knowledge_base' in your tier."""
        return await self.call_agent(MEMORY_AGENT_URL, 'search_knowledge_base', {'query': query, 'top_k': top_k})

    async def share_knowledge_document(self, query: str) -> Dict[str, Any]:
        """Long-term memory: finds and returns the original uploaded
        document (not just a text snippet) that best matches `query`.
        Requires 'memory.share_knowledge_document' in your tier."""
        return await self.call_agent(MEMORY_AGENT_URL, 'share_knowledge_document', {'query': query})

    async def recall_contact_memory(self, contact_name: str, query: str, top_k: int = 3) -> Dict[str, Any]:
        """Short-term memory: raw read-back of actual past conversation
        with one specific contact - moods, goals, things they said
        directly. Not for using that history to reason or advise; this is
        only "what do we know." Requires 'memory.recall_contact_memory'
        in your tier."""
        return await self.call_agent(
            MEMORY_AGENT_URL, 'recall_contact_memory', {'contact_name': contact_name, 'query': query, 'top_k': top_k},
        )

    async def remember_interaction(self, contact_name: str, user_text: str, reply_text: str) -> Dict[str, Any]:
        """Short-term memory: writes one new turn (what the contact said,
        what was replied) to that contact's conversation history. Requires
        'memory.remember_interaction' in your tier."""
        return await self.call_agent(
            MEMORY_AGENT_URL, 'remember_interaction',
            {'contact_name': contact_name, 'user_text': user_text, 'reply_text': reply_text},
        )

    async def schedule(
        self, job_id: str, invoke_at: str, target_agent_url: str, skill_id: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Registers a one-shot wake-up: at `invoke_at` (ISO 8601), fires
        `target_agent_url`'s `skill_id` with `params`, via cron_trigger.
        Calling this again with the same `job_id` replaces the prior
        registration - cron_trigger doesn't know about recurrence, so a
        skill that wants to run again re-registers itself each time it
        fires (see mesh/adiyan_reader/skills/read_next_page.py for the
        pattern: register tomorrow's fire at the end of today's run).
        Requires 'mcp.cron_trigger.register_trigger' in your tier."""
        return await self.call_tool(CRON_TRIGGER_URL, 'register_trigger', {
            'job_id': job_id, 'invoke_at': invoke_at,
            'target_agent_url': target_agent_url, 'skill_id': skill_id, 'params': params or {},
        })

    async def notify_owner(self, text: str) -> bool:
        """Sends `text` to the owner's own WhatsApp (self-chat). True on
        send, False otherwise (owner's phone couldn't be resolved, this
        agent's tier isn't allowed one of the two calls it needs, or
        whatsapp_mcp/OpenWA is unreachable) - never raises, per this
        mesh's whatsapp-silent-on-failure rule.

        Requires mesh/lib/permissions_config.json to have a
        `<agent_id>_service` tier allowing both
        'mcp.whatsapp.get_own_phone' and 'mcp.whatsapp.send_message' - see
        adiyan_reader_service for the exact shape to copy."""
        return await _notify_owner(self.agent_id, self._tier, text)

    async def resolve_chat_id(self, phone: str) -> Optional[str]:
        """Resolves an arbitrary phone number to its real WhatsApp chat id
        - use this (not notify_owner) when messaging someone other than
        the owner, e.g. a specific client a job is registered against.
        Not simply f'{phone}@c.us' - some contacts are addressed via
        WhatsApp's newer @lid scheme instead, with a lid number
        unrelated to the phone number; the actual resolution happens
        server-side against the live session. Returns None if OpenWA has
        no match. Requires 'mcp.whatsapp.resolve_chat_id' in your tier."""
        result = await self.call_tool(WHATSAPP_MCP_URL, 'resolve_chat_id', {'phone': phone})
        return result.get('chat_id')

    async def send_message_to(self, chat_id: str, text: str) -> Dict[str, Any]:
        """Sends `text` to an already-resolved chat_id (see
        resolve_chat_id). Use notify_owner instead if the destination is
        the owner - it resolves the phone for you in one call. Requires
        'mcp.whatsapp.send_message' in your tier."""
        return await self.call_tool(WHATSAPP_MCP_URL, 'send_message', {'chat_id': chat_id, 'text': text})

    async def send_voice_to(self, chat_id: str, audio: bytes, mimetype: str = 'audio/ogg; codecs=opus') -> Dict[str, Any]:
        """Sends `audio` to an already-resolved chat_id as a real WhatsApp
        voice note (PTT), not a file attachment. `audio` must be
        Opus-encoded OGG bytes - OpenWA's own docs note plain WAV/PCM
        doesn't reliably render the PTT waveform UI. Requires
        'mcp.whatsapp.send_voice' in your tier."""
        content_b64 = base64.b64encode(audio).decode('ascii')
        return await self.call_tool(
            WHATSAPP_MCP_URL, 'send_voice', {'chat_id': chat_id, 'content_b64': content_b64, 'mimetype': mimetype},
        )
