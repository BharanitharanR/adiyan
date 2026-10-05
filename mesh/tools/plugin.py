#!/usr/bin/env python3
"""
Install and manage Adiyan plugins (see mesh/lib/plugins.py).

    python -m mesh.tools.plugin install <git URL or folder> [--yes]
    python -m mesh.tools.plugin list
    python -m mesh.tools.plugin update <id>
    python -m mesh.tools.plugin disable <id> | enable <id>
    python -m mesh.tools.plugin remove <id>

install: copies (or clones) the plugin into ~/.Adiyan/plugins/<module_root>, checks its
adiyan-plugin.json, shows what it asks for (permissions, gateway settings, secrets) and asks
before going on, installs its requirements.txt into Adiyan's own Python, creates any secrets it
declares that aren't set yet (never overwriting one), then starts it and refreshes the gateway.
remove keeps the plugin's data in ~/.Adiyan/<id>/; only its code is deleted.
"""
import json
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
from pathlib import Path

from mesh.lib import plugins
from mesh.lib.secrets_vault import get_secret, set_secret

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
START_ALL = REPO_ROOT / 'mesh' / 'start_all.sh'


def _fetch(source: str, into: Path) -> Path:
    """The plugin's files in a temporary folder: a git clone, or a copy of a local folder."""
    src = Path(source).expanduser()
    target = into / 'plugin'
    if src.is_dir():
        shutil.copytree(src, target, ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
    else:
        subprocess.run(['git', 'clone', '--depth', '1', '--quiet', source, str(target)], check=True)
    return target


def _manifest(folder: Path) -> dict:
    mf = folder / plugins.MANIFEST
    if not mf.exists():
        raise plugins.PluginError(f'no {plugins.MANIFEST} in {folder}')
    return plugins.validate(json.loads(mf.read_text()), plugins._core_tier_names())


def _restart(*names: str) -> None:
    subprocess.run([str(START_ALL), 'restart', 'nginx_gateway_watcher', *names], cwd=REPO_ROOT, check=False)


def _make_secret(spec: dict) -> None:
    name = spec['name']
    if get_secret(name):
        print(f'  secret {name}: already set, kept')
        return
    if spec.get('generate') == 'random40':
        alphabet = string.ascii_letters + string.digits
        set_secret(name, ''.join(secrets.choice(alphabet) for _ in range(40)))
        print(f'  secret {name}: created (stored in the vault, not shown)')
    else:
        print(f'  secret {name}: not set - set it with mesh.lib.secrets_vault before using the plugin')


def install(source: str, yes: bool) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        folder = _fetch(source, Path(tmp))
        m = _manifest(folder)
        print(f"\n{m['name']} {m['version']}  (id: {m['id']}, port {m['port']})")
        if m.get('description'):
            print(f"  {m['description']}")
        print('\nIt asks for:')
        print('  permissions: ' + (', '.join(m.get('permissions', [])) or 'none'))
        print('  gateway settings: ' + (' '.join(m.get('gateway', [])) or 'none'))
        print('  secrets: ' + (', '.join(s['name'] for s in m.get('secrets', [])) or 'none'))
        print('  needs: ' + (', '.join(m.get('requires', [])) or 'nothing extra'))
        if not yes and input('\nInstall it? [y/N] ').strip().lower() != 'y':
            print('Not installed.')
            return 1
        dest = plugins.PLUGINS_DIR / m['module_root']
        if dest.exists():
            shutil.rmtree(dest)
        plugins.PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copytree(folder, dest)
        (dest / '.source').write_text(source)
    req = dest / 'requirements.txt'
    if req.exists():
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', '-r', str(req)], check=True)
    for spec in m.get('secrets', []):
        _make_secret(spec)
    plugins._cache['at'] = 0
    print(f'\nInstalled to {dest}. Starting it...')
    _restart(m['id'])
    if m.get('after_install'):
        print('\nNext: ' + m['after_install'])
    return 0


def _find(plugin_id: str) -> dict:
    for mf in plugins.PLUGINS_DIR.glob(f'*/{plugins.MANIFEST}'):
        m = json.loads(mf.read_text())
        if m.get('id') == plugin_id:
            m['_dir'] = str(mf.parent)
            return m
    raise SystemExit(f'No plugin with id {plugin_id!r}. Try: python -m mesh.tools.plugin list')


def main(argv) -> int:
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__.strip())
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == 'install' and rest:
        return install(rest[0], '--yes' in rest)
    if cmd == 'list':
        any_found = False
        for mf in sorted(plugins.PLUGINS_DIR.glob(f'*/{plugins.MANIFEST}')):
            any_found = True
            m = json.loads(mf.read_text())
            state = 'disabled' if (mf.parent / 'DISABLED').exists() else 'enabled'
            try:
                plugins.validate(m, plugins._core_tier_names())
            except plugins.PluginError as e:
                state = f'INVALID ({e})'
            print(f"{m.get('id')}  {m.get('version')}  {state}  {mf.parent}")
        if not any_found:
            print(f'No plugins in {plugins.PLUGINS_DIR}')
        return 0
    if cmd in ('disable', 'enable', 'remove', 'update') and rest:
        m = _find(rest[0])
        d = Path(m['_dir'])
        if cmd == 'disable':
            (d / 'DISABLED').write_text('')
            subprocess.run([str(START_ALL), 'stop', m['id']], cwd=REPO_ROOT, check=False)
        elif cmd == 'enable':
            (d / 'DISABLED').unlink(missing_ok=True)
            _restart(m['id'])
        elif cmd == 'remove':
            subprocess.run([str(START_ALL), 'stop', m['id']], cwd=REPO_ROOT, check=False)
            shutil.rmtree(d)
            print(f"Removed {m['id']}'s code. Its data in ~/.Adiyan/{m['id']}/ is kept.")
        else:
            source = (d / '.source').read_text().strip() if (d / '.source').exists() else ''
            if not source:
                raise SystemExit('No recorded source to update from; install it again.')
            return install(source, '--yes' in rest)
        plugins._cache['at'] = 0
        return 0
    print(__doc__.strip())
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
