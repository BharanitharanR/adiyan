"""
The one surface a new agent is expected to know about to reach WhatsApp -
everything below this (tokens, tiers, whatsapp_mcp, chat_id resolution) is
platform plumbing, not something agent code should ever need to think
about. Mirrors mesh/lib/config_sdk.py's role for config: a clean SDK an
agent imports, not an internal it reaches around.

    from mesh.lib.agent_sdk import AdiyanAgent
    agent = AdiyanAgent('my_agent_id')
    await agent.notify_owner("some processed result")

The only thing agent code still has to do by hand is add its own tier to
mesh/lib/permissions_config.json (see TIER_SUFFIX below for the exact
name) - that's a deliberate, explicit, human-reviewed grant, not
something this class auto-creates at runtime. Everything else - which
tier to mint against, how to resolve "owner," which MCP tool to call - is
this class's job, not the agent's.
"""
from mesh.lib.utilities.whatsapp.notify_owner import notify_owner as _notify_owner

# The tier-naming convention every *_service tier added tonight already
# follows (adiyan_reader_service, and this file's own docstring example) -
# centralized here once so "what does my agent's tier need to be called"
# has exactly one answer, not one invented per agent.
TIER_SUFFIX = '_service'


class AdiyanAgent:
    """One instance per agent, constructed once with that agent's own id -
    every method call mints a token against `<agent_id>_service`
    internally, never asking the caller to know that tier exists."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self._tier = f'{agent_id}{TIER_SUFFIX}'

    async def notify_owner(self, text: str) -> bool:
        """Sends `text` to the owner's own WhatsApp (self-chat). True on
        send, False otherwise (owner's phone couldn't be resolved, this
        agent's tier isn't allowed one of the two calls it needs, or
        whatsapp_mcp/OpenWA is unreachable) - never raises, per this
        mesh's whatsapp-silent-on-failure rule.

        Requires mesh/lib/permissions_config.json to have a
        `<agent_id>_service` tier allowing both
        'mcp.whatsapp.get_own_phone' and 'mcp.whatsapp.send_message' - see
        adiyan_reader_service for the exact shape to copy."""
        return await _notify_owner(self.agent_id, self._tier, text)
