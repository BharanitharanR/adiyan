from core.base_agent import BaseAgent, AgentState
from typing import Dict, Any, Optional
import json
from pathlib import Path

# Use ~/.Adiyan for data
DATA_DIR = Path.home() / '.Adiyan'
DATA_DIR.mkdir(exist_ok=True)

DEFAULT_PERSONAS = {
    'active_persona': 'executive_coach',
    'personas': {
        'executive_coach': {
            'name': 'Executive Coach',
            'system_prompt': (
                'You are an Executive Coach specializing in logical thinking and decision-making.\n\n'
                'COACHING RULES:\n'
                '1. Respond warmly and personally (not as a consultant)\n'
                '2. Provide 2-3 specific, actionable steps (not frameworks)\n'
                '3. Ask ONE probing question at the end\n'
                '4. Connect advice to their goals\n'
                '5. Ignore generic frameworks - find novel insights'
            ),
        },
    },
}

SYSTEM_PERSONA = {
    'name': 'System Handler',
    'system_prompt': 'You are a system assistant handling registration/unregistration.',
}


class RouterAgent(BaseAgent):
    """Agent 3: Route to the configured persona.

    Adiyan is meant to be one coach's digital twin, not a router across many
    simultaneous personalities - so this resolves to a single, deployment-wide
    `active_persona` rather than picking a different one per message or per
    contact. What IS pluggable: personas.json is a plain editable file (not
    hardcoded Python), so a coach can define their own system prompt, or the
    file can hold several persona definitions and swap which one is active,
    without touching code.
    """

    def __init__(self, config: Dict[str, Any] = None):
        tools = ['load_persona', 'get_routing_rules', 'determine_flow']
        super().__init__('RouterAgent', tools, config)
        self.personas_file = DATA_DIR / 'personas.json'
        self._personas_mtime = None
        self._active_persona = DEFAULT_PERSONAS['active_persona']
        self._personas = dict(DEFAULT_PERSONAS['personas'])
        self._load_personas()

    async def execute(self, state: AgentState) -> AgentState:
        """Route to persona"""
        try:
            self._load_personas()  # cheap mtime check, picks up edits without a restart

            # Skip routing for registration/unregistration
            if state.is_registration or state.is_unregistration:
                state.persona = 'system'
                state.metadata['persona'] = 'system'
                state.metadata['system_prompt'] = SYSTEM_PERSONA['system_prompt']
                self.log_stage(f"Routing to SYSTEM for command handling")
                return state

            # Skip if not whitelisted
            if not state.is_whitelisted:
                state.persona = 'none'
                state.error = 'Not whitelisted'
                self.log_stage(f"Not routing - user not whitelisted", 'warning')
                return state

            persona_id = self._active_persona
            persona = self._personas.get(persona_id)
            if not persona:
                return self.set_error(state, f"Configured active_persona '{persona_id}' not found in personas.json")

            state.persona = persona_id
            state.metadata['persona'] = persona_id
            state.metadata['system_prompt'] = persona.get('system_prompt', '')

            self.log_stage(f"✅ Routed to persona: {persona_id}")
            return state

        except Exception as e:
            return self.set_error(state, f"Routing failed: {str(e)}")

    def _load_personas(self):
        """Load personas.json, creating it with defaults if missing."""
        if not self.personas_file.exists():
            with open(self.personas_file, 'w') as f:
                json.dump(DEFAULT_PERSONAS, f, indent=2)
            self._active_persona = DEFAULT_PERSONAS['active_persona']
            self._personas = dict(DEFAULT_PERSONAS['personas'])
            self._personas_mtime = self.personas_file.stat().st_mtime
            return

        try:
            mtime = self.personas_file.stat().st_mtime
            if mtime == self._personas_mtime:
                return  # unchanged since last read

            with open(self.personas_file, 'r') as f:
                data = json.load(f)

            self._active_persona = data.get('active_persona', DEFAULT_PERSONAS['active_persona'])
            self._personas = data.get('personas', DEFAULT_PERSONAS['personas'])
            self._personas_mtime = mtime
        except Exception as e:
            self.logger.warning(f"Failed to load personas.json, keeping previous personas: {e}")

    def get_persona_prompt(self, persona: str) -> str:
        """Get system prompt for a persona (used outside the pipeline, e.g. by the control panel)."""
        if persona == 'system':
            return SYSTEM_PERSONA['system_prompt']
        return self._personas.get(persona, {}).get('system_prompt', '')
