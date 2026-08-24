"""get_active_vertical's real body - what's active right now, if anything."""
from typing import Any, Dict

from mesh.lib import config_sdk


async def run() -> Dict[str, Any]:
    vertical_id = await config_sdk.get_active_vertical_id()
    return {'active_vertical_id': vertical_id} if vertical_id else {'active_vertical_id': None, 'message': 'Running plain platform defaults - no vertical active.'}
