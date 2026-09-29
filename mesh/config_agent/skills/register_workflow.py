"""
register_workflow's real body - the "no code change" half of Adiyan's n8n
integration (see mesh/analysis/skills/analyze.py's trigger_workflow tool,
which reads this exact list to decide what it can call and how to describe
it to the model). A business owner builds a workflow in n8n's own editor,
gives it a webhook path, then tells Adiyan about it with one command like
"register a workflow called order_confirmation at new-order for placing
food orders" - no Python code or restart involved on Adiyan's side; the
next analyse_this call for that vertical picks it up immediately, since
run() re-fetches workflow_registry on every call.

Upserts by name, not append-only - re-registering an already-known
workflow (a typo fix on its description, or moving it to a new webhook
path) replaces the old entry rather than leaving a stale duplicate behind
for trigger_workflow to also list.

vertical_id comes from the caller's own token claims, same reasoning as
update_customer_record.py - refuses to guess when none is present, rather
than writing a workflow into the wrong business's registry or a bare
platform-level list nothing ever reads.

Owner-only in practice, same as update_customer_record.py - no
permissions_config.json entry needed, the owner tier's own 'allow' is
already ['*'].

The actual upsert-by-name write lives in mesh/lib/workflow_registry.py now,
not here - apply_vertical_spec.py's own auto-generated workflows (see its
`workflows:` spec section) need the identical write, and duplicating the
upsert logic across "an owner typed this" and "a spec asked for this"
would have meant fixing the same bug twice the next time it needed one.
"""
from typing import Any, Dict, Optional

from mesh.lib import workflow_registry


async def run(
    name: str, webhook_path: str, description: str, vertical_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not vertical_id:
        return {
            'registered': False,
            'error': (
                "No business vertical is active for this command - use that business's own wake "
                "phrase (e.g. \"@marinaspice register a workflow...\") so Adiyan knows which "
                'vertical this workflow belongs to.'
            ),
        }
    ok = await workflow_registry.register(vertical_id, name, webhook_path, description)
    if not ok:
        return {'registered': False, 'error': 'Could not write the registration - the config store may be unreachable.'}
    return {'registered': True, 'vertical_id': vertical_id, 'name': name, 'webhook_path': webhook_path}
