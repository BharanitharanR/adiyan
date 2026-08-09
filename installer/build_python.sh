#!/usr/bin/env bash
# Build the Adiyan orchestrator into a single native executable.
#
# Builds from an isolated venv containing ONLY requirements.txt - building
# from a general-purpose Python install (e.g. Anaconda's base env) drags in
# hundreds of unrelated packages that PyInstaller's static analysis picks up,
# which has actually broken the build here before (conflicting Qt bindings
# pulled in transitively by something in a full Anaconda environment).
set -euo pipefail

INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$INSTALLER_DIR")"
VENV_DIR="$INSTALLER_DIR/build_venv"

log() { echo "[build_python] $*" >&2; }

log "Setting up clean build venv..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r "$PROJECT_DIR/requirements.txt"
pip install --quiet pyinstaller

log "Building executable..."
cd "$PROJECT_DIR"
python3 -m PyInstaller --onefile --name adiyan \
    --add-data "$PROJECT_DIR/ui/dashboard.html:ui" \
    --distpath "$INSTALLER_DIR/dist" \
    --workpath "$INSTALLER_DIR/build" \
    --specpath "$INSTALLER_DIR" \
    main.py

log "Built: $INSTALLER_DIR/dist/adiyan"
