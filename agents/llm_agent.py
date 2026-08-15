from core.base_agent import BaseAgent, AgentState
from core.memory_index import get_memory_index
from typing import Dict, Any, List, Optional
import requests
import time
import asyncio

from langchain_core.tools import BaseTool
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

MEMORY_TOP_K = 3
MEMORY_SNIPPET_MAX_CHARS = 500
KB_TOP_K = 4
KB_SNIPPET_MAX_CHARS = 800

class LLMAgent(BaseAgent):
    """Agent 4: Call LLM (Ollama) for coaching response"""

    def __init__(self, config: Dict[str, Any] = None, agent_config = None, mcp_tools: Optional[List[BaseTool]] = None):
        tools = ['call_ollama', 'get_context', 'apply_system_prompt']
        super().__init__('LLMAgent', tools, config)
        self.ollama_url = config.get('ollama_url', 'http://localhost:11434') if config else 'http://localhost:11434'
        self.qdrant_url = config.get('qdrant_url', 'http://localhost:6339') if config else 'http://localhost:6339'

        # Use agent-specific config if available, otherwise use default
        if agent_config:
            self.model = agent_config.model
            self.temperature = agent_config.temperature
            self.timeout = agent_config.timeout
        else:
            self.model = config.get('model', 'qwen3:8b-16k') if config else 'qwen3:8b-16k'
            self.temperature = config.get('temperature', 0.7) if config else 0.7
            self.timeout = config.get('timeout', 180) if config else 180

        self.mcp_tools = mcp_tools or []

    def _build_react_agent(self):
        """Build a ReAct tool-calling loop (LLM <-> ToolNode) over the bound MCP tools.

        Built fresh on every call, not cached on self - langchain-ollama's ChatOllama
        holds a persistent ollama.AsyncClient, created the first time it's actually
        used, bound to whichever asyncio event loop is running at that moment. Each
        message is processed in its own fresh loop (asyncio.run() per message on the
        RabbitMQ consumer thread), so a cached react agent would work for exactly one
        message and then fail every one after with "Event loop is closed" - the same
        cross-loop bug services/openwa_service.py had, same fix shape: don't cache
        the client across loop lifetimes, rebuild per call. Construction itself does
        no I/O, so this costs object creation, not a network round trip.
        """
        from langchain_ollama import ChatOllama
        from langgraph.prebuilt import create_react_agent

        model = ChatOllama(
            model=self.model,
            base_url=self.ollama_url,
            temperature=self.temperature,
        )
        return create_react_agent(model, self.mcp_tools)

    async def execute(self, state: AgentState) -> AgentState:
        """Call LLM for response"""
        try:
            # Skip LLM for registration/unregistration (handled by system)
            if state.is_registration:
                state.llm_response = f"Registration request received from {state.contact_name}"
                self.log_stage(f"✅ Registration handler response ready")
                return state

            if state.is_unregistration:
                state.llm_response = f"Unregistration request received from {state.contact_name}"
                self.log_stage(f"✅ Unregistration handler response ready")
                return state

            # Skip if not whitelisted
            if not state.is_whitelisted:
                state.llm_response = None
                self.log_stage(f"Skipping LLM - not whitelisted", 'warning')
                return state

            # Call Ollama (tool-calling loop when MCP tools are bound, plain call otherwise)
            self.log_stage(f"Calling Ollama ({self.model})...")
            start_time = time.time()

            system_prompt = self._with_recalled_context(
                state.metadata.get('system_prompt', ''),
                query=state.message_body,
                contact_name=state.contact_name,
            )
            used_tools = False
            if self.mcp_tools:
                try:
                    response = await self._call_react_agent(
                        prompt=state.message_body,
                        system_prompt=system_prompt,
                    )
                    used_tools = True
                except Exception as e:
                    self.log_stage(f"⚠️  Tool-calling loop failed, falling back to plain call: {e}", 'warning')
                    response = await self._call_ollama(state.message_body, system_prompt)
            else:
                response = await self._call_ollama(state.message_body, system_prompt)

            elapsed = time.time() - start_time
            state.llm_response = response
            state.metadata['llm_time'] = elapsed
            state.metadata['model'] = self.model
            state.metadata['used_tools'] = used_tools

            self.log_stage(f"✅ LLM response ready ({elapsed:.1f}s, tools={used_tools})")
            return state

        except Exception as e:
            return self.set_error(state, f"LLM call failed: {str(e)}")

    def _with_recalled_context(self, system_prompt: str, query: str, contact_name: str) -> str:
        """Prepend relevant context before the model answers: the coach's knowledge base
        first (the coach's own authored material - source of truth), then this contact's
        past conversations. Best-effort throughout: any failure (no memory index, empty
        results, embedding call down) just skips that section rather than failing the turn."""
        memory = get_memory_index(self.qdrant_url, self.ollama_url)
        if not memory:
            return system_prompt

        sections = []

        try:
            kb_snippets = memory.retrieve_knowledge_base(query, top_k=KB_TOP_K)
            if kb_snippets:
                trimmed = [s[:KB_SNIPPET_MAX_CHARS] for s in kb_snippets]
                sections.append(
                    "Relevant material from the coach's knowledge base (treat as authoritative):\n"
                    + "\n\n".join(f"- {s}" for s in trimmed)
                )
        except Exception as e:
            self.log_stage(f"⚠️  Knowledge base recall skipped: {e}", 'warning')

        try:
            memory_snippets = memory.retrieve(query, contact_name=contact_name, top_k=MEMORY_TOP_K)
            if memory_snippets:
                trimmed = [s[:MEMORY_SNIPPET_MAX_CHARS] for s in memory_snippets]
                sections.append(
                    "Relevant past conversations with this person:\n"
                    + "\n\n".join(f"- {s}" for s in trimmed)
                )
        except Exception as e:
            self.log_stage(f"⚠️  Memory recall skipped: {e}", 'warning')

        if not sections:
            return system_prompt
        return system_prompt + "\n\n" + "\n\n".join(sections)

    async def _call_react_agent(self, prompt: str, system_prompt: str) -> str:
        """Run the bound tools through a ReAct loop: model requests a tool call,
        the tool runs, the result feeds back to the model, repeat until it answers."""
        react_agent = self._build_react_agent()

        messages = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))
        messages.append(HumanMessage(content=prompt))

        result = await asyncio.wait_for(
            react_agent.ainvoke({"messages": messages}),
            timeout=self.timeout,
        )

        final = result["messages"][-1]
        if not isinstance(final, AIMessage) or not final.content:
            raise Exception("Tool-calling loop finished without a final answer")
        return final.content.strip()

    async def _call_ollama(self, prompt: str, system_prompt: str) -> str:
        """Make HTTP request to Ollama"""
        try:
            response = requests.post(
                f"{self.ollama_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "system": system_prompt,
                    "stream": False,
                    "temperature": self.temperature,
                    "top_p": 0.95
                },
                timeout=self.timeout
            )

            if response.status_code != 200:
                # Ollama's error body (e.g. "requires more system memory than is available") is the
                # actual diagnostic - discarding it down to just the status code hides exactly the
                # detail needed to tell a memory-ceiling failure apart from anything else.
                detail = response.text.strip()
                raise Exception(f"Ollama returned {response.status_code}: {detail}")

            data = response.json()
            return data.get('response', '').strip()

        except requests.exceptions.ConnectionError:
            raise Exception("Cannot connect to Ollama - is it running?")
        except Exception as e:
            raise Exception(str(e))
