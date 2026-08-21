"""Journal Agent's AgentSkill catalog - same single-source-of-truth pattern
as every other agent's skills_catalog.py."""
from a2a.types import AgentSkill

SKILLS = [
    AgentSkill(
        id='craft_reflection_prompt',
        name='Craft Reflection Prompt',
        description="Craft a tailored journaling question for one person, personalized from what's known about them if anything is - otherwise a thoughtful general one.",
        tags=['journal', 'reflection'],
        examples=[
            'Craft a reflection prompt for sam_92, themed around work stress',
            'Give me a journaling question for tonight',
            'Remind me every night to journal',
            'Help me journal tonight',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
]
