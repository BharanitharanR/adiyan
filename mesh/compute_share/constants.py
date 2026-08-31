"""compute_share's identity constants.

Two live instances of this exact agent stand in for two different users'
Adiyan installs in the POC (see mesh/compute_share/README.md), each one
launched with its own PORT/DISPLAY_NAME via environment variables, not
two different codebases - the whole point being demonstrated is that "my
Adiyan" and "a peer's Adiyan" run the identical software.

AGENT_ID is deliberately NOT one of the things that varies per instance,
even though PORT and DISPLAY_NAME are - confirmed live to matter: an
earlier version of this file let AGENT_ID vary too ('alice'/'bob'), and
permissions.is_allowed() composes its lookup key from AGENT_ID
(f'{AGENT_ID}.{skill_id}'), so Bob's own instance was checking
'bob.run_inference' against a permissions_config.json rule written for
'compute_share.run_inference' - never matching, silently rejecting every
real peer call. Every real user's Adiyan runs this identical codebase
under the same AGENT_ID; only the reachable address and a cosmetic label
differ, so those are the only two things this file lets vary."""
import os

AGENT_ID = 'compute_share'
DISPLAY_NAME = os.environ.get('COMPUTE_SHARE_DISPLAY_NAME', 'compute_share')
HOST = '127.0.0.1'
PORT = int(os.environ.get('COMPUTE_SHARE_PORT', '8460'))
AGENT_URL = f'http://{HOST}:{PORT}'
OLLAMA_URL = os.environ.get('COMPUTE_SHARE_OLLAMA_URL', 'http://localhost:11434')

# Where this instance's own state.db/tasks.db live on disk - separate from
# AGENT_ID on purpose. In a real deployment every user's Adiyan lives on
# its own machine under its own ~/.Adiyan, so this distinction wouldn't
# exist at all; it only exists here because the POC runs two "different
# users' machines" as two processes on one machine, and they need
# non-colliding storage without lying about which agent_id they are for
# permission purposes.
STORAGE_ID = f'{AGENT_ID}_{DISPLAY_NAME}'
