"""
WhatsApp Admin Channel
The owner's self-chat isn't only for PDF uploads (services/kb_ingestion_poller.py) - a
plain text message there is treated as a natural-language admin request: agent config
("turn off momus"), client management ("add a client named Priya, number 9876543210"),
or platform stats ("how many active users this week").

Natural language in, but every mutation stays strict: the LLM's only job is to call a
tightly-typed tool (below) - never to freeform-edit the db. Each tool validates its own
inputs (agent id against the real 13, field against the real allowed set, client name
against what's on file) before touching config/control_plane.py or config/database.py.

Wired in by services/kb_ingestion_poller.py's poll loop (not a second independent
poller) - both PDF uploads and admin text share one fetch of the self-chat per cycle,
so this doesn't add a second consumer of OpenWA's rate-limited API budget.
"""
import asyncio
import logging
from typing import List

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool

import config.database as db

logger = logging.getLogger('OwnerAdminHandler')

ALL_AGENT_IDS = {
    'parser', 'validator', 'router', 'llm', 'synthesizer', 'storage', 'publisher',
    'hermes', 'prometheus', 'pythia', 'hephaestus', 'calliope', 'momus',
}
AGENT_CONFIG_FIELDS = {'enabled', 'model', 'temperature', 'timeout', 'prompt_template'}
CLIENT_FIELDS = {'notes', 'phone', 'tags'}

ADMIN_SYSTEM_PROMPT = (
    "You are Adiyan's admin assistant, used only by the platform owner through their own "
    "WhatsApp self-chat - not a client-facing conversation. Use the tools to look things up "
    "or make changes; never guess at data you haven't fetched. Keep your final reply short "
    "and factual, no coaching tone, no filler - a plain confirmation or a compact fact. If a "
    "request doesn't map to a real agent, field, or client, say so plainly rather than "
    "guessing or inventing one."
)


def _build_admin_tools(control_plane) -> List:
    @tool
    def get_agent_config(agent_id: str) -> dict:
        """Get one agent's current config: enabled, model, temperature, timeout. agent_id
        must be one of: parser, validator, router, llm, synthesizer, storage, publisher,
        hermes, prometheus, pythia, hephaestus, calliope, momus."""
        if agent_id not in ALL_AGENT_IDS:
            return {'error': f"Unknown agent '{agent_id}'. Valid: {sorted(ALL_AGENT_IDS)}"}
        cfg = control_plane.get_agent_config(agent_id)
        if not cfg:
            return {'error': f"Agent '{agent_id}' has no config"}
        return {
            'id': agent_id, 'name': cfg.name, 'enabled': cfg.enabled,
            'model': cfg.model, 'temperature': cfg.temperature, 'timeout': cfg.timeout,
        }

    @tool
    def update_agent_config(agent_id: str, field: str, value: str) -> dict:
        """Update one field of an agent's config. field must be one of: enabled, model,
        temperature, timeout, prompt_template. For 'enabled' pass 'true'/'false'. Applies
        immediately, live - no restart needed."""
        if agent_id not in ALL_AGENT_IDS:
            return {'error': f"Unknown agent '{agent_id}'. Valid: {sorted(ALL_AGENT_IDS)}"}
        if field not in AGENT_CONFIG_FIELDS:
            return {'error': f"Unknown field '{field}'. Valid: {sorted(AGENT_CONFIG_FIELDS)}"}

        if field == 'enabled':
            parsed = value.strip().lower() in ('true', 'on', 'yes', '1')
        elif field == 'temperature':
            try:
                parsed = float(value)
            except ValueError:
                return {'error': f"temperature must be a number, got {value!r}"}
        elif field == 'timeout':
            try:
                parsed = int(value)
            except ValueError:
                return {'error': f"timeout must be an integer, got {value!r}"}
        else:
            parsed = value

        ok = control_plane.update_agent_config(agent_id, **{field: parsed})
        return {'success': ok, 'agent_id': agent_id, 'field': field, 'value': parsed}

    @tool
    def get_client(name: str) -> dict:
        """Look up a registered client's details by contact name."""
        client = db.get_client(name)
        return client or {'error': f"No client named '{name}'"}

    @tool
    def list_clients(only_active: bool = False) -> list:
        """List registered clients. only_active=true limits to clients active in the last
        7 days."""
        return db.list_clients(active_only=only_active)

    @tool
    def add_client(name: str, phone: str = '') -> dict:
        """Register a new client (coach-initiated onboarding, no self-registration
        message needed). phone is optional."""
        if not name or not name.strip():
            return {'error': 'name is required'}
        db.add_client(name.strip(), phone=phone.strip() or None)
        return {'success': True, 'name': name.strip()}

    @tool
    def update_client(name: str, field: str, value: str) -> dict:
        """Update a registered client's notes, phone, or tags. field must be one of:
        notes, phone, tags."""
        if field not in CLIENT_FIELDS:
            return {'error': f"Unknown field '{field}'. Valid: {sorted(CLIENT_FIELDS)}"}
        ok = db.update_client(name, **{field: value})
        return {'success': ok, 'name': name} if ok else {'error': f"No client named '{name}'"}

    @tool
    def remove_client(name: str) -> dict:
        """Unregister/remove a client by name."""
        ok = db.remove_client(name)
        return {'success': ok} if ok else {'error': f"No client named '{name}'"}

    @tool
    def get_platform_stats() -> dict:
        """Platform stats: total registered clients, clients active in the last 7 days
        (and their names), documents processed into the knowledge base, total chunks."""
        return db.get_platform_stats()

    return [get_agent_config, update_agent_config, get_client, list_clients,
            add_client, update_client, remove_client, get_platform_stats]


class OwnerAdminHandler:
    """One instance per process, shared by kb_ingestion_poller.py for every non-document
    self-chat message."""

    def __init__(self, control_plane, openwa_service, ollama_url: str = None):
        self.control_plane = control_plane
        self.openwa = openwa_service
        self.ollama_url = ollama_url or control_plane.config.ollama_url
        # Reuses the 'llm' agent's configured model rather than inventing a separate
        # admin-specific one - one less thing to independently configure.
        llm_cfg = control_plane.get_agent_config('llm')
        self.model = llm_cfg.model if llm_cfg and llm_cfg.model else 'qwen3:8b-16k'

    async def handle_text_message(self, chat_id: str, message_body: str):
        if not message_body or not message_body.strip():
            return
        try:
            reply = await self._run_admin_agent(message_body)
        except Exception as e:
            logger.error(f"❌ Admin request failed: {e}", exc_info=True)
            reply = f"Couldn't process that: {e}"

        try:
            await self.openwa.send_message(chat_id, reply)
        except Exception as e:
            logger.error(f"❌ Failed to send admin reply: {e}")

    async def _run_admin_agent(self, message_body: str) -> str:
        from langchain_ollama import ChatOllama
        from langgraph.prebuilt import create_react_agent

        model = ChatOllama(model=self.model, base_url=self.ollama_url, temperature=0.2)
        tools = _build_admin_tools(self.control_plane)
        agent = create_react_agent(model, tools)

        messages = [SystemMessage(content=ADMIN_SYSTEM_PROMPT), HumanMessage(content=message_body)]
        result = await asyncio.wait_for(agent.ainvoke({"messages": messages}), timeout=60)

        final = result["messages"][-1]
        if not isinstance(final, AIMessage) or not final.content:
            raise Exception("Admin agent produced no final answer")
        return final.content.strip()
