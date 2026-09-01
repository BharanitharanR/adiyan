#!/usr/bin/env python3
"""
Switches every agent's own LLM stage(s) onto one model, via
config_sdk.set_stage_config() - the same explicit-overwrite path the
config dashboard's own edit uses, not a new mechanism. For a machine
that genuinely needs a lighter model than whatever ships as this
codebase's default (confirmed live: qwen3:8b's real weight footprint
alongside this mesh's own dozen-plus processes produced real memory
pressure on an 8GB Mac).

Discovers every stage to touch from each agent's own runtime_config.json
(the same file that seeds config_sdk's defaults on first run) - anything
with a "model" key gets switched, nothing else in that stage's config is
touched. This is a one-time, deliberate switch, not a standing default:
running install.sh again, or a fresh agent added later, still seeds
from its own runtime_config.json as before - re-run this again after
either of those if you want the switch to stick everywhere.

Needs MongoDB up (mesh/start_all.sh start mongodb is enough - the rest
of the mesh doesn't need to be running for this).

Run from the repo root:
    python3 -m mesh.tools.set_default_model qwen3:4b-16k
"""
import argparse
import asyncio
import json
from pathlib import Path

from mesh.lib import config_sdk

REPO_ROOT = Path(__file__).parent.parent.parent


async def main(model: str) -> None:
    updated = 0
    skipped = 0
    for runtime_config_path in sorted((REPO_ROOT / 'mesh').glob('*/runtime_config.json')):
        agent_id = runtime_config_path.parent.name
        try:
            stages = json.loads(runtime_config_path.read_text()).get('stages', {})
        except json.JSONDecodeError:
            continue
        for stage_name, cfg in stages.items():
            if 'model' not in cfg:
                continue
            new_cfg = dict(cfg)
            new_cfg['model'] = model
            ok = await config_sdk.set_stage_config(agent_id, stage_name, new_cfg)
            if ok:
                updated += 1
                print(f'  ok     {agent_id}.{stage_name} -> model={model!r}')
            else:
                skipped += 1
                print(f'  FAILED {agent_id}.{stage_name} - is MongoDB running?')

    print(f'\n{updated} stage(s) switched to {model!r}' + (f', {skipped} failed' if skipped else '') + '.')
    if updated == 0 and skipped == 0:
        print('Nothing found to switch - no runtime_config.json under mesh/*/ declares a model for any stage.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('model', help="Ollama model tag every stage should switch to, e.g. qwen3:4b-16k")
    args = parser.parse_args()
    asyncio.run(main(args.model))
