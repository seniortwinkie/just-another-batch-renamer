#!/usr/bin/env bash
# Build JABR as a Linux AppImage.
#
#   bash build_appimage.sh          (no arguments needed)
#
# Needs: python3 (with venv), curl, ~1.5 GB free disk space, internet access
# (GitHub + PyPI).
#
# Result:   dist/jabr-<arch>.AppImage              <- the AppImage is HERE
# Log:      build/build.log                        <- full output of the last run
# (build/ only holds intermediate files, including the unpacked AppDir.)
#
# Optional environment variables:
#   PYVER=3.12            Python version to bundle
#   BASE_IMAGE=/path/to/python3.12.X-cp312-cp312-manylinux_2_28_x86_64.AppImage
#                         use a python-appimage base image you already downloaded
#                         (keep its original file name)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCH="$(uname -m)"                          # x86_64 or aarch64
PYVER="${PYVER:-3.12}"
LINUX_TAG="${LINUX_TAG:-manylinux_2_28_${ARCH}}"   # PySide6 wheels need glibc >= 2.28
BUILD="$HERE/build"
DIST="$HERE/dist"
APPNAME="JABR"
APPDIR="$BUILD/AppDir"
LOG="$BUILD/build.log"
STEP="starting up"
export PATH="$PATH:/sbin:/usr/sbin"         # so a normal user can find ldconfig

mkdir -p "$BUILD" "$DIST" "$BUILD/tmp"
# Use a temp folder on the real disk: /tmp is often a small RAM disk (tmpfs) or
# mounted noexec, either of which breaks building a ~300 MB Python + Qt bundle.
export TMPDIR="$BUILD/tmp"
# Lets the AppImage tools run without FUSE (works in containers and CI too).
export APPIMAGE_EXTRACT_AND_RUN=1

# Keep a copy of everything printed, so a failure can be diagnosed afterwards.
: > "$LOG"
exec > >(tee -a "$LOG") 2>&1

say() { STEP="$*"; printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

on_error() {
  local code=$?
  echo
  echo "!! BUILD FAILED while: $STEP   (exit code $code, script line ${BASH_LINENO[0]})"
  echo "!! No AppImage was created. Full output of this run: $LOG"
  echo "!! Common causes:"
  echo "!!   - no internet, or GitHub / PyPI blocked (corporate proxy or firewall)"
  echo "!!   - GitHub rate limit (HTTP 403): wait a while, or download the base image"
  echo "!!     yourself and run:  BASE_IMAGE=/path/to/file.AppImage bash build_appimage.sh"
  echo "!!   - not enough free disk space (needs about 1.5 GB)"
  sleep 0.5   # let the log writer flush before the shell prompt returns
}
trap on_error ERR

command -v python3 >/dev/null || die "python3 is required"
command -v curl    >/dev/null || die "curl is required"

say "Checking the project files"
# python-appimage silently skips recipe files it can't find and would then build a
# useless AppImage (a bare Python without PySide6), so check them here first.
missing=()
for f in jabr.py packaging/entrypoint.sh packaging/jabr.desktop \
         packaging/jabr.png packaging/requirements.txt; do
  [[ -f "$HERE/$f" ]] || missing+=("$f")
done
if (( ${#missing[@]} > 0 )); then
  echo "These files are missing (looked in $HERE):"
  printf '  %s\n' "${missing[@]}"
  echo
  echo "build_appimage.sh needs this layout:"
  echo "  build_appimage.sh"
  echo "  jabr.py"
  echo "  packaging/entrypoint.sh"
  echo "  packaging/jabr.desktop"
  echo "  packaging/jabr.png"
  echo "  packaging/requirements.txt"
  die "put the missing files in place (note the packaging/ subfolder) and run again"
fi

say "Setting up build tooling"
python3 -m venv "$BUILD/venv" \
  || die "could not create a venv (Debian/Ubuntu: sudo apt install python3-venv)"
"$BUILD/venv/bin/pip" install --quiet --upgrade pip python-appimage

# Find and download the bundled-Python base image straight from GitHub's website.
# (python-appimage's own lookup uses GitHub's API, which allows only 60 anonymous
#  requests per hour per IP address and is a common cause of HTTP 403 failures.)
fetch_base_image() {
  local page path name
  page="$(curl -fsSL --retry 3 \
    "https://github.com/niess/python-appimage/releases/expanded_assets/python${PYVER}" || true)"
  path="$(printf '%s' "$page" \
    | grep -o "/niess/python-appimage/releases/download/python${PYVER}/[^\"]*-${LINUX_TAG}\.AppImage" \
    | head -n 1 || true)"
  [[ -n "$path" ]] || return 1
  name="$(basename "$path")"
  mkdir -p "$BUILD/base"
  if [[ ! -s "$BUILD/base/$name" ]]; then
    curl -fL --retry 3 -o "$BUILD/base/$name" "https://github.com$path" || return 1
  fi
  chmod +x "$BUILD/base/$name"
  BASE_IMAGE="$BUILD/base/$name"
}

if [[ -z "${BASE_IMAGE:-}" ]]; then
  say "Downloading the Python $PYVER base image"
  if fetch_base_image; then
    echo "using $BASE_IMAGE"
  else
    echo "Direct download did not work; falling back to python-appimage's own lookup."
  fi
fi

say "Assembling the AppDir (bundled Python $PYVER + PySide6)"
WORK="$BUILD/work"
rm -rf "$WORK" "$APPDIR"
mkdir -p "$WORK"
if [[ -n "${BASE_IMAGE:-}" ]]; then
  BASE_ARGS=(-b "$BASE_IMAGE")
else
  BASE_ARGS=(-p "$PYVER" -l "$LINUX_TAG")
fi
( cd "$WORK" && "$BUILD/venv/bin/python" -m python_appimage build app \
    "${BASE_ARGS[@]}" "$HERE/packaging" -x "$HERE/jabr.py" --no-packaging )
BUILT="$(find "$WORK" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
[[ -n "$BUILT" ]] || die "python-appimage did not produce an AppDir"
mv "$BUILT" "$APPDIR"

say "Checking the AppDir"
[[ -n "$(find "$APPDIR/opt" -maxdepth 6 -type d -name PySide6 | head -n 1)" ]] \
  || die "PySide6 was not bundled (is packaging/requirements.txt in place?)"
grep -q 'jabr.py' "$APPDIR/AppRun" \
  || die "AppRun does not start jabr.py (is packaging/entrypoint.sh in place?)"
[[ -f "$APPDIR/jabr.desktop" ]] \
  || die "jabr.desktop is missing from the AppDir (is it in packaging/?)"
# The base Python image ships AppStream metadata that describes Python, not this app.
rm -f "$APPDIR"/usr/share/metainfo/*.appdata.xml "$APPDIR"/usr/share/metainfo/*.metainfo.xml
echo "OK: PySide6, launcher and desktop entry are in place"

say "Bundling libxcb-cursor (Qt's X11 plugin needs it; many distros don't install it)"
EXTRA="$APPDIR/opt/extra-libs"
mkdir -p "$EXTRA"
CURSOR="$(ldconfig -p 2>/dev/null \
  | awk '/libxcb-cursor\.so\.0/ && ($0 ~ /x86-64/ || $0 ~ /AArch64/) {print $NF; exit}' || true)"
if [[ -n "$CURSOR" && -e "$CURSOR" ]]; then
  cp -L "$CURSOR" "$EXTRA/"
  # its helper libraries (libxcb-image, libxcb-render-util, ...); NOT libxcb.so.1 itself
  while read -r lib; do
    cp -L "$lib" "$EXTRA/"
  done < <(ldd "$CURSOR" | awk '/=> \//{print $3}' | grep -E '/libxcb-[^/]+\.so' || true)
  echo "bundled: $(ls "$EXTRA" | tr '\n' ' ')"
else
  echo "WARNING: libxcb-cursor not found on this machine, so it is NOT bundled."
  echo "         The AppImage will then need it on the machine that runs it."
  echo "         To bundle it, install it here and re-run:"
  echo "           Debian/Ubuntu: sudo apt install libxcb-cursor0"
  echo "           Fedora:        sudo dnf install xcb-util-cursor"
  echo "           Arch:          sudo pacman -S xcb-util-cursor"
fi

say "Fixing permissions (the newest appimagetool refuses 0700 directories)"
chmod -R a+rX "$APPDIR"
chmod a+rx "$APPDIR/AppRun"

say "Packaging"
TOOL="$BUILD/appimagetool-$ARCH.AppImage"
if [[ ! -x "$TOOL" ]]; then
  curl -fL --retry 3 -o "$TOOL" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
  chmod +x "$TOOL"
fi
OUT="$DIST/$APPNAME-$ARCH.AppImage"
rm -f "$OUT"
ARCH="$ARCH" "$TOOL" --no-appstream "$APPDIR" "$OUT"
chmod +x "$OUT"
[[ -s "$OUT" ]] || die "appimagetool finished but $OUT is missing"

say "Done"
ls -lh "$OUT"
echo
echo "Your AppImage is here:  $OUT"
sleep 0.5