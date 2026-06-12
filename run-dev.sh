#!/bin/sh
# run-dev.sh — launch Timbre from the project root without installing
# Usage: ./run-dev.sh [args...]
#
# Prerequisites (one-time):
#   meson setup build
#   ninja -C build
set -e

PROJ="$(dirname "$(realpath "$0")")"
BUILDDIR="$PROJ/build"

# Ensure the timbre/ symlink exists (src/ as the importable package)
if [ ! -L "$PROJ/timbre" ]; then
  ln -sf "$PROJ/src" "$PROJ/timbre"
fi

# Compile schemas if stale
mkdir -p "$BUILDDIR/schemas"
cp "$PROJ/data/io.github.tylerreece.timbre.gschema.xml" "$BUILDDIR/schemas/"
glib-compile-schemas "$BUILDDIR/schemas/"

# Launch
exec env \
  GSETTINGS_SCHEMA_DIR="$BUILDDIR/schemas" \
  PYTHONPATH="$PROJ" \
  python3 - "$@" <<EOF
import os, sys
PROJ = "$PROJ"
BUILDDIR = "$BUILDDIR"
sys.path.insert(0, PROJ)
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gst', '1.0')
gi.require_version('Gdk', '4.0')
from gi.repository import Gio
resource = Gio.Resource.load(BUILDDIR + '/data/timbre.gresource')
resource._register()
from timbre import main
sys.exit(main.main('0.1.0-dev'))
EOF
