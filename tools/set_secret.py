#!/usr/bin/env python3
"""
One-time interactive setup: store a secret in Adiyan's OS Keychain vault
(config/secrets_vault.py). Values are entered via getpass, so they're never
echoed to the terminal or saved in shell history.

Usage:
    python3 tools/set_secret.py GOOGLE_OAUTH_CLIENT_ID
    python3 tools/set_secret.py GOOGLE_OAUTH_CLIENT_SECRET
    python3 tools/set_secret.py OPENWA_API_KEY

    python3 tools/set_secret.py --list                 # show which known keys are set (not their values)
    python3 tools/set_secret.py --delete OPENWA_API_KEY # remove a stored secret
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.secrets_vault import get_secret, set_secret, delete_secret

# Not an enforced allow-list (any key name works) - just what this CLI knows to
# report on for --list, so a typo'd key name doesn't silently vanish unnoticed.
KNOWN_KEYS = ['GOOGLE_OAUTH_CLIENT_ID', 'GOOGLE_OAUTH_CLIENT_SECRET', 'OPENWA_API_KEY',
              'DASHBOARD_USERNAME', 'DASHBOARD_PASSWORD']


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('key', nargs='?', help='Secret name to set, e.g. GOOGLE_OAUTH_CLIENT_ID')
    parser.add_argument('--list', action='store_true', help='Show which known keys are currently set')
    parser.add_argument('--delete', metavar='KEY', help='Remove a stored secret')
    args = parser.parse_args()

    if args.list:
        for key in KNOWN_KEYS:
            status = '✅ set' if get_secret(key) else '⚠️  not set'
            print(f"{key}: {status}")
        return

    if args.delete:
        delete_secret(args.delete)
        print(f"✅ Removed '{args.delete}' from the vault (if it was set)")
        return

    if not args.key:
        parser.print_help()
        return

    value = getpass.getpass(f"Enter value for {args.key} (input hidden): ").strip()
    if not value:
        print("❌ Empty value - nothing stored")
        return
    set_secret(args.key, value)
    print(f"✅ Stored '{args.key}' in the Adiyan vault (macOS Keychain)")


if __name__ == '__main__':
    main()
