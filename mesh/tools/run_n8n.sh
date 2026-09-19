#!/bin/bash
# Launches n8n with its owner account pre-provisioned - see
# provision_n8n_owner.js's own docstring for why this exists (skipping the
# interactive "Set up owner account" browser wizard on a fresh install).
#
# A real wrapper script, not a single COMPONENTS array command line, because
# start_all.sh's own launch mechanism (`nohup $cmd`) passes $cmd straight to
# nohup as a program+arguments after word-splitting - it can't express
# `source` (a shell builtin, not an executable) or `&&` sequencing at all.
# Provisioning must run BEFORE n8n starts, so it has to be a script.
set -e
node "$(dirname "$0")/provision_n8n_owner.js"

# Single-quoted values in n8n_owner.env are what make this `source` safe -
# a bcrypt hash's own literal `$` characters would otherwise be expanded by
# this same `source` as positional-parameter references (confirmed live:
# n8n rejected the resulting mangled hash outright). Do not unquote that
# file without re-verifying this still holds.
set -a
source "$HOME/.Adiyan/n8n_owner.env"
set +a

exec n8n start
