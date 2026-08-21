"""Journal Agent's own identity constants, plus the hardcoded Memory Agent
address it calls - registry idea deliberately parked, so this is a plain
constant, not a lookup."""
AGENT_ID = 'journal'
HOST = '127.0.0.1'
PORT = 8422
AGENT_URL = f'http://{HOST}:{PORT}'

MEMORY_AGENT_URL = 'http://127.0.0.1:8423'
