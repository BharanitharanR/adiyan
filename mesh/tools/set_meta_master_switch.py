#!/usr/bin/env python3
"""
The ONLY place that turns on real Meta Model API spend, platform-wide.
Run by hand, locally, on this machine - never from WhatsApp, never from a
chat phrase, never by any agent's own code. mesh/lib/agent_sdk.py's ask()
checks this constant before it will even look at any per-agent 'use_meta'
toggle (e.g. the one mesh/orchestrator/skills/handle_message.py's
_parse_wayfinder_meta_command flips from a WhatsApp message) - with this
switch off, NO call reaches Meta no matter what that toggle says.

Off by default on a fresh install. Flip it on only when you actually want
real API spend to start happening.

Usage (run from the repo root):
    python3 -m mesh.tools.set_meta_master_switch --status
    python3 -m mesh.tools.set_meta_master_switch --on
    python3 -m mesh.tools.set_meta_master_switch --off
"""
import argparse
import asyncio

from mesh.lib import config_sdk

_AGENT_ID = '_meta_model'
_KEY = 'master_switch_enabled'


async def _get() -> bool:
    return await config_sdk.get_constant(_AGENT_ID, _KEY, False)


async def _set(value: bool) -> None:
    await config_sdk.set_constant(_AGENT_ID, _KEY, value)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--on', action='store_true', help='Allow real Meta Model API calls platform-wide')
    group.add_argument('--off', action='store_true', help='Block every Meta Model API call platform-wide (safe default)')
    group.add_argument('--status', action='store_true', help='Show the current switch state, change nothing')
    args = parser.parse_args()

    if args.status:
        current = asyncio.run(_get())
        print(f"Meta master switch: {'ON - real API calls can happen' if current else 'OFF - no request reaches Meta'}")
        return

    if args.on:
        asyncio.run(_set(True))
        print(
            'Meta master switch is now ON.\n'
            'Any agent/stage whose own config AND its own "use_meta" toggle are also on will now '
            'make real, billed calls to the Meta Model API.'
        )
        return

    asyncio.run(_set(False))
    print('Meta master switch is now OFF. No request will reach Meta, regardless of any other toggle.')


if __name__ == '__main__':
    main()
