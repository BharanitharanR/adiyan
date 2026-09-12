"""
How a chat_id becomes the one stable key everything else in this mesh keys
off. Lives in mesh/lib/ rather than mesh/orchestrator/db.py (where it
started) because it stopped being orchestrator's private concern the
moment agents began reading identity out of their own call tokens - an
agent importing mesh.orchestrator.db just to normalize a key would be a
dependency pointing the wrong way.

Pure function, no I/O, no imports - so both orchestrator (which stores the
result as orchestrator_clients' own _id) and any agent (which derives the
same value from its A2A token's `sub` claim) reach the identical key
without either one owning the other.
"""


def resolve_identity_key(chat_id: str) -> str:
    """Phone digits when chat_id is already phone-form (@c.us) - the
    stable, WhatsApp-account-level identifier. Falls back to the raw
    chat_id (usually @lid) otherwise - a lid-form contact's phone number
    isn't safely resolvable (resolve_chat_id() is confirmed live to hang
    indefinitely - see mesh/mcp/whatsapp/server.py's get_own_phone()
    docstring), so the lid is the best available identity for them today.

    Every clients-collection read/write, and now every memory-graph
    Identity node, goes through this - not the raw chat_id a webhook
    happened to report. Confirmed live that the same contact can be
    addressed in different JID forms depending on which field you read
    (see openwa_receiver.py's is_self_chat fix), so comparing raw chat_id
    values directly is unreliable. Delivery (send_message) still uses the
    real, un-normalized chat_id - only identity lookups go through this."""
    if chat_id and chat_id.endswith('@c.us'):
        return chat_id.split('@')[0]
    return chat_id
