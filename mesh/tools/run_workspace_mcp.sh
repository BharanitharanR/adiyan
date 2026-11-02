#!/bin/bash
# Launches workspace-mcp (Gmail/Calendar/Drive/Docs/Sheets/Slides/Tasks/
# Contacts/Chat/Forms/Search - every service the package has, full
# read/write, no --tools/--permissions/--read-only flags at all) as a
# persistent streamable-HTTP server, OAuth redirect pointed at Adiyan's own
# public ngrok tunnel - see mesh/nginx/generate_config.py's SERVICE_ROUTES
# entry for /oauth2callback, which proxies that path back to this process's
# own port.
#
# A real wrapper script, not a single COMPONENTS array command line, for
# the same reason run_n8n.sh is one: the redirect URI depends on ngrok's
# CURRENT public URL, which only exists once ngrok is actually up - it has
# to be read live, at the moment this script runs, not baked into a static
# string start_all.sh's own COMPONENTS array would otherwise need.
#
# Google's OAuth redirect URI must be registered EXACTLY (scheme, host,
# path) in that OAuth client's own "Authorized redirect URIs" list in
# Google Cloud Console - and ngrok's free tier hands out a brand-new random
# URL every restart (same instability mesh/mcp/whatsapp/register_webhook.py
# already documents for itself), so that registration goes stale on every
# restart until it's updated there by hand. Accepted tradeoff, not a bug -
# see the Wayfinder build notes for why a paid static ngrok domain is the
# real fix if this becomes a hassle.
set -e

CLIENT_ID=$("$(dirname "$0")/../../.venv/bin/python3" -c "from mesh.lib.secrets_vault import get_secret; print(get_secret('GOOGLE_OAUTH_CLIENT_ID') or '')")
CLIENT_SECRET=$("$(dirname "$0")/../../.venv/bin/python3" -c "from mesh.lib.secrets_vault import get_secret; print(get_secret('GOOGLE_OAUTH_CLIENT_SECRET') or '')")
if [ -z "$CLIENT_ID" ] || [ -z "$CLIENT_SECRET" ]; then
    echo "GOOGLE_OAUTH_CLIENT_ID/GOOGLE_OAUTH_CLIENT_SECRET not set in the vault - see mesh/tools/set_secret.py. Exiting." >&2
    exit 1
fi

TUNNEL_JSON=$(curl -s http://127.0.0.1:4040/api/tunnels)
PUBLIC_URL=$(echo "$TUNNEL_JSON" | python3 -c "
import json, sys
tunnels = json.load(sys.stdin)['tunnels']
https = next((t for t in tunnels if t['proto'] == 'https'), None)
print(https['public_url'] if https else '')
")
if [ -z "$PUBLIC_URL" ]; then
    echo "No https ngrok tunnel found at 127.0.0.1:4040 - is 'ngrok http 8081' running? Exiting." >&2
    exit 1
fi

WORKSPACE_MCP_PORT="${WORKSPACE_MCP_PORT:-8447}"
echo "workspace-mcp OAuth redirect URI: ${PUBLIC_URL}/oauth2callback"
echo "If this is a NEW ngrok URL since the last restart, add it to this OAuth client's Authorized redirect URIs in Google Cloud Console before anyone tries to connect."

export GOOGLE_OAUTH_CLIENT_ID="$CLIENT_ID"
export GOOGLE_OAUTH_CLIENT_SECRET="$CLIENT_SECRET"
export GOOGLE_OAUTH_REDIRECT_URI="${PUBLIC_URL}/oauth2callback"
export WORKSPACE_MCP_PORT="$WORKSPACE_MCP_PORT"
# --single-user mode still needs to know WHOSE credentials to look up/start
# OAuth for - without this, auth/service_decorator.py's
# _get_configured_user_google_email() returns None and every tool call
# (including the very first one that's supposed to hand back the Google
# consent URL) hard-fails with "'user_google_email' parameter is required
# but was not found" instead of ever reaching the OAuth-start path.
export USER_GOOGLE_EMAIL="bharani3ran@gmail.com"


# Explicit venv path, not a bare `workspace-mcp` - confirmed live this
# session that a bare name can resolve to a DIFFERENT Python installation's
# copy (e.g. Anaconda's, if it's earlier on PATH) with no indication
# anything was wrong, the exact failure class $PYTHON_BIN already exists
# to close off for every other component here.
exec "$(dirname "$0")/../../.venv/bin/workspace-mcp" --transport streamable-http --single-user
