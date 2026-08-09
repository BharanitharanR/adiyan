from core.base_agent import BaseAgent, AgentState
from typing import Dict, Any, List

class SynthesizerAgent(BaseAgent):
    """Agent 5: Synthesize and format LLM response"""

    def __init__(self, config: Dict[str, Any] = None):
        tools = ['format_response', 'split_chunks', 'apply_persona_rules']
        super().__init__('SynthesizerAgent', tools, config)
        self.max_length = config.get('max_response_length', 4096) if config else 4096

    async def execute(self, state: AgentState) -> AgentState:
        """Format and synthesize response"""
        try:
            if not state.llm_response:
                self.log_stage("No LLM response to synthesize", 'warning')
                state.metadata['chunks'] = []
                return state

            # Format response
            formatted = self._format_response(state.llm_response)

            # Split into chunks if needed
            chunks = self._split_into_chunks(formatted)
            state.metadata['chunks'] = chunks
            state.metadata['chunk_count'] = len(chunks)

            self.log_stage(f"✅ Synthesized response into {len(chunks)} chunk(s)")
            return state

        except Exception as e:
            return self.set_error(state, f"Synthesis failed: {str(e)}")

    def _format_response(self, response: str) -> str:
        """Format response text"""
        # Clean up whitespace
        text = response.strip()
        # Ensure proper line breaks
        return text

    def _split_into_chunks(self, text: str) -> List[str]:
        """Split response into WhatsApp-friendly chunks"""
        if len(text) <= self.max_length:
            return [text]

        chunks = []
        current_chunk = ""

        # Split by sentences to avoid breaking mid-sentence
        sentences = text.split('. ')

        for sentence in sentences:
            if len(current_chunk) + len(sentence) + 2 > self.max_length:
                if current_chunk:
                    chunks.append(current_chunk.rstrip())
                current_chunk = sentence
            else:
                if current_chunk:
                    current_chunk += ". " + sentence
                else:
                    current_chunk = sentence

        if current_chunk:
            chunks.append(current_chunk.rstrip())

        return chunks
