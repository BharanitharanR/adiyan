"""Config Server's own identity constants, plus the agents it talks to."""
HOST = '127.0.0.1'
PORT = 8500

CONFIG_AGENT_URL = 'http://127.0.0.1:8428'
WHATSAPP_MCP_URL = 'https://127.0.0.1:8425/mcp'

OTP_TTL_SECONDS = 300

# OpenWA's own dashboard (QR code / registration status) - single-port
# production setup, the NestJS app serves its bundled SPA from the same
# port it listens for API/webhook traffic on (see penwa/src/app.module.ts's
# ServeStaticModule wiring). Same value as mesh/scheduler/skills/
# run_routine.py's OPENWA_URL - not imported from there to avoid a
# config_server -> scheduler dependency for one URL string.
OPENWA_DASHBOARD_URL = 'http://localhost:2785'

# How long the agents-online page waits for one agent's card before calling
# it offline - short enough that a genuinely dead agent doesn't stall the
# whole page, long enough not to false-negative a slow-but-alive one.
AGENT_STATUS_TIMEOUT_SECONDS = 1.5
