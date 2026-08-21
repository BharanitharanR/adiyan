"""Orchestrator Agent's AgentSkill catalog. One skill: take an incoming
message and a chat to reply to, figure out who should handle it, reply.
Primary caller is the whatsapp MCP server's webhook push (always a precise
DataPart call - see mesh/mcp/whatsapp/server.py), but kept A2A-compliant
with real examples for any future free-text caller too."""
from a2a.types import AgentSkill

SKILLS = [
    AgentSkill(
        id='handle_message',
        name='Handle Message',
        description="Route an incoming message to the right agent, get a response, and reply back to the given chat.",
        tags=['orchestration', 'routing'],
        examples=[
            'Handle this message and reply to the sender',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
]
