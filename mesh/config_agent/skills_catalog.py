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
    AgentSkill(
        id='activate_vertical',
        name='Activate Vertical',
        description=(
            'Re-enable a named business vertical that was previously deactivated, so it '
            'answers to its own summon phrase again - every other configured vertical (and '
            'plain platform defaults) keep working the whole time, this only affects the '
            'one named. The vertical must already have at least one setting configured '
            'somewhere; refuses an unknown one rather than creating a phantom vertical.'
        ),
        tags=['config', 'admin', 'vertical'],
        examples=[
            'Activate the gym_trainer vertical',
            'Turn the gym_trainer persona back on',
            'Re-enable nutrition_coach',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
    AgentSkill(
        id='deactivate_vertical',
        name='Deactivate Vertical',
        description=(
            'Stop a named business vertical from answering to its own summon phrase - '
            'every other configured vertical (and plain platform defaults) are unaffected. '
            'Its settings and summon phrase stay on file, so activate_vertical can bring it '
            'straight back with no re-upload.'
        ),
        tags=['config', 'admin', 'vertical'],
        examples=[
            'Deactivate the gym_trainer vertical',
            'Turn off nutrition_coach',
            'Stop responding to the gym_trainer wake phrase',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
    AgentSkill(
        id='get_active_vertical',
        name='Get Active Vertical',
        description=(
            'List every business vertical configured on this deployment, its own summon '
            'phrase, and whether it is currently enabled - multiple can be live at once, '
            'alongside plain platform defaults on @adiyan.'
        ),
        tags=['config', 'admin', 'vertical'],
        examples=[
            'Which verticals are configured right now?',
            'What personas is this deployment running?',
            'List the active business verticals',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
    AgentSkill(
        id='update_customer_record',
        name='Update Customer Record',
        description=(
            "Set a fact on a specific customer's record for the currently-summoned business "
            "vertical - the ONLY way a consequential fact (payment confirmed, active/inactive, a "
            "subscription plan) gets recorded, since customers can never write or see this "
            "themselves. Requires the customer's real phone number, not just their name. Must be "
            "issued under that business's own wake phrase, not the plain @adiyan default."
        ),
        tags=['config', 'admin', 'vertical', 'customer'],
        examples=[
            'Mark 9198765432 as paid for the tiffin plan',
            "Set Priya's (9198765432) subscription status to active",
            'Record that 919876543210 confirmed their order',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
]
