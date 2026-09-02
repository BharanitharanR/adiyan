"""p2p's identity constants - same pattern as every other agent's
constants.py (see mesh/compute_share/constants.py for the closest
sibling: this agent replaces compute_share's role as Inference Router's
peer-offload backend, so it mirrors that file's shape deliberately).
"""
import os

AGENT_ID = 'p2p'

# This agent's own A2A server - what inference_router calls via a real
# skill (mesh/p2p/skills/dispatch.py), not a bare Python import.
HOST = os.environ.get('P2P_HOST', '127.0.0.1')
PORT = int(os.environ.get('P2P_PORT', '8462'))
AGENT_URL = f'http://{HOST}:{PORT}'

# The raw UDP port mesh/p2p/p2p_app.py's worker listens on for actual
# cross-machine task dispatch (its own protocol, not A2A) - separate from
# the A2A port above, same "two ports, one process" shape
# mesh/compute_share/constants.py's own AVAILABILITY_PORT already uses.
UDP_PORT = int(os.environ.get('P2P_UDP_PORT', '9998'))

# What this instance advertises to the matchmaker as servable - a
# discovery label only; the worker always answers with whatever model
# agent_sdk.py's ask() actually resolves locally, regardless of this list
# (see mesh/p2p/p2p_app.py's own worker loop).
CAPABILITIES = [c.strip() for c in os.environ.get('P2P_CAPABILITIES', 'qwen2.5-7b,llama3').split(',') if c.strip()]
