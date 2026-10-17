#!/bin/bash
# Builds Kokoro Reader.app, installs it to ~/Applications and registers its
# "TTS" service. The app runs server.py from this checkout, so re-run this
# after moving the repo.
set -euo pipefail

MAC_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$MAC_DIR")"
BUILD="$MAC_DIR/build/Kokoro Reader.app"
DEST="$HOME/Applications/Kokoro Reader.app"

echo "Syncing Python environment…"
(cd "$ROOT" && uv sync --quiet)

echo "Building app…"
rm -rf "$BUILD"
mkdir -p "$BUILD/Contents/MacOS"
swiftc -O -swift-version 5 -target arm64-apple-macos14 \
  "$MAC_DIR"/Sources/*.swift -o "$BUILD/Contents/MacOS/KokoroReader"
cp "$MAC_DIR/Info.plist" "$BUILD/Contents/Info.plist"
plutil -replace ReaderProjectDir -string "$ROOT" "$BUILD/Contents/Info.plist"
codesign --force --sign - "$BUILD"

echo "Installing to $DEST…"
pkill -x KokoroReader 2>/dev/null && sleep 1 || true
mkdir -p "$HOME/Applications"
rm -rf "$DEST"
cp -R "$BUILD" "$DEST"
/System/Library/CoreServices/pbs -update
open "$DEST"

echo "Done. Select text in any app, right-click, and choose TTS"
echo "(it may be under Services). Logs: ~/Library/Logs/KokoroReader/server.log"
