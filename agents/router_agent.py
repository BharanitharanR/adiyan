from core.base_agent import BaseAgent, AgentState
from typing import Dict, Any, Optional
import os
from pathlib import Path

# Use ~/.Adiyan for data
DATA_DIR = Path.home() / '.Adiyan'
DATA_DIR.mkdir(exist_ok=True)

class RouterAgent(BaseAgent):
    """Agent 3: Route to appropriate persona"""

    def __init__(self, config: Dict[str, Any] = None):
        tools = ['load_persona', 'get_routing_rules', 'determine_flow']
        super().__init__('RouterAgent', tools, config)
        self.personas = self._load_personas()

    async def execute(self, state: AgentState) -> AgentState:
        """Route to persona"""
        try:
            # Skip routing for registration/unregistration
            if state.is_registration or state.is_unregistration:
                state.persona = 'system'
                self.log_stage(f"Routing to SYSTEM for command handling")
                state.metadata['persona'] = 'system'
                return state

            # Skip if not whitelisted
            if not state.is_whitelisted:
                state.persona = 'none'
                state.error = 'Not whitelisted'
                self.log_stage(f"Not routing - user not whitelisted", 'warning')
                return state

            # Default persona for coaching
            persona = 'executive_coach'
            state.persona = persona
            state.metadata['persona'] = persona

            self.log_stage(f"✅ Routed to persona: {persona}")
            return state

        except Exception as e:
            return self.set_error(state, f"Routing failed: {str(e)}")

    def _load_personas(self) -> Dict[str, Dict[str, Any]]:
        """Load persona configurations"""
        return {
            'executive_coach': {
                'name': 'Executive Coach',
                'system_prompt': '''You are an Executive Coach specializing in logical thinking and decision-making.

COACHING RULES:
1. Respond warmly and personally (not as a consultant)
2. Provide 2-3 specific, actionable steps (not frameworks)
3. Ask ONE probing question at the end
4. Connect advice to their goals
5. Ignore generic frameworks - find novel insights'''
            },
            'system': {
                'name': 'System Handler',
                'system_prompt': 'You are a system assistant handling registration/unregistration.'
            }
        }

    def get_persona_prompt(self, persona: str) -> str:
        """Get system prompt for persona"""
        return self.personas.get(persona, {}).get('system_prompt', '')
