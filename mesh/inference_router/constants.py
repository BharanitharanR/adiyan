"""Inference Router's own identity constants.

Platform infrastructure, not a reference agent for others to copy - the
one thing standing between mesh/lib/agent_sdk.py's ask() and both actual
LLM backends (local Ollama, a compute_share peer). Every plain-text
ask() call routes through here so the "am I backed up" decision lives
in one place, not duplicated inline in every calling agent's process."""
AGENT_ID = 'inference_router'
HOST = '127.0.0.1'
PORT = 8441
AGENT_URL = f'http://{HOST}:{PORT}'
OLLAMA_URL = 'http://localhost:11434'
COMPUTE_SHARE_URL = 'http://127.0.0.1:8460'
