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

# workspace-mcp (Gmail/Calendar/Drive/...) - see mesh/tools/run_workspace_mcp.sh.
# Plain http, loopback only - the public-facing leg is Google's OAuth redirect
# through ngrok/nginx (mesh/nginx/generate_config.py's /oauth2callback route),
# not this URL, which Orchestrator only ever calls from this same machine.
WORKSPACE_MCP_URL = 'http://127.0.0.1:8447/mcp'
