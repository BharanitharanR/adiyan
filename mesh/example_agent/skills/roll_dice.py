"""
roll_dice's real body - the entire thing a new agent author has to write.
No permission check, no A2A wiring, no config loading in here: all of that
already happened before this function was ever called (see
agent_executor.py). This is just the actual work.
"""
import random
from typing import Any, Dict


async def run(sides: int = 6) -> Dict[str, Any]:
    if sides < 2:
        raise ValueError('A die needs at least 2 sides.')
    result = random.randint(1, sides)
    return {'sides': sides, 'result': result}
