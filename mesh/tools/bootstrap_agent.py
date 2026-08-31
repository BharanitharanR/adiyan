#!/usr/bin/env python3
"""
Bootstraps a new agent from mesh/example_agent/, so plugging in an agent
does not mean copying and renaming files by hand.

This mesh is not pip-installable today. There is no setup.py or
pyproject.toml anywhere in the repo, confirmed before this tool was
written rather than assumed. So this is a local generator script, not a
`pip install` plus package export. It does the same mechanical work a
real scaffolding tool would. It copies the reference agent, renames its
identity (directory, AGENT_ID, class name, display name), and picks a
free port. Those are the same four things you would otherwise edit by
hand in five different files.

It does not rename the example skill (roll_dice). That is your actual
logic to write, not something a generator should guess at.

Usage (run from the repo root):
    python3 -m mesh.tools.bootstrap_agent weather_agent
"""
import argparse
import re
import shutil
import socket
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_DIR = REPO_ROOT / 'mesh' / 'example_agent'
TEMPLATE_AGENT_ID = 'example_agent'
TEMPLATE_CLASS_PREFIX = 'ExampleAgent'
TEMPLATE_DISPLAY_NAME = 'Example Agent'


def _pascal_case(snake_name: str) -> str:
    return ''.join(part.capitalize() for part in snake_name.split('_'))


def _title_case(snake_name: str) -> str:
    return ' '.join(part.capitalize() for part in snake_name.split('_'))


def _next_free_port(start: int = 8440) -> int:
    # Scans every existing agent's constants.py for a PORT line, so a new
    # agent never collides with one already claimed, even one that is not
    # running right now. Then confirms the chosen port is not actually bound
    # by anything else on the machine either, retrying upward if it is.
    used = set()
    for constants_file in (REPO_ROOT / 'mesh').glob('*/constants.py'):
        text = constants_file.read_text()
        match = re.search(r'^PORT\s*=\s*(\d+)', text, re.MULTILINE)
        if match:
            used.add(int(match.group(1)))

    port = start
    while True:
        if port not in used:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.2)
                if sock.connect_ex(('127.0.0.1', port)) != 0:
                    return port
        port += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('name', help="New agent's id, snake_case, e.g. weather_agent")
    args = parser.parse_args()

    name = args.name
    if not re.fullmatch(r'[a-z][a-z0-9_]*', name):
        parser.error('Agent name must be snake_case: lowercase letters, digits, underscores, starting with a letter.')

    target_dir = REPO_ROOT / 'mesh' / name
    if target_dir.exists():
        parser.error(f"mesh/{name}/ already exists. Pick a different name.")

    port = _next_free_port()
    class_prefix = _pascal_case(name)
    display_name = _title_case(name)

    shutil.copytree(TEMPLATE_DIR, target_dir, ignore=shutil.ignore_patterns('__pycache__', 'README.md'))

    for path in target_dir.rglob('*.py'):
        text = path.read_text()
        text = text.replace(TEMPLATE_AGENT_ID, name)
        text = text.replace(TEMPLATE_CLASS_PREFIX, class_prefix)
        text = text.replace(TEMPLATE_DISPLAY_NAME, display_name)
        text = text.replace('PORT = 8440', f'PORT = {port}')
        path.write_text(text)

    print(f"Created mesh/{name}/ on port {port}.")
    print(f"Run it with: python3 -m mesh.{name}.server")
    print("It still has the example's roll_dice skill in it. Replace mesh/"
          f"{name}/skills/roll_dice.py and mesh/{name}/skills_catalog.py "
          "with your own logic and description.")


if __name__ == '__main__':
    main()
