#!/bin/bash
# Build the self-contained macOS app bundle: dist/TIFF Visualizer.app
# Usage: bash packaging/build_macos.sh   (from the repo root)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e . pyinstaller pillow

# Regenerate icon assets if missing
[ -f packaging/icon.icns ] || .venv/bin/python packaging/make_icon.py

# The spec file carries the bundle config, including the Info.plist document
# types that let Finder/Dock accept dropped TIFF files.
.venv/bin/pyinstaller --noconfirm --clean packaging/tiffviz_macos.spec

echo
echo "Done: dist/TIFF Visualizer.app"
echo "First launch of an unsigned app: right-click the app > Open (once),"
echo "or run: xattr -cr 'dist/TIFF Visualizer.app'"
