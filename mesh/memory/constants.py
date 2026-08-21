"""Memory Agent's own identity constants - same split-out-to-avoid-cycles
reasoning as mesh/scheduler/constants.py."""
AGENT_ID = 'memory'
HOST = '127.0.0.1'
PORT = 8423
AGENT_URL = f'http://{HOST}:{PORT}'

# Adiyan's own bundled Qdrant/Ollama - see config/control_plane.py's defaults
# (qdrant_url uses 6339, not Qdrant's usual 6333, to avoid colliding with any
# other instance already on the machine) and core/memory_index.py.
QDRANT_URL = 'http://localhost:6339'
OLLAMA_URL = 'http://localhost:11434'
