#!/bin/bash
# SPDX-License-Identifier: MIT
# frame-care を Steam Frame に入れる・外す。Frame の Konsole で、ふつうのユーザーで実行する（sudo 不要）。
#
#   curl -fsSL https://raw.githubusercontent.com/cympfh/frame-care/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/cympfh/frame-care/main/install.sh | bash -s -- --uninstall
#
# 入れるもの: ~/.local/opt/frame-care/（本体と、カードを出す frame-care-toast。この場でビルドする）、
#             ~/.config/systemd/user/frame-care.service、~/.config/frame-care/config.json（無いときだけサンプルからコピー）
# ビルドには g++・cairo・pango を使う（SteamOS に入っている）。pacman などで何かを足すことはしない。
# 自動起動は勝手にオンにしない。SteamVR などは再起動しない。
# 外すとき（--uninstall）は消さずに ~/.local/share/frame-care-uninstalled/<日時>/ に移す。設定ファイルはそのまま残す。
set -euo pipefail

name=frame-care
repo=cympfh/$name
ref=${FRAME_TOOL_REF:-main}
dest="$HOME/.local/opt/$name"
config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
unit_dir="$config_home/systemd/user"
unit="$name.service"
conf_dir="$config_home/$name"
files="frame-care.py config.example.json README.md LICENSE"

# デスクトップモードの Konsole でも systemctl --user が本来のセッションに届くようにする
real="/run/user/$(id -u)"
if [[ ! -S "${XDG_RUNTIME_DIR:-}/systemd/private" && -S "$real/systemd/private" ]]; then
    export XDG_RUNTIME_DIR="$real" DBUS_SESSION_BUS_ADDRESS="unix:path=$real/bus"
fi

[[ "$(id -u)" != 0 ]] || { echo "root では動かしません。sudo なしで実行してください。" >&2; exit 1; }

if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl --user disable --now "$unit" 2>/dev/null || true
    aside="$HOME/.local/share/$name-uninstalled/$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$aside"
    if [[ -e "$dest" ]]; then mv "$dest" "$aside/"; fi
    if [[ -e "$unit_dir/$unit" ]]; then mv "$unit_dir/$unit" "$aside/"; fi
    systemctl --user daemon-reload || true
    echo "$name を外しました。"
    echo "  外したファイルは $aside に移してあります（要らなければ消してOK）。"
    echo "  設定ファイル $conf_dir はそのまま残しています。"
    exit 0
fi

# ファイルの出どころ: このスクリプトの隣にあればそれ、無ければ GitHub から tar.gz を取ってくる（git は使わない）
here=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [[ -f "$here/frame-care.py" ]] || here=""
fi
if [[ -z "$here" ]]; then
    cache="${XDG_CACHE_HOME:-$HOME/.cache}"
    mkdir -p "$cache"
    tmp="$(mktemp -d "$cache/$name-install.XXXXXX")"
    trap 'rm -rf "$tmp"' EXIT
    if [[ -n "${FRAME_TOOL_TARBALL:-}" ]]; then
        cp "$FRAME_TOOL_TARBALL" "$tmp/src.tar.gz"
    else
        echo "ダウンロード中: https://github.com/$repo ($ref)"
        curl -fsSL "https://codeload.github.com/$repo/tar.gz/$ref" -o "$tmp/src.tar.gz"
    fi
    mkdir -p "$tmp/src"
    tar xzf "$tmp/src.tar.gz" -C "$tmp/src" --strip-components=1
    here="$tmp/src"
fi

mkdir -p "$dest" "$unit_dir" "$conf_dir"
if [[ "$here" != "$dest" ]]; then
    for f in $files; do
        install -m644 "$here/$f" "$dest/$f.new"
        mv -f "$dest/$f.new" "$dest/$f"
    done
    chmod 755 "$dest/frame-care.py"
fi
# カードを出す frame-care-toast をビルドする（失敗しても frame-care は動く。知らせはログだけになる）
log="${XDG_CACHE_HOME:-$HOME/.cache}/frame-care-build.log"
mkdir -p "$(dirname "$log")"
if bash "$here/toast/build.sh" "$dest/frame-care-toast.new" >"$log" 2>&1; then
    mv -f "$dest/frame-care-toast.new" "$dest/frame-care-toast"
    rm -f "$log"
    built=1
else
    rm -f "$dest/frame-care-toast.new"
    built=""
fi
# 設定ファイル: 無いときだけサンプルからコピー
if [[ ! -f "$conf_dir/config.json" ]]; then
    cp "$here/config.example.json" "$conf_dir/config.json"
fi
install -m644 "$here/contrib/$unit" "$unit_dir/$unit"
systemctl --user daemon-reload
# 動いていたときだけ、新しい版で動かし直す（このツールだけ）
systemctl --user try-restart "$unit" 2>/dev/null || true

cat <<MSG

frame-care を入れました。
  オンにする:   systemctl --user enable --now $name   （次からは SteamVR と一緒に起動）
  テスト:       python3 $dest/frame-care.py --test-toast   （SteamVR を起動しているときにカードが 1 枚出ます）
  オフにする:   systemctl --user disable --now $name
  設定:         $conf_dir/config.json
MSG
if [[ -z "$built" ]]; then
    echo
    echo "注意: カードを出す部品（frame-care-toast）をビルドできませんでした。知らせはログに書くだけになります。"
    echo "  くわしくは $log"
    exit 1
fi
