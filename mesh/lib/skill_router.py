"""
The shared two-stage dispatcher every agent under mesh/ needs: given free
text from a caller treated as a stranger (no shortcuts for Adiyan's own
orchestrator - see mesh/scheduler/agent_executor.py's module docstring),
decide which of this agent's AgentSkills applies, then extract that skill's
parameters from prose.

Generic on purpose: this file knows nothing about any specific agent's
skills or parameter shapes. The caller supplies the AgentSkill list (for
building the classifier prompt from real card content, not duplicated text)
and a {skill_id: Pydantic model} map (for extraction) - both are agent-
specific and don't belong in a shared module.
"""
from typing import Any, Dict, List, Optional, Type

from a2a.types import AgentSkill
from langchain_ollama import ChatOllama
from pydantic import BaseModel


class SkillChoice(BaseModel):
    """skill_id set and ambiguous_between empty: a clean match.
    skill_id None and ambiguous_between empty: no skill matches - a real,
    valid outcome, not an error.
    skill_id None and ambiguous_between has 2+ ids: genuinely ambiguous -
    distinct from no-match, and the caller must not conflate the two."""
    skill_id: Optional[str] = None
    ambiguous_between: List[str] = []


def _classify_prompt(message_text: str, skills: List[AgentSkill]) -> str:
    skill_blocks = []
    for skill in skills:
        examples = '\n'.join(f'  e.g. "{ex}"' for ex in skill.examples)
        skill_blocks.append(f'Skill: {skill.id}\n  {skill.description}\n{examples}')
    return (
        'Given the skills below and what the caller said, decide which skill applies.\n'
        'If exactly one clearly applies, set skill_id and leave ambiguous_between empty.\n'
        'If none apply, leave both empty.\n'
        'If two or more plausibly apply and the text does not disambiguate them, '
        'leave skill_id empty and list their ids in ambiguous_between.\n\n'
        + '\n\n'.join(skill_blocks)
        + f'\n\nCaller said: "{message_text}"'
    )


async def classify(message_text: str, skills: List[AgentSkill], model_cfg: Dict[str, Any]) -> SkillChoice:
    model = ChatOllama(
        model=model_cfg['model'],
        base_url=model_cfg.get('base_url', 'http://localhost:11434'),
        temperature=model_cfg['temperature'],
    ).with_structured_output(SkillChoice)
    choice = await model.ainvoke(_classify_prompt(message_text, skills))
    valid_ids = {s.id for s in skills}
    if choice.skill_id not in valid_ids:
        # Covers two real, confirmed failure modes: "" instead of JSON null
        # for "no match" (local models via structured output don't always
        # follow that convention), and outright hallucinated skill_id
        # strings that were never in the list at all (confirmed live:
        # "How to run a routine" instead of a real id) - SkillChoice.skill_id
        # is an open Optional[str], nothing constrains it to the real set.
        # Anything not actually offered is treated as no match, not passed
        # through to crash extraction_schemas[choice.skill_id] downstream.
        choice.skill_id = None
    return choice


async def extract(
    message_text: str,
    skill_id: str,
    schema: Type[BaseModel],
    model_cfg: Dict[str, Any],
) -> BaseModel:
    """schema is the skill's own extraction shape (e.g. ScheduleJobParams) -
    supplied by the caller, not looked up here. Narrowing the prompt to just
    this one skill's fields, not the whole catalog, is the point of running
    this as a separate stage from classify()."""
    model = ChatOllama(
        model=model_cfg['model'],
        base_url=model_cfg.get('base_url', 'http://localhost:11434'),
        temperature=model_cfg['temperature'],
    ).with_structured_output(schema)
    field_lines = []
    for name, field in schema.model_fields.items():
        line = f'- {name}'
        if field.description:
            line += f': {field.description}'
        # field.is_required() is the correct check - field.default is
        # Pydantic's PydanticUndefined sentinel for a required field, not
        # None and not Ellipsis, so testing against those two directly let
        # that sentinel's own repr leak into the prompt as literal text.
        if not field.is_required():
            line += f' (default: {field.default!r} - use this unless the text clearly says otherwise, never invent a value)'
        field_lines.append(line)
    return await model.ainvoke(
        'Extract these fields from what the caller said. For any field with a '
        'default listed, use that default unless the text clearly specifies '
        'otherwise - do not guess or invent a value just to fill the field.\n\n'
        + '\n'.join(field_lines)
        + f'\n\nCaller said: "{message_text}"'
    )


async def route(
    message_text: str,
    skills: List[AgentSkill],
    extraction_schemas: Dict[str, Type[BaseModel]],
    classify_cfg: Dict[str, Any],
    extract_cfg: Dict[str, Any],
) -> tuple:
    """Returns (skill_id_or_None, ambiguous_between, params_dict). params_dict
    is empty unless skill_id is set - extraction never runs for a no-match or
    ambiguous outcome, by design (see this module's docstring on why classify
    and extract stay separate stages)."""
    choice = await classify(message_text, skills, classify_cfg)
    if choice.skill_id is None:
        return None, choice.ambiguous_between, {}
    schema = extraction_schemas[choice.skill_id]
    extracted = await extract(message_text, choice.skill_id, schema, extract_cfg)
    return choice.skill_id, [], extracted.model_dump()
