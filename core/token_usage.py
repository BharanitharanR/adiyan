"""
Shared token-usage tracking - real per-call counts (from ChatOllama's own
usage_metadata on each AIMessage), not an estimate, recorded to
config/database.py's token_usage table for the dashboard's token-usage card.

The intended use is a concrete "this is what stays on your own hardware"
number: exactly how many tokens each routine run, coaching turn, or admin
call actually cost, broken down by routine so it's obvious which automations
are expensive and which are cheap.
"""
import logging
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage

logger = logging.getLogger('TokenUsage')


def sum_usage(messages: List[Any]) -> Dict[str, int]:
    """Sums usage_metadata across every AIMessage in a message list - a single
    agent.ainvoke() call (a react-agent tool-calling loop) can produce more
    than one AIMessage (one per turn before the final answer), and every one
    of them consumed real tokens, not just the last."""
    prompt = completion = total = 0
    for m in messages:
        if isinstance(m, AIMessage):
            usage = getattr(m, 'usage_metadata', None)
            if usage:
                prompt += usage.get('input_tokens') or 0
                completion += usage.get('output_tokens') or 0
                total += usage.get('total_tokens') or 0
    return {'prompt_tokens': prompt, 'completion_tokens': completion, 'total_tokens': total}


def record(context_type: str, model: str, messages: List[Any], context_label: Optional[str] = None,
           routine_name: Optional[str] = None, job_id: Optional[int] = None,
           contact_name: Optional[str] = None):
    """Sums usage from a message list and records it. Best-effort and never
    raises - a tracking bug must never break the actual feature it's
    measuring (a job send, a coaching reply, an admin action)."""
    try:
        usage = sum_usage(messages)
        if usage['total_tokens'] == 0:
            return
        import config.database as db
        db.record_token_usage(
            context_type=context_type, model=model, context_label=context_label,
            routine_name=routine_name, job_id=job_id, contact_name=contact_name, **usage,
        )
    except Exception:
        logger.warning('Failed to record token usage', exc_info=True)
