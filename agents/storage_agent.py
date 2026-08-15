from core.base_agent import BaseAgent, AgentState
from core.memory_index import get_memory_index
from typing import Dict, Any
import json
from datetime import datetime
from pathlib import Path

# Use ~/.Adiyan for data
DATA_DIR = Path.home() / '.Adiyan'
DATA_DIR.mkdir(exist_ok=True)

class StorageAgent(BaseAgent):
    """Agent 6: Store interaction history in Qdrant (via LlamaIndex) and file"""

    def __init__(self, config: Dict[str, Any] = None):
        tools = ['store_in_qdrant', 'save_to_file', 'update_metadata']
        super().__init__('StorageAgent', tools, config)
        self.qdrant_url = config.get('qdrant_url', 'http://localhost:6339') if config else 'http://localhost:6339'
        self.ollama_url = config.get('ollama_url', 'http://localhost:11434') if config else 'http://localhost:11434'
        self.history_file = DATA_DIR / 'interaction_history.jsonl'

    async def execute(self, state: AgentState) -> AgentState:
        """Store interaction"""
        try:
            # Always save to file (most reliable)
            self._save_to_file(state)

            # Try to save to Qdrant (non-blocking)
            try:
                await self._save_to_qdrant(state)
            except Exception as e:
                self.log_stage(f"⚠️  Qdrant save failed: {str(e)}", 'warning')

            state.metadata['stored_at'] = datetime.now().isoformat()
            self.log_stage(f"✅ Interaction stored")
            return state

        except Exception as e:
            # Don't fail pipeline - storage is supplementary
            self.log_stage(f"⚠️  Storage warning: {str(e)}", 'warning')
            return state

    def _save_to_file(self, state: AgentState):
        """Save interaction to JSONL file"""
        record = {
            'timestamp': datetime.now().isoformat(),
            'message_id': state.message_id,
            'contact_name': state.contact_name,
            'lid': state.lid,
            'message': state.message_body,
            'response': state.llm_response or state.metadata.get('chunks', []),
            'is_registration': state.is_registration,
            'is_unregistration': state.is_unregistration,
            'persona': state.persona,
            'metadata': state.metadata
        }

        with open(self.history_file, 'a') as f:
            f.write(json.dumps(record) + '\n')

    async def _save_to_qdrant(self, state: AgentState):
        """Embed and store the interaction via LlamaIndex, for LLMAgent's later
        semantic recall of this contact's history."""
        if not state.llm_response:
            return

        memory = get_memory_index(self.qdrant_url, self.ollama_url)
        if not memory:
            return  # unavailable - StorageAgent's caller already treats this as non-fatal

        text = f"User: {state.message_body}\nCoach: {state.llm_response}"
        memory.insert(
            text=text,
            contact_name=state.contact_name,
            persona=state.persona,
            timestamp=datetime.now().isoformat(),
            message_id=state.message_id,
        )
