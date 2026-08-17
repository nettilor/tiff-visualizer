#!/bin/bash
# Packages the distributable disk image into
# build/TIFF-Visualizer-<version>-macos.dmg: a drag-to-Applications window
# with the first-launch Gatekeeper steps drawn on the background.
#
# Usage: bash packaging/make_dmg.sh   (from the repo root, on macOS)
#
# The name is what the in-app update check looks for on a release
# (tiff_visualizer/updater.py) — keep the "macos" in it.
set -euo pipefail
cd "$(dirname "$0")/.."

bash packaging/build_macos.sh

APP="dist/TIFF Visualizer.app"
[ -d "$APP" ] || { echo "make_dmg: $APP is missing" >&2; exit 1; }

# The version that actually shipped in the bundle, not the one in the source
# tree — a stale build would otherwise be published under a fresh name.
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist")"
VOLUME="TIFF Visualizer ${VERSION}"
DMG="build/TIFF-Visualizer-${VERSION}-macos.dmg"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

STAGE="$WORK/stage"
mkdir -p "$STAGE/.background"
.venv/bin/python packaging/make_dmg_background.py "$WORK" >/dev/null
cp "$WORK/background.tiff" "$STAGE/.background/background.tiff"

ditto "$APP" "$STAGE/TIFF Visualizer.app"
ln -s /Applications "$STAGE/Applications"

RW="$WORK/rw.dmg"
hdiutil create -volname "$VOLUME" -srcfolder "$STAGE" -fs HFS+ -format UDRW -ov -quiet "$RW"
MOUNT="/Volumes/${VOLUME}"
if [ -d "$MOUNT" ]; then hdiutil detach "$MOUNT" -quiet || true; fi
hdiutil attach "$RW" -noautoopen -quiet

# The volume icon file goes in now so the layout script below can park it.
# Finder's "update" deletes it again, so it is restored after the window
# session, and the flag that activates it is set only then.
cp packaging/icon.icns "$MOUNT/.VolumeIcon.icns"

# Finder is what writes the icon layout into the volume's .DS_Store; the
# positions here must agree with the geometry in make_dmg_background.py.
osascript <<OSA || echo "(window layout skipped — the image still works, icons just land unarranged)"
tell application "Finder"
    tell disk "$VOLUME"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {200, 120, 840, 568}
        set opts to the icon view options of container window
        set arrangement of opts to not arranged
        set icon size of opts to 104
        set text size of opts to 12
        set background picture of opts to file ".background:background.tiff"
        set position of item "TIFF Visualizer.app" of container window to {150, 130}
        set position of item "Applications" of container window to {490, 130}
        -- Hidden housekeeping items still show for anyone with hidden files
        -- visible; park them well below the window.
        try
            set position of item ".background" of container window to {150, 800}
        end try
        try
            set position of item ".fseventsd" of container window to {320, 800}
        end try
        try
            set position of item ".VolumeIcon.icns" of container window to {490, 800}
        end try
        update without registering applications
        delay 1
        close
        delay 1
        open
        update without registering applications
        delay 2
    end tell
end tell
OSA

cp packaging/icon.icns "$MOUNT/.VolumeIcon.icns"
SetFile -a C "$MOUNT" 2>/dev/null || echo "(volume icon skipped — SetFile unavailable)"
sync
sleep 2
hdiutil detach "$MOUNT" -quiet
rm -f "$DMG"
hdiutil convert "$RW" -format UDZO -imagekey zlib-level=9 -o "$DMG" -quiet

echo
echo "Wrote $DMG"
echo "Check it with:    open \"$DMG\""
echo "Attach it to the release:"
echo "    gh release create v${VERSION} \"$DMG\" --title v${VERSION} --notes-file <notes>"
