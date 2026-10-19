#!/bin/bash
# SPDX-License-Identifier: MIT
# frame-care-toast を Steam Frame の上でビルドする（g++、cairo、pango、Vulkan が要る。SteamOS には入っている）。
#   bash toast/build.sh [出力先]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$here/.."
out="${1:-$here/frame-care-toast}"
steamvr_lib=/opt/steamvr/bin/linuxarm64
tp="$root/third_party/frame-mic-tuner"
g++ -std=c++17 -O2 -Wall -Wextra -o "$out" "$here/frame-care-toast.cpp" "$tp/vk_texture.cpp" \
    -I"$here" -I"$tp" -I"$root/vendor/openvr" $(pkg-config --cflags pangocairo cairo vulkan) \
    -L"$steamvr_lib" -lopenvr_api -Wl,-rpath,"$steamvr_lib" \
    $(pkg-config --libs pangocairo cairo vulkan) -lgobject-2.0
