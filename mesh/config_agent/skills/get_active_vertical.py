"""get_active_vertical's real body. Renamed in spirit, not in skill_id (a
skill_id rename would need every existing spec/skills_catalog reference
updated for no functional gain): under phrase-based routing
(mesh/orchestrator/rules_engine.py's _resolve_summoned_vertical()), there is
no single "the active vertical" anymore - N verticals can each be live on
their own summon_phrase at once. This lists every vertical Orchestrator has
any configuration for, its own phrase, and whether it's currently enabled
(vertical_enabled - see deactivate_vertical.py), which is the real,
useful answer to "what's this deployment running" now."""
from typing import Any, Dict, List

from mesh.lib import config_sdk

DEFAULT_SUMMON_PHRASE = '@adiyan'


async def run() -> Dict[str, Any]:
    verticals: List[Dict[str, Any]] = []
    for vertical_id in await config_sdk.list_vertical_ids('orchestrator'):
        phrase = await config_sdk.get_constant(
            'orchestrator', 'summon_phrase', DEFAULT_SUMMON_PHRASE, vertical_id=vertical_id,
        )
        enabled = await config_sdk.get_constant('orchestrator', 'vertical_enabled', True, vertical_id=vertical_id)
        verticals.append({'vertical_id': vertical_id, 'summon_phrase': phrase, 'enabled': enabled})

    if not verticals:
        return {
            'platform_summon_phrase': DEFAULT_SUMMON_PHRASE,
            'verticals': [],
            'message': f'No business vertical configured - only plain platform defaults ({DEFAULT_SUMMON_PHRASE!r}) are live.',
        }
    return {'platform_summon_phrase': DEFAULT_SUMMON_PHRASE, 'verticals': verticals}
