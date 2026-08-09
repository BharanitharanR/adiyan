from core.base_agent import BaseAgent, AgentState
from typing import Dict, Any
import requests
import time

class LLMAgent(BaseAgent):
    """Agent 4: Call LLM (Ollama) for coaching response"""

    def __init__(self, config: Dict[str, Any] = None, agent_config = None):
        tools = ['call_ollama', 'get_context', 'apply_system_prompt']
        super().__init__('LLMAgent', tools, config)
        self.ollama_url = config.get('ollama_url', 'http://localhost:11434') if config else 'http://localhost:11434'

        # Use agent-specific config if available, otherwise use default
        if agent_config:
            self.model = agent_config.model
            self.temperature = agent_config.temperature
            self.timeout = agent_config.timeout
        else:
            self.model = config.get('model', 'qwen3:8b-16k') if config else 'qwen3:8b-16k'
            self.temperature = config.get('temperature', 0.7) if config else 0.7
            self.timeout = config.get('timeout', 180) if config else 180

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

            # Call Ollama
            self.log_stage(f"Calling Ollama ({self.model})...")
            start_time = time.time()

            response = await self._call_ollama(
                prompt=state.message_body,
                system_prompt=state.metadata.get('system_prompt', '')
            )

            elapsed = time.time() - start_time
            state.llm_response = response
            state.metadata['llm_time'] = elapsed
            state.metadata['model'] = self.model

            self.log_stage(f"✅ LLM response ready ({elapsed:.1f}s)")
            return state

        except Exception as e:
            return self.set_error(state, f"LLM call failed: {str(e)}")

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
                raise Exception(f"Ollama returned {response.status_code}")

            data = response.json()
            return data.get('response', '').strip()

        except requests.exceptions.ConnectionError:
            raise Exception("Cannot connect to Ollama - is it running?")
        except Exception as e:
            raise Exception(str(e))
