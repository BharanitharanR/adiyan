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
import uuid

from mesh.lib.paths import agent_home

AGENT_ID = 'compute_share'
DISPLAY_NAME = os.environ.get('COMPUTE_SHARE_DISPLAY_NAME', 'compute_share')

# Defaults to loopback-only, same as every other agent in this mesh - a
# real cross-machine peer test is an explicit opt-in
# (COMPUTE_SHARE_HOST=0.0.0.0, or a Tailscale-bound interface), not
# something this file turns on by itself just because compute_share's
# whole purpose is eventually being reached from outside.
HOST = os.environ.get('COMPUTE_SHARE_HOST', '127.0.0.1')
PORT = int(os.environ.get('COMPUTE_SHARE_PORT', '8460'))
AGENT_URL = f'http://{HOST}:{PORT}'

# The address actually handed to peers via announce_peer - distinct from
# AGENT_URL (what this process binds to) because a real deployment binds
# to 0.0.0.0 but announces a specific reachable address (a Tailscale IP,
# a public URL) - "bind everywhere, announce one real address" is a
# normal split, not something HOST alone can express. Falls back to
# AGENT_URL so purely local testing (both peers on localhost) needs no
# extra configuration, matching the original POC's own walkthrough.
PUBLIC_URL = os.environ.get('COMPUTE_SHARE_PUBLIC_URL', AGENT_URL)

OLLAMA_URL = os.environ.get('COMPUTE_SHARE_OLLAMA_URL', 'http://localhost:11434')

# Where this instance's own state.db/tasks.db live on disk - separate from
# AGENT_ID on purpose. In a real deployment every user's Adiyan lives on
# its own machine under its own ~/.Adiyan, so this distinction wouldn't
# exist at all; it only exists here because the POC runs two "different
# users' machines" as two processes on one machine, and they need
# non-colliding storage without lying about which agent_id they are for
# permission purposes.
STORAGE_ID = f'{AGENT_ID}_{DISPLAY_NAME}'


def _load_or_create_instance_id() -> str:
    # A stable handle for "this same peer," independent of PUBLIC_URL -
    # confirmed necessary (not just tidier) because an address can change
    # (a Tailscale IP reassigned, a reconnect) while remaining the same
    # peer; keying gossip on address alone would make every reconnect
    # look like a brand new, unrelated peer. Generated once, persisted
    # locally, never tied to a name, phone number, or WhatsApp identity -
    # purely a random handle for peer bookkeeping.
    id_path = agent_home(STORAGE_ID) / 'instance_id'
    if id_path.exists():
        return id_path.read_text().strip()
    new_id = str(uuid.uuid4())
    id_path.write_text(new_id)
    return new_id


INSTANCE_ID = _load_or_create_instance_id()
