"""Config Agent's AgentSkill catalog - single source for server.py's card
and agent_executor.py's classifier prompt, same reasoning as every other
agent's skills_catalog.py.

Owner-only by construction, not by a special-case check here: neither skill
appears in the 'service' or 'standard' tiers of permissions_config.json,
only 'owner' (wildcard '*') - see mesh/lib/permissions.py."""
from a2a.types import AgentSkill

SKILLS = [
    AgentSkill(
        id='query_config',
        name='Query Config',
        description=(
            'Look up a live setting, prompt, or toggle for one of the mesh agents - '
            "what a prompt currently says, what model/temperature a stage uses, "
            "whether a toggle like strict grounding is on. Read-only."
        ),
        tags=['config', 'admin'],
        examples=[
            "What's Orchestrator's humanize prompt right now?",
            'Is strict grounding on for Analysis Agent?',
            'Show me all of Orchestrator\'s settings',
            'What model does the classify_skill stage use?',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
    AgentSkill(
        id='update_config',
        name='Update Config',
        description=(
            'Change a live constant or toggle for one of the mesh agents - a prompt '
            'template, a feature flag like strict grounding. Not for stage settings '
            '(model/temperature/timeout) - those need the config dashboard.'
        ),
        tags=['config', 'admin'],
        examples=[
            'Turn off strict grounding for Analysis Agent',
            'Set strict grounding to true for analysis',
            "Update Orchestrator's humanize prompt to be more casual",
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
]
