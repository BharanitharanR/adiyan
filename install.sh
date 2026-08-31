#!/usr/bin/env bash
# Adiyan Platform - one-time setup for a fresh Mac.
#
# Sets up everything this mesh needs to run: a Python venv with its
# dependencies, the Qdrant binary, the OpenWA/Node dependencies, and the
# three Ollama models every agent's LLM stage relies on. Does NOT start any
# service itself - mesh/start_all.sh is the one thing that starts/stops
# processes, and that's a deliberate, separate, explicit step you run
# yourself once this script finishes.
#
# Run from the repo root:
#   ./install.sh
#
# Safe to re-run - every step checks whether its own work is already done
# before doing it again, the same idempotent shape mesh/start_all.sh's own
# "start is a no-op for something already running" already follows.
#
# 2026-08-30 incident fixes (all in source, so a fresh clone gets them for
# free - nothing this script needs to seed):
#   - mesh/scheduler/job_lookup.py: name_or_phrase lookup (used by
#     delete_job/run_routine) was reusing find_similar_job(), a function
#     built for exact-schedule dedup - crashed on a missing arg, and even
#     fixed would have wrongly excluded jobs on a different schedule. Added
#     db.find_job_by_name() (searches all jobs, no schedule filter) instead.
#   - mesh/scheduler/skills/run_routine.py + seed_config.json: a scheduled
#     job with nothing concrete to report (a vague description like "Log
#     progress each day at 6pm") reliably made compose_generic produce
#     unfilled "Hi [Name]!" template text. Prompt rewritten to forbid
#     placeholder address entirely, and _looks_like_unfilled_template()
#     now drops the message (stays silent) instead of sending it if one
#     slips through anyway.
#   - mesh/lib/permissions_config.json: mcp.whatsapp.send_message/
#     send_document/send_image removed from the 'service' tier (scheduler
#     and journal have no real WhatsApp identity and always mint this
#     tier) - a deliberate, current lockdown after a runaway-message
#     incident whose root cause was never fully isolated before shutdown.
#     Your own live @Adiyan chat replies are unaffected (owner tier).
#     Re-add those three lines once you've confirmed what was actually
#     sending and are ready to re-enable automated sends.
#   - mesh/lib/utilities/watermark.py: DEFAULT_TEXT confirmed correct
#     ('அடியான்') - the live config value had drifted to a different
#     spelling ('அடியேன்') at some point; reset via config_sdk.set_constant,
#     not something install.sh seeds.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

BOLD='\033[1m'
DIM='\033[2m'
RED='\033[31m'
GREEN='\033[32m'
RESET='\033[0m'

step() { echo -e "\n${BOLD}==> $1${RESET}"; }
ok()   { echo -e "${GREEN}  ok${RESET} - $1"; }
skip() { echo -e "${DIM}  skip${RESET} - $1"; }
fail() { echo -e "${RED}  missing${RESET} - $1"; MISSING=1; }

MISSING=0

step "Checking prerequisites"

# Everything below except Homebrew itself is auto-installed via brew when
# missing, not just reported - consistent with how the rest of this script
# already behaves (it pulls multi-GB Ollama models and clones penwa/
# without asking first). Homebrew itself is the one thing that stays a
# hard manual step: its own official installer is interactive and can
# prompt for a sudo password, which isn't something this script should
# run unattended on someone else's machine.
if ! command -v brew >/dev/null 2>&1; then
    fail "Homebrew not found - install it first: https://brew.sh"
else
    ok "Homebrew"
fi

if [ "$MISSING" -eq 1 ]; then
    echo -e "\n${RED}Homebrew is required before this script can install anything else - get it from https://brew.sh, then re-run ./install.sh.${RESET}"
    exit 1
fi

PYTHON_BIN=""
# Upper-bounded at 3.13, not open-ended "3.11+" - confirmed live: a
# machine whose only python3 was 3.14 passed the old "minor >= 11" check,
# started the install, and then failed deep inside pip with
# "Could not find a version that satisfies the requirement
# llama-index-vector-stores-qdrant>=0.10.0", since that package (and
# several others in requirements.txt) haven't published anything
# compatible with 3.14 yet.
find_compatible_python() {
    for candidate in python3.11 python3.12 python3.13 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            version="$("$candidate" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
            major="${version%%.*}"
            minor="${version##*.}"
            if [ "$major" -eq 3 ] && [ "$minor" -ge 11 ] && [ "$minor" -le 13 ]; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}
if PYTHON_BIN="$(find_compatible_python)"; then
    ok "Python ($PYTHON_BIN, $("$PYTHON_BIN" --version 2>&1))"
else
    echo -e "${DIM}  installing${RESET} - Python 3.13 (brew install python@3.13)"
    brew install python@3.13
    if PYTHON_BIN="$(find_compatible_python)"; then
        ok "Python ($PYTHON_BIN, $("$PYTHON_BIN" --version 2>&1))"
    else
        fail "Installed python@3.13 via brew, but no python3.11-3.13 is on PATH yet - open a new terminal (or run 'brew link python@3.13') and re-run ./install.sh."
    fi
fi

if ! command -v node >/dev/null 2>&1; then
    echo -e "${DIM}  installing${RESET} - Node.js (brew install node)"
    brew install node
fi
if command -v node >/dev/null 2>&1; then
    ok "Node ($(node --version))"
else
    fail "brew install node ran but node still isn't on PATH - open a new terminal and re-run ./install.sh."
fi

if ! command -v mongod >/dev/null 2>&1; then
    echo -e "${DIM}  installing${RESET} - MongoDB (brew tap mongodb/brew && brew trust mongodb/brew && brew install mongodb-community)"
    brew tap mongodb/brew
    # brew trust is required by current Homebrew versions before loading a
    # formula from a non-official tap - confirmed live: 'brew install
    # mongodb-community' on a fresh machine failed with 'Refusing to load
    # formula ... from untrusted tap mongodb/brew', and the fix Homebrew
    # itself suggests is exactly this trust step, not a workaround.
    brew trust mongodb/brew
    brew install mongodb-community
fi
if command -v mongod >/dev/null 2>&1; then
    ok "MongoDB"
else
    fail "brew install mongodb-community ran but mongod still isn't on PATH - open a new terminal and re-run ./install.sh."
fi

if ! command -v ollama >/dev/null 2>&1; then
    echo -e "${DIM}  installing${RESET} - Ollama (brew install ollama)"
    brew install ollama
fi
if command -v ollama >/dev/null 2>&1; then
    ok "Ollama"
else
    fail "brew install ollama ran but ollama still isn't on PATH - open a new terminal and re-run ./install.sh."
fi

if [ "$MISSING" -eq 1 ]; then
    echo -e "\n${RED}One or more prerequisites couldn't be finished automatically - see the messages above, then re-run ./install.sh.${RESET}"
    exit 1
fi

step "Python virtual environment"
if [ -d ".venv" ]; then
    skip ".venv already exists"
else
    "$PYTHON_BIN" -m venv .venv
    ok "Created .venv"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
ok "Python dependencies installed"

step "Qdrant binary"
if [ -x "mesh/qdrant/qdrant-bin" ]; then
    skip "mesh/qdrant/qdrant-bin already present"
else
    bash mesh/qdrant/fetch_binary.sh
fi

step "OpenWA (WhatsApp)"
# penwa/ is a separate repo (rmyndharis/OpenWA), deliberately excluded from
# this one via .gitignore - a fresh clone of Adiyan has no penwa/ directory
# at all. Confirmed live: install.sh silently assumed it already existed
# (true only on a machine that already had a manual checkout) and failed
# with a bare "no such file" from npm on a genuinely fresh clone.
if [ -d "penwa/.git" ]; then
    skip "penwa/ already present"
else
    git clone https://github.com/rmyndharis/OpenWA penwa
    ok "Cloned penwa/"
fi

# A real launch-and-close, not just "npm install exited 0" - confirmed
# live that the two are different questions. One failure mode surfaces as
# a non-zero npm exit code ("the executable ... is missing"). A second,
# worse one doesn't: npm reported success while the actual downloaded
# Chrome binary was corrupted, and the only symptom was OpenWA failing at
# runtime with "dlopen ... (no such file)" deep inside a framework
# bundle - node_modules and the top-level .app folder both looked
# perfectly normal. Puppeteer's own launch() is the one check that
# actually exercises the binary the way OpenWA will.
# Optional $1: a specific Chrome binary to launch instead of Puppeteer's
# own downloaded one - used by the real-Chrome fallback below, which
# needs to verify that specific executable actually works before
# committing to it, the same way the default path gets verified.
verify_puppeteer_browser() {
    local executable_path="${1:-}"
    node -e "
require('./penwa/node_modules/puppeteer').launch({
  headless: true,
  ${executable_path:+executablePath: '$executable_path',}
})
  .then(async b => { await b.close(); process.exit(0); })
  .catch(() => process.exit(1));
" >/dev/null 2>&1
}

# A previous run may have already fallen back to a real installed Chrome
# (see the PUPPETEER_EXECUTABLE_PATH branch below) - checked first so
# re-running install.sh doesn't redo that whole dance every time.
CONFIGURED_CHROME="$(grep "^PUPPETEER_EXECUTABLE_PATH=" penwa/.env 2>/dev/null | tail -1 | cut -d= -f2- || true)"

if [ -d "penwa/node_modules" ] && verify_puppeteer_browser "$CONFIGURED_CHROME"; then
    skip "penwa/node_modules already present, Chrome launches"
else
    # Cleared before the first attempt too, not just after one fails - a
    # stale, corrupted cache left behind by an earlier interrupted run
    # (network drop, Ctrl-C, a killed terminal) sits there silently and
    # can poison this very first attempt the exact same way it poisoned
    # the one that created it. Safe either way: this is only ever a
    # download cache, never anything with real data in it.
    rm -rf "$HOME/.cache/puppeteer"
    # Confirmed live: this step's own spinner gives no progress percentage
    # and no size estimate while it silently pulls down a real, large
    # (~200MB+) Chrome binary - on a slow connection that looks
    # indistinguishable from hung, and Ctrl-C'ing out of it is exactly
    # what leaves the corrupted half-downloaded folder this whole
    # verify-and-retry block exists to catch in the first place. Said
    # here, once, before the step that actually looks stuck.
    echo -e "${DIM}  Downloading Chrome for WhatsApp automation (~200MB) - this can take a few minutes with no visible progress. Don't interrupt it.${RESET}"
    # Up to two attempts: a plain retry first (handles a non-zero npm exit
    # code), then - if npm looked fine but the browser still won't launch -
    # a harder reset that clears both the download cache and node_modules,
    # since Puppeteer's postinstall only re-downloads when it doesn't
    # already think a browser is present.
    #
    # npm's own exit code is tracked separately from verify_puppeteer_browser
    # now, not just discarded with `|| true` - confirmed live that the two
    # can disagree: Puppeteer downloads a *second* browser besides the one
    # verify_puppeteer_browser launches (chrome-headless-shell), and a
    # corrupted extraction of that one still fails the overall `npm install`
    # even though the Chrome build verify_puppeteer_browser checks launches
    # fine. `|| true` swallowed that failure, verify_puppeteer_browser
    # reported success, and install.sh moved on having actually left
    # node_modules incomplete - the real symptom downstream was OpenWA's
    # `nest` binary (a devDependency) missing at startup. Either signal
    # failing now triggers the same retry path.
    npm_ok=1
    npm --prefix penwa install && npm_ok=1 || npm_ok=0
    if [ "$npm_ok" -eq 0 ] || ! verify_puppeteer_browser; then
        echo -e "${DIM}  penwa install didn't finish cleanly - clearing Puppeteer's cache and retrying${RESET}"
        rm -rf "$HOME/.cache/puppeteer"
        npm --prefix penwa install && npm_ok=1 || npm_ok=0
    fi
    if [ "$npm_ok" -eq 0 ] || ! verify_puppeteer_browser; then
        echo -e "${DIM}  still failing - clearing node_modules too (Puppeteer's postinstall only redownloads for a fresh install) and retrying once more${RESET}"
        rm -rf "$HOME/.cache/puppeteer" penwa/node_modules
        npm --prefix penwa install
        npm_ok=1
    fi
    if [ "$npm_ok" -eq 1 ] && verify_puppeteer_browser; then
        ok "penwa dependencies installed, Chrome launches"
    else
        # Confirmed live: on at least one real machine, none of the above
        # ever fixed it - `find` showed Contents/Frameworks/ missing from
        # the extracted .app entirely, on every single attempt, which
        # rules out a one-off truncated download. That pattern points at
        # something on the machine actively interfering with this one
        # specific nested directory during extraction (security/antivirus
        # software silently stripping an ad-hoc-signed nested framework is
        # the known cause for this exact symptom on macOS), not Puppeteer
        # or this repo. Rather than keep retrying a download that's
        # already been shown not to work, fall back to a real, already-
        # installed, properly-signed Chrome if one exists.
        REAL_CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if [ -x "$REAL_CHROME" ] && verify_puppeteer_browser "$REAL_CHROME"; then
            echo -e "${DIM}  Puppeteer's own downloaded Chrome won't launch, but a real installed Chrome does - using that instead${RESET}"
            if ! grep -q "^PUPPETEER_EXECUTABLE_PATH=" penwa/.env 2>/dev/null; then
                echo "PUPPETEER_EXECUTABLE_PATH=$REAL_CHROME" >> penwa/.env
            fi
            ok "penwa dependencies installed, using your installed Chrome"
        else
            fail "penwa's Chrome still won't launch after two clean-and-retry attempts, and no usable installed Chrome was found to fall back to - check the output above and https://pptr.dev/troubleshooting by hand"
        fi
    fi
fi

# The dashboard is a separate npm project (penwa/dashboard/), not an npm
# workspace of the root one - `npm --prefix penwa install` above never
# touches it. Confirmed live: without this, Config Server's own /register
# route (mesh/config_server/server.py) redirects correctly to OpenWA's
# dashboard at http://localhost:2785, which then 404s on every path,
# since there's no built UI to serve there at all - the exact dead end a
# first-time WhatsApp linking flow cannot recover from on its own. `npm
# start` itself doesn't need this (Nest compiles on the fly), so only the
# dashboard half of `build:all` is actually required here, not a full
# `nest build` too.
if [ -d "penwa/dashboard/dist" ]; then
    skip "penwa dashboard already built"
else
    npm --prefix penwa/dashboard install
    npm --prefix penwa run dashboard:build
    ok "penwa dashboard built"
fi

step "Ollama models"
# Base models pulled from Ollama's own library. qwen3:8b-16k is not a
# stock tag - it's this deployment's own 16k-context variant of qwen3:8b
# (every agent's own runtime_config.json/seed_config.json assumes this
# exact tag exists), built here via a Modelfile rather than pulled.
pull_if_missing() {
    local model="$1"
    if ollama list | awk '{print $1}' | grep -qx "$model"; then
        skip "$model already pulled"
    else
        echo "Pulling $model (this can take a while - it's a multi-GB download)..."
        ollama pull "$model"
    fi
}
pull_if_missing "qwen3:8b"
pull_if_missing "qwen3-vl:8b"
pull_if_missing "nomic-embed-text"

if ollama list | awk '{print $1}' | grep -qx "qwen3:8b-16k"; then
    skip "qwen3:8b-16k already built"
else
    echo "Building qwen3:8b-16k (qwen3:8b with a 16384-token context window)..."
    modelfile="$(mktemp)"
    printf 'FROM qwen3:8b\nPARAMETER num_ctx 16384\n' > "$modelfile"
    ollama create qwen3:8b-16k -f "$modelfile"
    rm -f "$modelfile"
    ok "Built qwen3:8b-16k"
fi

step "Secrets"
echo -e "${DIM}  This mesh stores secrets in the macOS Keychain, not a .env file. Set one with:${RESET}"
echo -e "${DIM}    .venv/bin/python3 -m mesh.tools.set_secret PERMISSIONS_JWT_SECRET${RESET}"
echo -e "${DIM}  See which known keys are already set: .venv/bin/python3 -m mesh.tools.set_secret --list${RESET}"

echo -e "\n${GREEN}${BOLD}Setup complete.${RESET}"
echo -e "Next: run ${BOLD}mesh/start_all.sh${RESET} when you're ready to start the mesh - this script deliberately doesn't start anything itself."
echo -e "\n${DIM}Note: OpenWA generates its own dashboard login key the first time it actually starts (this script never starts"
echo -e "anything, so it doesn't exist yet). Once mesh/start_all.sh has run, find it with:"
echo -e "  cat penwa/data/.api-key"
echo -e "That's what the dashboard's login screen (opened automatically for first-time WhatsApp linking) is asking for -"
echo -e "confirmed live: without knowing to look here, that login screen just says \"the key is invalid\" with no hint"
echo -e "where a real one comes from.${RESET}"
echo -e "\n${DIM}Note: automated WhatsApp sends (scheduler, journal) are currently locked down in mesh/lib/permissions_config.json"
echo -e "following a 2026-08-30 runaway-message incident. Your own live @Adiyan chat replies still work. See this script's"
echo -e "header comment for what changed and what to check before re-enabling.${RESET}"
