from core.base_agent import BaseAgent, AgentState
from typing import Dict, Any, Optional
from pathlib import Path
import json
import re

DATA_DIR = Path.home() / '.Adiyan'
DATA_DIR.mkdir(exist_ok=True)

DEFAULT_TRIGGERS = {
    'register': 'register me',
    'unregister': 'unregister me'
}

class ParserAgent(BaseAgent):
    """Agent 1: Parse incoming WhatsApp message"""

    def __init__(self, config: Dict[str, Any] = None):
        tools = ['extract_message', 'get_contact_info', 'parse_lid', 'detect_command']
        super().__init__('ParserAgent', tools, config)
        self.triggers_file = DATA_DIR / 'trigger_words.json'
        self._triggers_mtime = None
        self._triggers = DEFAULT_TRIGGERS.copy()
        self._load_triggers()

    async def execute(self, state: AgentState) -> AgentState:
        """Parse message and extract components"""
        try:
            # Skip processing if not a user message (prevent reprocessing responses)
            if state.metadata.get('is_user_message') is False:
                self.log_stage("⏭️  Skipping system message (not from user)")
                return state

            # Validate input
            if not state.contact_name or not isinstance(state.contact_name, str):
                return self.set_error(state, "Invalid contact_name")
            if not state.message_body or not isinstance(state.message_body, str):
                return self.set_error(state, "Invalid message_body")

            self.log_stage(f"Parsing message from {state.contact_name}")

            # Check for special commands
            command = self._detect_command(state.message_body)
            if command == 'register':
                state.is_registration = True
                self.log_stage("Detected REGISTER command")
            elif command == 'unregister':
                state.is_unregistration = True
                self.log_stage("Detected UNREGISTER command")

            # Store parse metadata
            state.metadata['parsed_at'] = True
            state.metadata['message_length'] = len(state.message_body)
            state.metadata['command_detected'] = command

            self.log_stage(f"✅ Message parsed (length: {len(state.message_body)}, command: {command})")
            return state

        except Exception as e:
            return self.set_error(state, f"Parse failed: {str(e)}")

    def _load_triggers(self):
        """Load trigger words from file, creating it with defaults if missing"""
        if not self.triggers_file.exists():
            with open(self.triggers_file, 'w') as f:
                json.dump(DEFAULT_TRIGGERS, f, indent=2)
            self._triggers = DEFAULT_TRIGGERS.copy()
            self._triggers_mtime = self.triggers_file.stat().st_mtime
            return

        try:
            mtime = self.triggers_file.stat().st_mtime
            if mtime == self._triggers_mtime:
                return  # unchanged since last read

            with open(self.triggers_file, 'r') as f:
                data = json.load(f)

            self._triggers = {
                'register': str(data.get('register', DEFAULT_TRIGGERS['register'])).lower(),
                'unregister': str(data.get('unregister', DEFAULT_TRIGGERS['unregister'])).lower()
            }
            self._triggers_mtime = mtime
        except Exception as e:
            self.logger.warning(f"Failed to load trigger_words.json, keeping previous triggers: {e}")

    def _detect_command(self, message: str) -> Optional[str]:
        """Detect registration/unregistration commands using file-configured trigger words.

        Word-boundary matched, not plain substring: "register me" must not match inside
        "unregister me" (it would with a bare `in` check, since "unregister" contains
        "register" with no gap). Checking unregister first is an extra safety net in case
        a future trigger phrase is itself a true word-bounded substring of another.
        """
        self._load_triggers()  # cheap mtime check, picks up edits without a restart

        msg_lower = message.lower()

        unregister_pattern = r'\b' + re.escape(self._triggers['unregister']) + r'\b'
        register_pattern = r'\b' + re.escape(self._triggers['register']) + r'\b'

        if re.search(unregister_pattern, msg_lower):
            return 'unregister'
        elif re.search(register_pattern, msg_lower):
            return 'register'

        return None
