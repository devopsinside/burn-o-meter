#!/usr/bin/env bash
#
# Build burn-o-meter.app.
#
# Produces a normal macOS app bundle from the Swift package. The bundle is named
# burn-o-meter.app to match the project everywhere a user can see it; the
# executable inside keeps the Swift product name, which nobody looks at. The app is a
# LSUIElement (agent) app: no Dock icon, no menu bar menu — it lives entirely in
# the status bar, which is what a meter should do.
#
# Ad-hoc signing is applied so the app runs on the machine that built it.
# Distributing it to anyone else requires a Developer ID and notarisation; see
# README.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG="$HERE/burn-o-meter"
INSTALL_TO_APPLICATIONS=0
if [ "${1:-}" = "--install" ]; then
  INSTALL_TO_APPLICATIONS=1
  shift
fi
APP="${1:-$HERE/build/burn-o-meter.app}"
# Read from the package rather than hardcoding. A literal default here went stale
# the moment 0.2.0 shipped and quietly labelled every later build "0.1.0" - a
# version string that lies is worse than one that is missing, because nobody
# doubts it. Fail loudly instead.
VERSION="${BURNOMETER_VERSION:-$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$HERE/../src/burnometer/__init__.py")}"
if [ -z "$VERSION" ]; then
  echo "could not read __version__ from src/burnometer/__init__.py" >&2
  exit 1
fi

echo "==> building (release)"
swift build --package-path "$PKG" -c release

BIN="$(swift build --package-path "$PKG" -c release --show-bin-path)/burn-o-meter"
[ -x "$BIN" ] || { echo "build produced no binary at $BIN" >&2; exit 1; }

echo "==> assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/burn-o-meter"

# Without an icon the app shows a blank page in Finder, the Dock and Spotlight,
# which makes it look unfinished and hard to find by eye. Regenerate with
# `swift macos/make-icon.swift` if the artwork changes.
if [ -f "$HERE/burn-o-meter.icns" ]; then
  cp "$HERE/burn-o-meter.icns" "$APP/Contents/Resources/burn-o-meter.icns"
fi

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>                  <string>burn-o-meter</string>
    <key>CFBundleDisplayName</key>           <string>burn-o-meter</string>
    <key>CFBundleIdentifier</key>            <string>com.burn-o-meter.app</string>
    <key>CFBundleVersion</key>               <string>$VERSION</string>
    <key>CFBundleShortVersionString</key>    <string>$VERSION</string>
    <key>CFBundlePackageType</key>           <string>APPL</string>
    <key>CFBundleExecutable</key>            <string>burn-o-meter</string>
    <key>CFBundleIconFile</key>              <string>burn-o-meter</string>
    <key>LSMinimumSystemVersion</key>        <string>14.0</string>
    <!-- Status-bar only: no Dock icon, no app menu. -->
    <key>LSUIElement</key>                   <true/>
    <key>NSHumanReadableCopyright</key>      <string>MIT licensed</string>
    <!-- The app reads one JSON file under the user's home and makes no network
         requests. There is nothing here to justify any additional entitlement. -->
</dict>
PLIST
echo "</plist>" >> "$APP/Contents/Info.plist"

printf 'APPL????' > "$APP/Contents/PkgInfo"

echo "==> signing (ad-hoc)"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 \
    || echo "    ad-hoc signing failed; the app will still run locally"

echo
# Install into /Applications when asked. This is not tidiness: macOS refuses to
# register a login item for a bundle outside /Applications, so an app left in the
# build directory can never start itself. It appears to work until the first
# reboot, after which the only way back is Spotlight.
if [ "${INSTALL_TO_APPLICATIONS:-0}" = "1" ]; then
  DEST="/Applications/$(basename "$APP")"
  if [ -e "$DEST" ]; then
    pkill -f "$DEST/Contents/MacOS/" 2>/dev/null || true
    sleep 1
    rm -rf "$DEST"
  fi
  cp -R "$APP" "$DEST"
  APP="$DEST"
  echo "installed: $DEST"
fi

echo "built: $APP"
echo "run:   open '$APP'"
echo
echo "Note: this bundle is ad-hoc signed and will only run on this machine."
echo "Distribution needs a Developer ID certificate plus notarisation."
