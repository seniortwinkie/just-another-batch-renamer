#!/usr/bin/env bash
# Build JABR as a macOS app (.app) with PyInstaller.
#
#   bash build_macos.sh          (no arguments needed; run it ON a Mac)
#
# Needs: Python 3.9+ (python.org installer or Homebrew), internet access.
# Results:  dist/JABR.app                   <- the app
#           dist/JABR-macOS.zip             <- the same app, zipped for sharing
# Log:      build-macos/build.log           <- full output of the last run
#
# Optional environment variables:
#   BUNDLE_ID=com.yourname.jabr           app identifier (default: com.example.jabr)
#   TARGET_ARCH=universal2                universal2, arm64 or x86_64. Default is this Mac's own
#                                         architecture. universal2 needs a universal2 Python
#                                         (the python.org installer is; Homebrew's is not).
#   PYTHON=/path/to/python3               use a specific Python
#
# (Written to work with the old bash 3.2 that macOS ships.)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$HERE/build-macos"
DIST="$HERE/dist"
APPNAME="JABR"
APP="$DIST/$APPNAME.app"
ZIP="$DIST/JABR-macOS.zip"
BUNDLE_ID="${BUNDLE_ID:-com.example.jabr}"
STEP="starting up"

mkdir -p "$BUILD" "$DIST"
LOG="$BUILD/build.log"
: > "$LOG"
exec > >(tee -a "$LOG") 2>&1

say() { STEP="$*"; printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

on_error() {
  local code=$?
  echo
  echo "!! BUILD FAILED while: $STEP   (exit code $code, script line ${BASH_LINENO[0]})"
  echo "!! No app was created. Full output of this run: $LOG"
  echo "!! Common causes: no internet (PyPI blocked), or Python older than 3.9."
  sleep 1   # let the log writer flush before the prompt returns
}
trap on_error ERR

[ "$(uname -s)" = "Darwin" ] || die "this script builds the macOS app and must be run on a Mac"

say "Checking the project files"
missing=""
for f in jabr.py packaging/jabr.png icons/make_icons.py; do
  [ -f "$HERE/$f" ] || missing="$missing  $f"$'\n'
done
if [ -n "$missing" ]; then
  printf 'These files are missing (looked in %s):\n%s\n' "$HERE" "$missing"
  die "run this script from the folder that contains jabr.py, packaging/ and icons/"
fi

say "Checking Python"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || die "python3 not found - install it from python.org or with: brew install python"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Python 3.9 or newer is needed (found: $("$PY" -V 2>&1))"
echo "using $("$PY" -V 2>&1) at $(command -v "$PY")"

say "Setting up the build environment"
"$PY" -m venv "$BUILD/venv"
VPY="$BUILD/venv/bin/python"
"$VPY" -m pip install --quiet --upgrade pip
# PySide6-Essentials has everything the app uses, without the huge Addons package.
"$VPY" -m pip install --quiet pyinstaller "PySide6-Essentials~=6.5" pillow

say "Creating the app icon"
"$VPY" "$HERE/icons/make_icons.py" "$BUILD/icons"

say "Building $APPNAME.app with PyInstaller"
rm -rf "$APP" "${DIST:?}/$APPNAME" "$BUILD/pyi"
"$VPY" -m PyInstaller --noconfirm --clean --windowed \
  --name "$APPNAME" \
  --icon "$BUILD/icons/app.icns" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  ${TARGET_ARCH:+--target-architecture "$TARGET_ARCH"} \
  --add-data "$HERE/packaging/jabr.png:packaging" \
  --distpath "$DIST" --workpath "$BUILD/pyi" --specpath "$BUILD" \
  "$HERE/jabr.py"

say "Checking the app bundle"
[ -d "$APP" ] || die "PyInstaller finished but $APP was not created"
[ -x "$APP/Contents/MacOS/$APPNAME" ] || die "the app bundle has no executable inside"
rm -rf "${DIST:?}/$APPNAME"    # PyInstaller also leaves a plain folder next to the .app
if command -v lipo >/dev/null 2>&1; then
  echo "architectures: $(lipo -archs "$APP/Contents/MacOS/$APPNAME" 2>/dev/null || echo unknown)"
fi

say "Signing (ad-hoc, so it runs on this Mac)"
if command -v codesign >/dev/null 2>&1; then
  codesign --force --deep --sign - "$APP" \
    || echo "WARNING: ad-hoc signing failed; the app may still run on this Mac."
else
  echo "codesign not found (install the Xcode command line tools: xcode-select --install)"
fi

say "Zipping for sharing"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"

say "Done"
du -sh "$APP" "$ZIP"
echo
echo "Your app:  $APP"
echo "Run it:    open \"$APP\"     (or drag it into /Applications)"
echo
echo "On OTHER Macs the first launch is blocked because the app isn't notarized by Apple:"
echo "right-click it > Open > Open, or run:  xattr -dr com.apple.quarantine \"/Applications/$APPNAME.app\""
sleep 1
