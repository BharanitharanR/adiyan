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

# The kill switch: whether this machine ever accepts inbound work from a
# peer at all - "am I a worker," not "can I ask for help" (that half is
# already gated by the caller's own community trigger word, unrelated to
# this flag). Deliberately checked in exactly one place (server.py, before
# the UDP socket is ever bound or the heartbeat announcer ever starts) -
# false means this machine is never discoverable and never listening,
# not just "listens but refuses," so there's no socket for an offender to
# even reach. Default true (the feature only exists if it's on), but this
# is the one lever to turn it off entirely without touching code, e.g. if
# abuse shows up faster than the guardrails below can be tuned.
ENABLED = os.environ.get('P2P_ENABLED', 'true').strip().lower() not in ('false', '0', 'no')

# Guardrails for the worker (see p2p_app.py's start_worker_endpoint) - the
# real protection given this design's own no-peer-auth stance (see
# mesh/compute_share/README.md's original BitTorrent reasoning, carried
# over here): verify what's being asked, not who's asking, since a
# genuinely different Adiyan install has no credential this one could
# check anyway. None of these need a real identity to be meaningful.
#
# A generous prompt can legitimately run a few hundred words through
# Ollama; there's no legitimate reason for a single request to need
# many times that just to ask a question.
MAX_PROMPT_CHARS = int(os.environ.get('P2P_MAX_PROMPT_CHARS', '4000'))

# Per-source-IP request budget - keyed on the sender's address (whatever
# arrives on the UDP socket, effectively free to fake unlike a real TCP
# handshake's source, but still enough to stop an ordinary flood without
# needing peer identity at all). Reset on a rolling window, not a hard
# quota - see p2p_app.py's own rate-limiter for the exact algorithm.
RATE_LIMIT_PER_MINUTE = int(os.environ.get('P2P_RATE_LIMIT_PER_MINUTE', '10'))

# How many requests this worker will actually run through Ollama at once,
# queueing (not rejecting) anything past that - same reasoning as
# mesh/inference_router/skills/complete.py's own LOCAL_CONCURRENCY_LIMIT,
# just enforced on the serving side here instead of the asking side: a
# burst of legitimate-looking requests still shouldn't be able to pin
# this machine's Ollama with unbounded concurrent generations.
WORKER_CONCURRENCY_LIMIT = int(os.environ.get('P2P_WORKER_CONCURRENCY_LIMIT', '1'))
