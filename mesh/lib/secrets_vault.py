"""
OS Keychain-backed secret storage for Adiyan, via Python's `keyring` library
(macOS Keychain on this platform; Credential Manager on Windows, Secret
Service/libsecret on Linux - same API either way).

Replaces plaintext secret storage - a .env file for Google OAuth credentials, the
plaintext settings-table storage the OpenWA API key used to live in - with the
OS's own encrypted, login-protected credential store. No new service to run, no
additional attack surface beyond what the OS already protects; appropriate for
Adiyan's single-machine, local-first deployment (a dedicated secrets server like
HashiCorp Vault would be the right call for a server/team deployment, not this).

Keys are namespaced under one keyring "service" (SERVICE_NAME) so they all group
together under one entry in Keychain Access, distinguished by "account" name
(e.g. GOOGLE_OAUTH_CLIENT_ID, OPENWA_API_KEY).

Setting a secret is a one-time, owner-driven action - see tools/set_secret.py for
the interactive CLI. Nothing in the running service ever writes a secret; it only
ever reads.
"""
import logging
from typing import Optional

logger = logging.getLogger('SecretsVault')

SERVICE_NAME = 'Adiyan'


def get_secret(key: str) -> Optional[str]:
    """Returns None (not an error) if the key isn't set, or if keyring/the OS
    credential store isn't available for some reason - callers treat a missing
    secret as "not configured yet", the same graceful-absence pattern used
    throughout this codebase for every optional integration."""
    try:
        import keyring
        return keyring.get_password(SERVICE_NAME, key)
    except Exception as e:
        logger.warning(f"⚠️  Could not read '{key}' from the OS keychain: {e}")
        return None


def set_secret(key: str, value: str) -> None:
    """Stores (or overwrites) a secret. One-time setup, not a hot-path call."""
    import keyring
    keyring.set_password(SERVICE_NAME, key, value)


def delete_secret(key: str) -> None:
    import keyring
    try:
        keyring.delete_password(SERVICE_NAME, key)
    except Exception:
        pass  # already absent - nothing to do
