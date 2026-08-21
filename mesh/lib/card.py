"""
Builds an AgentCard with Adiyan's own repeated defaults filled in, so each
agent's own server.py only supplies what's actually agent-specific: name,
description, skills, and where it listens.

Targets A2A protocol v1.0 (protobuf-normative - see mesh/scheduler/server.py's
module docstring for why v0.3/Pydantic-shaped fields like preferredTransport
or stateTransitionHistory don't apply here).
"""
from typing import List

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
)


def adiyan_card(
    name: str,
    description: str,
    skills: List[AgentSkill],
    host: str,
    port: int,
    version: str = '0.1.0',
) -> AgentCard:
    """Every Adiyan agent shares the same provider, capabilities, and default
    modes - only identity and skills genuinely differ agent to agent."""
    return AgentCard(
        name=name,
        description=description,
        version=version,
        provider=AgentProvider(organization='Adiyan'),
        default_input_modes=['text/plain'],
        default_output_modes=['application/json'],
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=True,
        ),
        supported_interfaces=[
            AgentInterface(
                protocol_binding='JSONRPC',
                url=f'http://{host}:{port}',
                protocol_version='1.0',
            ),
        ],
        skills=skills,
    )
