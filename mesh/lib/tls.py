"""
Self-signed TLS cert generation, shared by any mesh/ component that wants to
serve HTTPS directly rather than relying on an external tunnel (ngrok, etc.)
for encryption in transit. Generated once per install, reused after -
regenerating on every restart would invalidate anything that had pinned/
trusted the previous cert.

Does not solve reachability - a self-signed cert on 127.0.0.1 is still
127.0.0.1. OpenWA's own SSRF guard blocks by destination address, not by
scheme, so this is independent of (and doesn't replace) whatever gets a
public URL to the endpoint (ngrok today). This is about the endpoint itself
speaking TLS, not about who can reach it.
"""
import subprocess
from pathlib import Path
from typing import Tuple


def ensure_self_signed_cert(tls_dir: Path, common_name: str = 'localhost') -> Tuple[Path, Path]:
    """Returns (cert_path, key_path), generating a self-signed cert via the
    system's openssl if one doesn't already exist at tls_dir. SAN includes
    both localhost and 127.0.0.1 - a cert with only a CN (no SAN) is
    rejected outright by modern TLS clients, not just warned about."""
    tls_dir.mkdir(parents=True, exist_ok=True)
    cert_path = tls_dir / 'cert.pem'
    key_path = tls_dir / 'key.pem'

    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    subprocess.run(
        [
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
            '-keyout', str(key_path), '-out', str(cert_path),
            '-days', '825', '-nodes',
            '-subj', f'/CN={common_name}',
            '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
        ],
        check=True,
        capture_output=True,
    )
    return cert_path, key_path
