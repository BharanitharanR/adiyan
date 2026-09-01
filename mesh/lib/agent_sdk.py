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
import asyncio
import base64
from typing import Any, Dict, Optional, Type

from langchain_ollama import ChatOllama
from pydantic import BaseModel

from mesh.lib import config_sdk, permissions
from mesh.lib.a2a_client import call_agent as _call_agent
from mesh.lib.mcp_client import call_tool as _call_tool
from mesh.lib.utilities.whatsapp.notify_owner import WHATSAPP_MCP_URL, notify_owner as _notify_owner

# The tier-naming convention every *_service tier added tonight already
# follows (adiyan_reader_service, and this file's own docstring example) -
# centralized here once so "what does my agent's tier need to be called"
# has exactly one answer, not one invented per agent.
TIER_SUFFIX = '_service'

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
COMPUTE_SHARE_URL = 'http://127.0.0.1:8460'

# ask()'s entire opt-in to compute_share's peer network - see that
# method's own docstring for why this is a literal magic string, not a
# boolean. Centralized here (not inlined at the one comparison site) so
# it reads as a real, named constant a caller can import and reference,
# not a string that has to be typed exactly right from memory.
COMMUNITY_SEARCH_MAGIC = 'communitySearch'

# How long ask() waits on local Ollama before treating it as "busy
# enough to consider a peer" - not a measured number, a starting point:
# long enough that a normal local call never trips it, short enough that
# a genuinely stuck local queue doesn't leave a caller waiting
# indefinitely before the community fallback even gets a chance.
LOCAL_ASK_TIMEOUT_SECONDS = 30


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
        community: Optional[str] = None,
    ) -> Any:
        """Calls the mesh's LLM. No permission key needed - every agent
        reaches it the same way, same as config/storage access.

        Today this always means local Ollama - but nothing in this
        signature says "Ollama" anywhere, on purpose. This is the one seam
        meant to absorb a future backend change without touching a single
        caller: routing some/all calls to Claude or Gemini, or to another
        Adiyan peer's spare compute via mesh/compute_share/ (already a
        real, working peer-exchange network - see run_inference/offload
        there) when this machine's own Ollama is loaded or a bigger model
        is needed than it can run. That decision belongs entirely inside
        this one method's body - every agent that calls agent.ask(prompt)
        today needs zero code changes on that day.

        stage/model/temperature: `stage` is a per-agent, per-pipeline-step
        name (e.g. 'craft_reply', 'classify_intent') - resolved through
        config_sdk.get_stage_config() so the actual model/temperature used
        become dashboard-editable later without a code change, seeded from
        the model/temperature given here the first time this stage is ever
        called. Two different `stage` names on the same agent get
        independently tunable configs; reusing 'default' for everything
        works too if you don't need that.

        schema: a pydantic BaseModel - if given, the response is parsed
        into it (structured output) and that instance is returned. If
        omitted, the model's plain text reply is returned as a str.
        Never raises for a bad response shape when schema is given -
        langchain's own with_structured_output() retries/repairs that
        internally; this method doesn't add a second layer of retry.

        community: the ENTIRE opt-in to compute_share's peer-sharing
        network. Whatever prompt you build IS what leaves this machine if
        this falls back to a peer - see mesh/compute_share/README.md's own
        trust-boundary section. This is deliberately not a boolean a
        careless `True` could flip by accident: pass the literal string
        'communitySearch' to opt in for this one call. Omitted (the
        default): local-only, always - a local failure or timeout raises
        normally, with no attempt to reach a peer, exactly as before this
        parameter existed. Only ever applies to plain-text calls
        (schema=None) - a peer's raw completion can't honor a structured-
        output contract the way local with_structured_output() does, so a
        schema call never falls back regardless of this parameter."""
        cfg = await config_sdk.get_stage_config(
            self.agent_id, stage, {'model': model, 'temperature': temperature},
        )
        try:
            return await asyncio.wait_for(self._ask_local(prompt, cfg, schema), timeout=LOCAL_ASK_TIMEOUT_SECONDS)
        except Exception:
            if community != COMMUNITY_SEARCH_MAGIC or schema is not None:
                raise
            return await self._ask_community(prompt, cfg['model'])

    async def _ask_local(self, prompt: str, cfg: Dict[str, Any], schema: Optional[Type[BaseModel]]) -> Any:
        llm = ChatOllama(model=cfg['model'], base_url=OLLAMA_URL, temperature=cfg['temperature'])
        if schema is not None:
            structured = llm.with_structured_output(schema)
            return await structured.ainvoke(prompt)
        result = await llm.ainvoke(prompt)
        return result.content

    async def _ask_community(self, prompt: str, model: str) -> str:
        # A fixed platform identity, not self.agent_id/self._tier - this
        # is the whole point of the 'compute_share_client' tier's own
        # description: offloading is a capability the platform grants by
        # construction, not something threaded through every agent's own
        # permission grant. Whichever agent called ask() never mints this
        # token itself and never sees compute_share exist.
        token = permissions.mint_token('adiyan_platform', 'compute_share_client')
        result = await _call_agent(COMPUTE_SHARE_URL, 'offload', {'prompt': prompt, 'model': model}, token=token)
        return result.get('completion')

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
