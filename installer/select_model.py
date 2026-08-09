#!/usr/bin/env python3
"""
Pick the best-fit Ollama model for this machine from model_ctx.json,
and write it into ~/.Adiyan/pipeline.json's LLM agent config.

Usage: python3 select_model.py [--dry-run]
Prints the chosen model name to stdout on success.
"""

import json
import subprocess
import sys
from pathlib import Path

INSTALLER_DIR = Path(__file__).resolve().parent
MODEL_CTX_FILE = INSTALLER_DIR / 'model_ctx.json'
PIPELINE_FILE = Path.home() / '.Adiyan' / 'pipeline.json'


def get_system_ram_gb() -> float:
    """macOS-only: total physical RAM in GB."""
    result = subprocess.run(
        ['sysctl', '-n', 'hw.memsize'],
        capture_output=True, text=True, check=True
    )
    return int(result.stdout.strip()) / (1024 ** 3)


def choose_model(model_ctx: dict, ram_gb: float) -> dict:
    """Pick the largest model whose min_ram_gb fits within ram_gb, given the
    manifest's headroom requirement. Falls back to the smallest model if
    even that doesn't fit, rather than failing outright."""
    headroom = model_ctx.get('ram_headroom_gb', 0)
    usable_ram = ram_gb - headroom

    candidates = [m for m in model_ctx['models'] if m['min_ram_gb'] <= usable_ram]
    if candidates:
        return max(candidates, key=lambda m: m['min_ram_gb'])

    # Nothing comfortably fits - fall back to the smallest model and let the
    # user decide whether to proceed rather than blocking installation.
    return min(model_ctx['models'], key=lambda m: m['min_ram_gb'])


def write_model_to_pipeline_config(model_name: str):
    PIPELINE_FILE.parent.mkdir(exist_ok=True)

    if PIPELINE_FILE.exists():
        data = json.loads(PIPELINE_FILE.read_text())
    else:
        # No config yet - main.py's ControlPlane will fill in the rest of the
        # defaults on first run; we only need to seed the model choice here.
        data = {'agents': {'llm': {}}}

    data.setdefault('agents', {}).setdefault('llm', {})['model'] = model_name
    PIPELINE_FILE.write_text(json.dumps(data, indent=2))


def main():
    dry_run = '--dry-run' in sys.argv

    model_ctx = json.loads(MODEL_CTX_FILE.read_text())
    ram_gb = get_system_ram_gb()
    chosen = choose_model(model_ctx, ram_gb)

    print(f"System RAM: {ram_gb:.1f} GB", file=sys.stderr)
    print(f"Selected model: {chosen['name']} (needs {chosen['min_ram_gb']} GB, ~{chosen['download_gb']} GB download)", file=sys.stderr)

    if not dry_run:
        write_model_to_pipeline_config(chosen['name'])
        print(f"Wrote model choice to {PIPELINE_FILE}", file=sys.stderr)

    # setup_ollama.sh consumes this: pulls `base`, then builds the local
    # `name` variant (extended context) from it.
    print(f"{chosen['base']} {chosen['name']}")


if __name__ == '__main__':
    main()
