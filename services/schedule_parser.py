"""
Natural language -> cron expression, for AI Cron Jobs (services/cron_scheduler.py).

One scoped LLM call, then validated with croniter before it's ever stored - same
validate-before-write discipline services/owner_admin_handler.py already uses for
temperature/timeout. A bad model output fails the tool call loudly with an error
instead of silently storing a schedule that doesn't parse.
"""
from croniter import croniter
from langchain_core.messages import HumanMessage, SystemMessage

SCHEDULE_SYSTEM_PROMPT = (
    "Convert the user's scheduling request into a single standard 5-field cron "
    "expression (minute hour day-of-month month day-of-week). Respond with ONLY the "
    "cron expression, nothing else - no explanation, no surrounding text, no code "
    "fences. Example: for 'every night at 9pm' respond exactly: 0 21 * * *"
)


async def parse_natural_schedule(text: str, model_name: str, ollama_url: str) -> str:
    """Raises ValueError if the model's output isn't a valid 5-field cron expression."""
    from langchain_ollama import ChatOllama

    # Built fresh per call, not cached - same cross-event-loop reason every other
    # ChatOllama construction in this codebase is (agents/llm_agent.py's
    # _build_react_agent, agents/reasoning_cycle.py's _call_stage).
    model = ChatOllama(model=model_name, base_url=ollama_url, temperature=0.0)
    result = await model.ainvoke([
        SystemMessage(content=SCHEDULE_SYSTEM_PROMPT),
        HumanMessage(content=text),
    ])
    candidate = (result.content or '').strip().strip('`').strip()
    if not croniter.is_valid(candidate):
        raise ValueError(
            f"Could not turn {text!r} into a valid schedule (model returned: {candidate!r})"
        )
    return candidate
