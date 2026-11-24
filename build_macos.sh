#!/bin/bash
# Build dist/waveview.app and dist/waveview for macOS: one program that needs
# no Python, Verilator or C compiler on the Mac it runs on. Run on a Mac with
# the Xcode command line tools and python3.
#   helpers:  clang compiles the FST helpers against the GTKWave FST library,
#             taken from Verilator if the installed one still ships it
#             (up to 5.040), else from the Verilator 5.040 release tarball
#   package:  PyInstaller packs Python, the UI and the helpers
# Everything it installs stays under build/macos. The result is built for the
# CPU of this Mac (arm64 or x86_64); build on each kind for both.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
WORK="${WAVEVIEW_BUILD_DIR:-$ROOT/build/macos}"
PY="${PYTHON:-python3}"
CC="${CC:-cc}"
VL=verilator-5.040
VL_SHA256=56c7c46314adfad06dd093b77823bfd9b49ebef72342549f790718199c3e8223
mkdir -p "$WORK/helpers" "$ROOT/dist"

FST_INC="${WAVEVIEW_FST_SRC:-$(verilator --getenv VERILATOR_ROOT 2>/dev/null || true)/include/gtkwave}"
if [ ! -f "$FST_INC/fstapi.c" ]; then
    FST_INC="$WORK/$VL/include/gtkwave"
    if [ ! -f "$FST_INC/fstapi.c" ]; then
        curl -fsSL -o "$WORK/$VL.tar.gz" "https://github.com/verilator/verilator/archive/refs/tags/v${VL#verilator-}.tar.gz"
        echo "$VL_SHA256  $WORK/$VL.tar.gz" | shasum -a 256 -c -
        tar xzf "$WORK/$VL.tar.gz" -C "$WORK" "$VL/include/gtkwave"
    fi
fi
echo "FST library: $FST_INC"

# Depending on the version fstapi.c includes fastlz.c and lz4.c itself or
# expects them next to it: try with them first, then without.
EXTRA=""
for f in fastlz.c lz4.c; do
    [ -f "$FST_INC/$f" ] && EXTRA="$EXTRA $FST_INC/$f"
done
for tool in fstsrv fst2vcd_mini; do
    FLAGS=(-O2 -w '-DFST_CONFIG_INCLUDE="fst_config.h"' "-I$FST_INC" -o "$WORK/helpers/$tool" "$ROOT/$tool.c" "$FST_INC/fstapi.c")
    # shellcheck disable=SC2086
    "$CC" "${FLAGS[@]}" $EXTRA -lz -lpthread 2>/dev/null || "$CC" "${FLAGS[@]}" -lz -lpthread
done

if [ ! -x "$WORK/pyenv/bin/python" ]; then
    "$PY" -m venv "$WORK/pyenv"
fi
"$WORK/pyenv/bin/python" -m pip -q install pyinstaller

rm -rf "$WORK/dist" "$WORK/work"
cd "$ROOT"
# --windowed makes the .app bundle (and still leaves the plain binary), with
# no terminal of its own; --argv-emulation turns a file dropped on the app
# into an argument, like a file dropped on the Windows exe
"$WORK/pyenv/bin/python" -m PyInstaller --noconfirm --clean --onefile --windowed --argv-emulation --name waveview \
    --distpath "$WORK/dist" --workpath "$WORK/work" --specpath "$WORK/work" \
    --add-data "$ROOT/waveview_ui:waveview_ui" \
    --add-data "$ROOT/fstsrv.c:." \
    --add-data "$ROOT/fsdbsrv.cc:." \
    --add-data "$ROOT/rtlindex.py:." \
    --add-binary "$WORK/helpers/fstsrv:." \
    --add-binary "$WORK/helpers/fst2vcd_mini:." \
    waveview.py

# The bundle's Info.plist: open dumps with it, and no Dock icon or menu bar
# of its own (the browser window is the application). Then sign it again
# (ad hoc) since the plist changed.
"$WORK/pyenv/bin/python" - "$WORK/dist/waveview.app/Contents/Info.plist" <<'EOF'
import plistlib, sys
path = sys.argv[1]
with open(path, "rb") as fh:
    info = plistlib.load(fh)
info["CFBundleIdentifier"] = "io.github.msyksphinz.waveview"
info["CFBundleDisplayName"] = "waveview"
info["LSUIElement"] = True
info["CFBundleDocumentTypes"] = [{
    "CFBundleTypeName": "Simulation waveform dump",
    "CFBundleTypeRole": "Viewer",
    "CFBundleTypeExtensions": ["fst", "vcd", "fsdb"],
    "LSHandlerRank": "Alternate",
}]
with open(path, "wb") as fh:
    plistlib.dump(info, fh)
EOF
codesign --force --deep --sign - "$WORK/dist/waveview.app"

# New files, not overwritten ones: a binary that was run before keeps its
# validated code signature in the kernel's cache, and new contents under
# the same inode get killed on launch (and on any mmap) until it is gone.
rm -rf "$ROOT/dist/waveview.app" "$ROOT/dist/waveview"
cp -R "$WORK/dist/waveview.app" "$ROOT/dist/waveview.app"
cp "$WORK/dist/waveview" "$ROOT/dist/waveview"
ls -ld "$ROOT/dist/waveview.app" "$ROOT/dist/waveview"
