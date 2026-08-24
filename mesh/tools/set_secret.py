#!/usr/bin/env python3
"""
One-time interactive setup: store a secret in mesh/lib/secrets_vault.py's
OS Keychain vault. Values are entered via getpass, so they're never echoed
to the terminal or saved in shell history.

Usage (run from the repo root):
    python3 -m mesh.tools.set_secret PERMISSIONS_JWT_SECRET
    python3 -m mesh.tools.set_secret CONFIG_DASHBOARD_PASSWORD

    python3 -m mesh.tools.set_secret --list                        # show which known keys are set (not their values)
    python3 -m mesh.tools.set_secret --delete CONFIG_DASHBOARD_PASSWORD
"""
import argparse
import getpass

from mesh.lib.secrets_vault import delete_secret, get_secret, set_secret

# Not an enforced allow-list (any key name works) - just what this CLI knows
# to report on for --list, so a typo'd key name doesn't silently vanish
# unnoticed.
KNOWN_KEYS = ['PERMISSIONS_JWT_SECRET', 'CONFIG_DASHBOARD_PASSWORD']


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('key', nargs='?', help='Secret name to set, e.g. CONFIG_DASHBOARD_PASSWORD')
    parser.add_argument('--list', action='store_true', help='Show which known keys are currently set')
    parser.add_argument('--delete', metavar='KEY', help='Remove a stored secret')
    args = parser.parse_args()

    if args.list:
        for key in KNOWN_KEYS:
            status = 'set' if get_secret(key) else 'not set'
            print(f'{key}: {status}')
        return

    if args.delete:
        delete_secret(args.delete)
        print(f"Removed '{args.delete}' from the vault (if it was set)")
        return

    if not args.key:
        parser.print_help()
        return

    value = getpass.getpass(f'Enter value for {args.key} (input hidden): ').strip()
    if not value:
        print('Empty value - nothing stored')
        return
    set_secret(args.key, value)
    print(f"Stored '{args.key}' in the Adiyan vault")


if __name__ == '__main__':
    main()
