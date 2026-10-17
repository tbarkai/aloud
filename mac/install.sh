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
# AppIcon.png is a full-bleed 1024px icon; shrink it onto the macOS icon grid
# (824px body on a 1024px canvas) so it sits at the same size as other Dock icons.
ICONSET="$MAC_DIR/build/AppIcon.iconset"
rm -rf "$ICONSET" && mkdir -p "$ICONSET"
sips -Z 824 "$MAC_DIR/AppIcon.png" --out "$MAC_DIR/build/icon-body.png" >/dev/null
sips -p 1024 1024 "$MAC_DIR/build/icon-body.png" --out "$MAC_DIR/build/icon-1024.png" >/dev/null
for base in 16 32 128 256 512; do
  sips -z $base $base "$MAC_DIR/build/icon-1024.png" --out "$ICONSET/icon_${base}x${base}.png" >/dev/null
  sips -z $((base * 2)) $((base * 2)) "$MAC_DIR/build/icon-1024.png" --out "$ICONSET/icon_${base}x${base}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$BUILD/Contents/Resources/AppIcon.icns"
cp "$MAC_DIR/Info.plist" "$BUILD/Contents/Info.plist"
plutil -replace ReaderProjectDir -string "$ROOT" "$BUILD/Contents/Info.plist"
codesign --force --sign - "$BUILD"

echo "Installing to ${DEST}…"
# Quit normally so the app stops its server. (A server whose app crashed exits
# on its own; builds before that left them running on port 7861.)
osascript -e 'quit app id "dev.local.Aloud"' 2>/dev/null || true
pkill -f "server.py --port 7861" 2>/dev/null || true
mkdir -p "$HOME/Applications"
rm -rf "$DEST"
cp -R "$BUILD" "$DEST"
/System/Library/CoreServices/pbs -update
open "$DEST"

echo "Done. Select text in any app, right-click, and choose Read in Aloud"
echo "(it may be under Services). Logs: ~/Library/Logs/Aloud/server.log"
