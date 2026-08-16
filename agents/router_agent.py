from core.base_agent import BaseAgent, AgentState
from typing import Dict, Any
import config.database as db

SYSTEM_PERSONA_PROMPT = db.SYSTEM_PERSONA_PROMPT


class RouterAgent(BaseAgent):
    """Agent 3: Route to the configured persona.

    Adiyan is meant to be one coach's digital twin, not a router across many
    simultaneous personalities - so this resolves to a single, deployment-wide
    active persona rather than picking a different one per message or per contact.
    What IS pluggable: personas live in the db (config/database.py's `personas`
    table), editable from the dashboard or the WhatsApp admin channel, not
    hardcoded Python - a coach can define their own system prompt, hold several
    persona definitions, and swap which one is active, without touching code.
    """

    def __init__(self, config: Dict[str, Any] = None):
        tools = ['load_persona', 'get_routing_rules', 'determine_flow']
        super().__init__('RouterAgent', tools, config)

    async def execute(self, state: AgentState) -> AgentState:
        """Route to persona"""
        try:
            # Skip routing for registration/unregistration/job-response acknowledgment
            if state.is_registration or state.is_unregistration or state.is_job_response:
                state.persona = 'system'
                state.metadata['persona'] = 'system'
                state.metadata['system_prompt'] = SYSTEM_PERSONA_PROMPT
                self.log_stage(f"Routing to SYSTEM for command handling")
                return state

            # Skip if not whitelisted
            if not state.is_whitelisted:
                state.persona = 'none'
                state.error = 'Not whitelisted'
                self.log_stage(f"Not routing - user not whitelisted", 'warning')
                return state

            persona_id = db.get_active_persona_id()
            persona = db.get_personas().get(persona_id) if persona_id else None
            if not persona:
                return self.set_error(state, f"No active persona configured")

            state.persona = persona_id
            state.metadata['persona'] = persona_id
            state.metadata['system_prompt'] = persona.get('system_prompt', '')

            self.log_stage(f"✅ Routed to persona: {persona_id}")
            return state

        except Exception as e:
            return self.set_error(state, f"Routing failed: {str(e)}")

    def get_persona_prompt(self, persona: str) -> str:
        """Get system prompt for a persona (used outside the pipeline, e.g. by the control panel)."""
        if persona == 'system':
            return SYSTEM_PERSONA_PROMPT
        return db.get_personas().get(persona, {}).get('system_prompt', '')
