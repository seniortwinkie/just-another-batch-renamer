#! /bin/bash
# Libraries bundled by build_appimage.sh that many distros don't install by default
# (Qt's X11 plugin needs libxcb-cursor). Kept in their own folder so they can't
# shadow system libraries.
export LD_LIBRARY_PATH="${APPDIR}/opt/extra-libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec {{ python-executable }} "${APPDIR}/jabr.py" "$@"
