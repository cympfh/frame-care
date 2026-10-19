#!/bin/bash
# SPDX-License-Identifier: MIT
# frame-care-toast を Steam Frame の上でビルドする（g++、cairo、pango が要る。SteamOS には入っている）。
#   bash toast/build.sh [出力先]
#   bash toast/build.sh --find-lib     使う libopenvr_api.so のフォルダを表示するだけ
#
# libopenvr_api.so は SteamVR に入っているものを使う。探す順番:
#   1. 環境変数 STEAMVR_LIB（フォルダ）
#   2. ~/.config/openvr/openvrpaths.vrpath の runtime（OpenVR 自身が SteamVR を探すのと同じ場所）
#   3. Steam のライブラリ（~/.local/share/Steam と libraryfolders.vdf の各フォルダ）の steamapps/common/SteamVR
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$here/.."

case "$(uname -m)" in
    aarch64 | arm64) sub=linuxarm64 ;;
    *) sub=linux64 ;;
esac

candidates() {
    local vrpath="${XDG_CONFIG_HOME:-$HOME/.config}/openvr/openvrpaths.vrpath"
    if [[ -f "$vrpath" ]]; then
        python3 -c 'import json, sys; print("\n".join(json.load(open(sys.argv[1])).get("runtime") or []))' "$vrpath" 2>/dev/null || true
    fi
    local steam="$HOME/.local/share/Steam" vdf
    vdf="$steam/steamapps/libraryfolders.vdf"
    {
        echo "$steam"
        if [[ -f "$vdf" ]]; then sed -n 's/^[[:space:]]*"path"[[:space:]]*"\(.*\)"[[:space:]]*$/\1/p' "$vdf"; fi
    } | while read -r lib; do echo "$lib/steamapps/common/SteamVR"; done
}

find_lib() {
    local rt
    if [[ -n "${STEAMVR_LIB:-}" ]]; then
        [[ -f "$STEAMVR_LIB/libopenvr_api.so" ]] && { echo "$STEAMVR_LIB"; return 0; }
    fi
    while read -r rt; do
        if [[ -n "$rt" && -f "$rt/bin/$sub/libopenvr_api.so" ]]; then
            (cd "$rt/bin/$sub" && pwd)
            return 0
        fi
    done < <(candidates)
    return 1
}

if ! lib="$(find_lib)"; then
    echo "SteamVR の libopenvr_api.so が見つかりません（SteamVR を一度起動してから、もう一度ためしてください。STEAMVR_LIB で指定もできます）" >&2
    exit 1
fi
if [[ "${1:-}" == "--find-lib" ]]; then
    echo "$lib"
    exit 0
fi
out="${1:-$here/frame-care-toast}"
g++ -std=c++17 -O2 -Wall -Wextra -o "$out" "$here/frame-care-toast.cpp" \
    -I"$here" -I"$root/vendor/openvr" $(pkg-config --cflags pangocairo cairo) \
    -L"$lib" -lopenvr_api -Wl,-rpath,"$lib" \
    $(pkg-config --libs pangocairo cairo) -lgobject-2.0
