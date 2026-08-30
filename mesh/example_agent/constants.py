"""Example Agent's own identity constants.

This whole agent exists as a reference implementation - see this
directory's README.md for what plugging in a new agent actually involves,
and what the harness gives you for free just by following this shape."""
AGENT_ID = 'example_agent'
HOST = '127.0.0.1'
PORT = 8440
AGENT_URL = f'http://{HOST}:{PORT}'
