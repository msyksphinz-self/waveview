#!/bin/bash
# Build dist/waveview.exe, a single file that needs no Python, Verilator or C
# compiler on the Windows machine. Run from WSL with a Windows Python on PATH.
#   Linux side:   Zig cross-compiles the FST helpers, with a static zlib
#   Windows side: PyInstaller packs Python, the UI and the helpers
# Everything it installs stays under build/windows and a folder in %TEMP%.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
WORK="${WAVEVIEW_BUILD_DIR:-$ROOT/build/windows}"
ZLIB=zlib-1.3.1
ZLIB_SHA256=9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23
FST_INC="$(verilator --getenv VERILATOR_ROOT)/include/gtkwave"
mkdir -p "$WORK/helpers" "$ROOT/dist"

if [ ! -x "$WORK/zigenv/bin/python" ]; then
    python3 -m venv "$WORK/zigenv"
    "$WORK/zigenv/bin/pip" -q install ziglang
fi
if [ ! -d "$WORK/$ZLIB" ]; then
    curl -fsSL -o "$WORK/$ZLIB.tar.gz" "https://github.com/madler/zlib/releases/download/v1.3.1/$ZLIB.tar.gz"
    echo "$ZLIB_SHA256  $WORK/$ZLIB.tar.gz" | sha256sum -c -
    tar xzf "$WORK/$ZLIB.tar.gz" -C "$WORK"
fi

Z="$WORK/$ZLIB"
ZSRC=()
for f in adler32 compress crc32 deflate gzclose gzlib gzread gzwrite infback inffast inflate inftrees trees uncompr zutil; do
    ZSRC+=("$Z/$f.c")
done
for tool in fstsrv fst2vcd_mini; do
    "$WORK/zigenv/bin/python" -m ziglang cc -target x86_64-windows-gnu -O2 -w -s \
        '-DFST_CONFIG_INCLUDE="fst_config.h"' -I"$FST_INC" -I"$Z" \
        -o "$WORK/helpers/$tool.exe" "$ROOT/$tool.c" \
        "$FST_INC/fstapi.c" "$FST_INC/fastlz.c" "$FST_INC/lz4.c" "${ZSRC[@]}"
done
rm -f "$WORK"/helpers/*.pdb

# PyInstaller runs from a Windows folder: UNC paths into WSL are not reliable
WIN_TEMP="$(cd /mnt/c && cmd.exe /c 'echo %TEMP%' 2>/dev/null | tr -d '\r' | tail -1)"
PACK="$(wslpath -u "$WIN_TEMP")/waveview_build"
rm -rf "$PACK/src"
mkdir -p "$PACK/src"
cp -r "$ROOT"/*.py "$ROOT"/fstsrv.c "$ROOT"/fsdbsrv.cc "$ROOT"/fst2vcd_mini.c "$ROOT"/waveview_ui "$WORK"/helpers/*.exe "$PACK/src/"
cd "$PACK"
[ -f pyenv/Scripts/python.exe ] || python.exe -m venv pyenv
pyenv/Scripts/python.exe -m pip -q install pyinstaller
cd src
../pyenv/Scripts/python.exe -m PyInstaller --noconfirm --clean --onefile --console --name waveview \
    --distpath ../dist --workpath ../work --specpath ../work \
    --add-data "$(wslpath -w "$PACK/src/waveview_ui");waveview_ui" \
    --add-data "$(wslpath -w "$PACK/src/fstsrv.c");." \
    --add-data "$(wslpath -w "$PACK/src/fsdbsrv.cc");." \
    --add-data "$(wslpath -w "$PACK/src/rtlindex.py");." \
    --add-binary "$(wslpath -w "$PACK/src/fstsrv.exe");." \
    --add-binary "$(wslpath -w "$PACK/src/fst2vcd_mini.exe");." \
    waveview.py
cp "$PACK/dist/waveview.exe" "$ROOT/dist/waveview.exe"
ls -l "$ROOT/dist/waveview.exe"
