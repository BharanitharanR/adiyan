"""Analysis Agent's own identity constants, plus the hardcoded Memory Agent
address it calls - same fixed-dependency pattern mesh/journal/constants.py
already uses for its own call to Memory Agent (a known, single, fixed
dependency doesn't need registry-based resolution the way Orchestrator's
open-ended "which agent handles this" routing does)."""
AGENT_ID = 'analysis'
HOST = '127.0.0.1'
PORT = 8427
AGENT_URL = f'http://{HOST}:{PORT}'

MEMORY_AGENT_URL = 'http://127.0.0.1:8423'
