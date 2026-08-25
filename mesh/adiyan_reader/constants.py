"""AdiyanReader's own identity constants, plus the agents/services it calls."""
AGENT_ID = 'adiyan_reader'
HOST = '127.0.0.1'
PORT = 8429
AGENT_URL = f'http://{HOST}:{PORT}'

MEMORY_AGENT_URL = 'http://127.0.0.1:8423'
CRON_TRIGGER_URL = 'http://127.0.0.1:8421/mcp'

OLLAMA_URL = 'http://localhost:11434'
OPENWA_URL = 'http://localhost:2785'
OPENWA_SESSION_NAME = 'adiyan'
