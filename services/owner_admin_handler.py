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
import json
import logging
from pathlib import Path
from typing import List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool

import config.database as db
from core.memory_index import get_memory_index

logger = logging.getLogger('OwnerAdminHandler')

# Same file agents/storage_agent.py writes to - read-only from here, never written.
INTERACTION_HISTORY_FILE = Path.home() / '.Adiyan' / 'interaction_history.jsonl'

# Appended to every reply this handler sends into the self-chat, and checked for on
# every INCOMING message before processing (see kb_ingestion_poller.py's _handle_message).
# Self-chat messages are always direction=outgoing (fromMe=true) whether they're the
# owner's own typed input or the bot's own reply - there is no other field that tells
# them apart. Without this tag, every reply the bot sends gets picked up on the next
# poll cycle and misread as a brand new command, which produced a real runaway
# self-conversation loop in testing (confirmed live - the bot kept re-answering its
# own previous answers with slightly reworded text).
ADMIN_REPLY_TAG = '[AdminAI]'

# 5 exchanges (user + reply pairs) of running context, bounded so the prompt doesn't
# grow unbounded over a long admin session.
MAX_HISTORY_MESSAGES = 10

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
    "guessing or inventing one.\n\n"
    "You have READ-ONLY access to client conversation history via get_recent_client_messages "
    "and search_client_messages. Use it only to answer the owner's direct questions about what "
    "was said - report back facts (quote or summarize what was actually said), never invent "
    "content, and never give coaching advice yourself based on it. There is no tool to edit or "
    "delete a client's conversation history, by design."
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

    @tool
    def get_recent_client_messages(name: str, limit: int = 5) -> list:
        """Read-only: the most recent messages exchanged with a client, newest first -
        each with timestamp, what they said, and what the coach (Adiyan) replied. For
        "what's my most recent conversation with X" / "show me X's last few messages".
        Use search_client_messages instead for "did X ever mention Y" style questions."""
        limit = max(1, min(limit, 20))
        matches = []
        try:
            with open(INTERACTION_HISTORY_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get('contact_name', '').lower() == name.lower():
                        matches.append(record)
        except FileNotFoundError:
            return {'error': 'No interaction history recorded yet'}

        if not matches:
            return {'error': f"No message history for '{name}'"}
        matches.sort(key=lambda r: r.get('timestamp', ''), reverse=True)
        return [
            {'timestamp': r.get('timestamp'), 'message': r.get('message'), 'response': r.get('response')}
            for r in matches[:limit]
        ]

    @tool
    def search_client_messages(name: str, query: str, limit: int = 5) -> list:
        """Read-only: semantic search over a specific client's conversation history for
        something they may have discussed - e.g. "did X ever mention their income" or
        "what has X said about their goals". Returns the most relevant past exchanges,
        not necessarily the most recent - use get_recent_client_messages for that."""
        memory = get_memory_index(control_plane.config.qdrant_url, control_plane.config.ollama_url)
        if not memory:
            return {'error': 'Memory index unavailable'}
        limit = max(1, min(limit, 10))
        results = memory.retrieve(query, contact_name=name, top_k=limit)
        return results or {'error': f"No relevant history found for '{name}'"}

    return [get_agent_config, update_agent_config, get_client, list_clients,
            add_client, update_client, remove_client, get_platform_stats,
            get_recent_client_messages, search_client_messages]


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
        # Running context for the admin conversation - "what's hermes' temperature" ->
        # "now set it to 0.3" needs to know "it" means hermes. In-memory only, one
        # instance per process (main.py constructs this once), bounded so an old
        # session doesn't grow the prompt forever. Separate from the per-client
        # conversation memory in core/memory_index.py - this is the owner's own admin
        # channel, never mixed with client-facing coaching context.
        self._history: List = []

    async def handle_text_message(self, chat_id: str, message_body: str) -> Optional[str]:
        """Returns the sent reply's own message id (or None on send failure) - the
        caller (KBIngestionPoller) must mark this id as already-processed immediately,
        or its own reply lands back in the self-chat on the next poll and gets
        misread as a new command (self-chat messages are always direction=outgoing,
        same as everything else here - there's no way to tell "the bot's own reply"
        from "a new user message" except by tracking sent ids ourselves)."""
        if not message_body or not message_body.strip():
            return None
        try:
            reply = await self._run_admin_agent(message_body)
        except Exception as e:
            logger.error(f"❌ Admin request failed: {e}", exc_info=True)
            reply = f"Couldn't process that: {e}"

        try:
            result = await self.openwa.send_message(chat_id, f"{reply}\n\n{ADMIN_REPLY_TAG}")
            return result.get('messageId')
        except Exception as e:
            logger.error(f"❌ Failed to send admin reply: {e}")
            return None

    async def _run_admin_agent(self, message_body: str) -> str:
        from langchain_ollama import ChatOllama
        from langgraph.prebuilt import create_react_agent

        model = ChatOllama(model=self.model, base_url=self.ollama_url, temperature=0.2)
        tools = _build_admin_tools(self.control_plane)
        agent = create_react_agent(model, tools)

        human = HumanMessage(content=message_body)
        messages = [SystemMessage(content=ADMIN_SYSTEM_PROMPT)] + self._history + [human]
        result = await asyncio.wait_for(agent.ainvoke({"messages": messages}), timeout=60)

        final = result["messages"][-1]
        if not isinstance(final, AIMessage) or not final.content:
            raise Exception("Admin agent produced no final answer")

        # Only the user's turn and the final answer are kept - not the intermediate
        # tool-call/tool-result messages the react loop produced getting there, which
        # would otherwise pollute future turns with stale tool-call artifacts.
        self._history.extend([human, AIMessage(content=final.content)])
        self._history = self._history[-MAX_HISTORY_MESSAGES:]
        return final.content.strip()
