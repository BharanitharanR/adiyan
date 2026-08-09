from core.base_agent import BaseAgent, AgentState
from typing import Dict, Any
import os
from pathlib import Path

# Use ~/.Adiyan for data
DATA_DIR = Path.home() / '.Adiyan'
DATA_DIR.mkdir(exist_ok=True)

class ValidatorAgent(BaseAgent):
    """Agent 2: Validate user registration and whitelist status"""

    def __init__(self, config: Dict[str, Any] = None):
        tools = ['check_whitelist', 'validate_format', 'check_registration']
        super().__init__('ValidatorAgent', tools, config)
        self.whitelist_file = DATA_DIR / 'whitelist.txt'
        self.whitelist = self._load_whitelist()

    async def execute(self, state: AgentState) -> AgentState:
        """Validate user"""
        try:
            # Validate input
            if not state.contact_name:
                return self.set_error(state, "contact_name is required")

            # Handle registration
            if state.is_registration:
                try:
                    self.add_to_whitelist(state.contact_name)
                    state.is_whitelisted = True
                    state.metadata['action'] = 'registered'
                    self.log_stage(f"✅ Registered {state.contact_name}")
                except Exception as e:
                    return self.set_error(state, f"Registration failed: {str(e)}")
                return state

            # Handle unregistration
            if state.is_unregistration:
                try:
                    self.remove_from_whitelist(state.contact_name)
                    state.is_whitelisted = False
                    state.metadata['action'] = 'unregistered'
                    self.log_stage(f"✅ Unregistered {state.contact_name}")
                except Exception as e:
                    return self.set_error(state, f"Unregistration failed: {str(e)}")
                return state

            # Check whitelist
            is_whitelisted = self._check_whitelist(state.contact_name)
            state.is_whitelisted = is_whitelisted

            if is_whitelisted:
                self.log_stage(f"✅ {state.contact_name} is whitelisted")
            else:
                self.log_stage(f"⚠️  {state.contact_name} not whitelisted - ignoring message", 'warning')

            state.metadata['validated_at'] = True
            return state

        except Exception as e:
            return self.set_error(state, f"Validation failed: {str(e)}")

    def _load_whitelist(self) -> set:
        """Load whitelist from file"""
        if self.whitelist_file.exists():
            with open(self.whitelist_file, 'r') as f:
                return set(line.strip() for line in f if line.strip())
        return set()

    def _check_whitelist(self, contact_name: str) -> bool:
        """Check if contact is whitelisted"""
        return contact_name in self.whitelist

    def add_to_whitelist(self, contact_name: str):
        """Add contact to whitelist (used by registration)"""
        self.whitelist.add(contact_name)
        with open(self.whitelist_file, 'a') as f:
            f.write(f"{contact_name}\n")
        self.log_stage(f"✅ Added {contact_name} to whitelist")

    def remove_from_whitelist(self, contact_name: str):
        """Remove contact from whitelist (used by unregistration)"""
        if contact_name in self.whitelist:
            self.whitelist.discard(contact_name)
            # Rewrite file without the contact
            with open(self.whitelist_file, 'w') as f:
                for name in sorted(self.whitelist):
                    f.write(f"{name}\n")
            self.log_stage(f"✅ Removed {contact_name} from whitelist")
