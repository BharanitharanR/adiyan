"""
Scheduler Agent's AgentSkill catalog - the single source both server.py (for
the public card) and agent_executor.py (for skill_router's classifier
prompt) read from. Split out to avoid a server <-> agent_executor cycle, and
so the card and the router can never quietly drift to describe different
skills.
"""
from a2a.types import AgentSkill

SKILLS = [
    AgentSkill(
        id='schedule_job',
        name='Schedule Job',
        description='Create a recurring or one-time WhatsApp action from a natural-language time expression.',
        tags=['scheduling', 'cron', 'whatsapp'],
        examples=[
            'Every Sunday at 6pm, send everyone the weekly recap',
            'Remind me every night to journal',
            'Send this to everyone this week who said yes to the poll',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
    AgentSkill(
        id='run_routine',
        name='Run Routine',
        description='Re-run an existing named routine by name or trigger phrase.',
        tags=['scheduling', 'routines'],
        examples=[
            'Run the office attendance check now',
            'reached p',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
    AgentSkill(
        id='delete_job',
        name='Delete Job',
        description='Permanently cancel a scheduled job by name or phrase - it will not fire again.',
        tags=['scheduling', 'delete', 'cancel'],
        examples=[
            'Delete the journal reminder',
            'Cancel my morning wakeup job',
            'Stop reminding me about the office check',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
    AgentSkill(
        id='list_jobs',
        name='List Jobs',
        description='Report currently scheduled or pending jobs.',
        tags=['scheduling', 'status'],
        examples=[
            'What jobs are scheduled right now?',
            'Show me pending jobs for the gym group',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
]
