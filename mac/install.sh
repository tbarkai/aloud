#!/bin/bash
# Builds Aloud.app, installs it to ~/Applications and registers its
# "Read in Aloud" service. The app runs server.py from this checkout, so re-run this
# after moving the repo.
set -euo pipefail

MAC_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$MAC_DIR")"
BUILD="$MAC_DIR/build/Aloud.app"
DEST="$HOME/Applications/Aloud.app"

echo "Syncing Python environment…"
(cd "$ROOT" && uv sync --quiet)

echo "Building app…"
rm -rf "$BUILD"
mkdir -p "$BUILD/Contents/MacOS" "$BUILD/Contents/Resources"
swiftc -O -swift-version 5 -target arm64-apple-macos14 \
  "$MAC_DIR"/Sources/*.swift -o "$BUILD/Contents/MacOS/Aloud"
swiftc -O "$MAC_DIR/make-icon.swift" -o "$MAC_DIR/build/make-icon"
"$MAC_DIR/build/make-icon" "$MAC_DIR/build/AppIcon.iconset"
iconutil -c icns "$MAC_DIR/build/AppIcon.iconset" -o "$BUILD/Contents/Resources/AppIcon.icns"
cp "$MAC_DIR/Info.plist" "$BUILD/Contents/Info.plist"
plutil -replace ReaderProjectDir -string "$ROOT" "$BUILD/Contents/Info.plist"
codesign --force --sign - "$BUILD"

echo "Installing to ${DEST}…"
# Quit normally so the app stops its server, then clear any server left
# behind by a crash, so the new app doesn't reuse one running old code.
osascript -e 'quit app id "dev.local.Aloud"' 2>/dev/null || true
pkill -f "server.py --port 7861" 2>/dev/null && sleep 1 || true
mkdir -p "$HOME/Applications"
rm -rf "$DEST"
cp -R "$BUILD" "$DEST"
/System/Library/CoreServices/pbs -update
open "$DEST"

echo "Done. Select text in any app, right-click, and choose Read in Aloud"
echo "(it may be under Services). Logs: ~/Library/Logs/Aloud/server.log"
