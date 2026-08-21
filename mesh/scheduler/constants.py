"""
Scheduler Agent's own identity constants - shared by server.py (builds the
card, binds the port) and skills/schedule_job.py (needs its own URL to hand
cron_trigger.register_trigger as the callback target). Split out to avoid a
server -> agent_executor -> schedule_job -> server import cycle.
"""
AGENT_ID = 'scheduler'
HOST = '127.0.0.1'
PORT = 8420
AGENT_URL = f'http://{HOST}:{PORT}'

# Must match mesh/mcp/cron_trigger/server.py's HOST/PORT and FastMCP's default
# streamable_http_path ('/mcp'). Not imported from there directly - that module
# constructs a live AsyncIOScheduler + SQLAlchemyJobStore at import time, too
# heavy a side effect to pull in just to read two constants.
CRON_TRIGGER_URL = 'http://127.0.0.1:8421/mcp'
