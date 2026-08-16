from core.base_agent import BaseAgent, AgentState
from typing import Dict, Any
import time
import config.database as db

class ValidatorAgent(BaseAgent):
    """Agent 2: Validate user registration and whitelist status"""

    def __init__(self, config: Dict[str, Any] = None):
        tools = ['check_whitelist', 'validate_format', 'check_registration']
        super().__init__('ValidatorAgent', tools, config)

    async def execute(self, state: AgentState) -> AgentState:
        """Validate user"""
        try:
            # Validate input
            if not state.contact_name:
                return self.set_error(state, "contact_name is required")

            # Handle registration
            if state.is_registration:
                try:
                    self.add_to_whitelist(state.contact_name, lid=state.lid)
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
                db.touch_last_active(state.contact_name)
                self.log_stage(f"✅ {state.contact_name} is whitelisted")

                # Is this message answering a scheduled job we sent (services/cron_scheduler.py)?
                # If so it's captured as job_data, not routed through normal coaching -
                # same shape as the is_registration/is_unregistration short-circuit above.
                pending = db.get_pending_job_response(state.contact_name)
                if pending:
                    today = time.strftime('%Y-%m-%d')
                    db.write_job_data(
                        pending['job_id'], key=f"response:{today}",
                        value=state.message_body, contact_name=state.contact_name,
                    )
                    db.clear_pending_job_response(pending['id'])
                    state.is_job_response = True
                    self.log_stage(f"📝 Captured response for job {pending['job_id']}")
            else:
                self.log_stage(f"⚠️  {state.contact_name} not whitelisted - ignoring message", 'warning')

            state.metadata['validated_at'] = True
            return state

        except Exception as e:
            return self.set_error(state, f"Validation failed: {str(e)}")

    def _check_whitelist(self, contact_name: str) -> bool:
        """Check if contact is whitelisted"""
        return db.is_whitelisted(contact_name)

    def add_to_whitelist(self, contact_name: str, lid: str = None):
        """Add contact to whitelist (used by registration, and by the WhatsApp admin
        handler for coach-initiated onboarding)"""
        db.add_client(contact_name, lid=lid)
        self.log_stage(f"✅ Added {contact_name} to whitelist")

    def remove_from_whitelist(self, contact_name: str):
        """Remove contact from whitelist (used by unregistration)"""
        if db.remove_client(contact_name):
            self.log_stage(f"✅ Removed {contact_name} from whitelist")
