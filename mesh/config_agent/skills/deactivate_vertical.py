"""deactivate_vertical's real body - reverts the deployment to plain
platform defaults. Always succeeds even if nothing was active (idempotent -
"turn off the vertical" should never fail just because it was already off)."""
from typing import Any, Dict

from mesh.lib import config_sdk


async def run() -> Dict[str, Any]:
    current = await config_sdk.get_active_vertical_id()
    ok = await config_sdk.set_active_vertical_id(None)
    if not ok:
        return {'deactivated': False, 'message': 'Could not write the deactivation - the config store may be unreachable.'}
    return {'deactivated': True, 'was_active': current}
