"""Orchestrator Agent's identity constants. Which other A2A agents exist and
where they live is no longer hardcoded here - see mesh/orchestrator/router.py,
which resolves that from the Agent Registry (mesh/mcp/agent_registry/) at
startup instead."""
AGENT_ID = 'orchestrator'
HOST = '127.0.0.1'
PORT = 8426
AGENT_URL = f'http://{HOST}:{PORT}'

# https, not http - whatsapp MCP serves self-signed TLS only (mesh/lib/tls.py).
# mesh/lib/mcp_client.py's call_tool() skips CA verification for exactly this.
WHATSAPP_MCP_URL = 'https://127.0.0.1:8425/mcp'
