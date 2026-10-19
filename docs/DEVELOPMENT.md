# 開発者向けメモ

## 仕組み

`frame-care.py`（Python、標準ライブラリだけ）が `poll_seconds`（30 秒）ごとに値を読み、知らせることを決めます。
カードを出すのは同じフォルダの `frame-care-toast`（C++。`toast/`。install.sh が Frame の上で `toast/build.sh` でビルド）です。

- frame-care.py はカードを `frame-care-toast` の標準入力に 1 行 1 枚（`種類 TAB タイトル TAB 本文`）で渡します。
  frame-care-toast は SteamVR にオーバーレイでつなぎ、順にフェードイン・アウトで出し、出し終わったら切断して終わります。
  出している間に来たカードは frame-care.py が溜めておき、終わってから渡します（同時に 2 つは動かしません）。
- **SteamVR を起動しない**: `vrserver` のプロセスが無ければ、frame-care.py は frame-care-toast を呼ばず、
  frame-care-toast も `VR_Init` を呼ばずに終了コード 3 で終わります（`VR_Init` は SteamVR を起動してしまうため）。
- カードを出すたびに新しくつなぐので、SteamVR を終了・再起動しても、次のカードから普通に出ます。
  出している途中で SteamVR が終わる知らせ（`VREvent_Quit`）が来たら `AcknowledgeQuit_Exiting` を呼んで切断し、終了コード 4 で終わります。
  知らせなしに `vrserver` が居なくなったときも 1 秒以内に気づいて、それ以上 OpenVR を呼ばずに終わります。
- 見た目は vrc-wrist-notifier の Frame 版（wrist-toast 0.4.5）と同じカードです（640×160、幅 0.30 m、正面 0.8 m・少し下）。
  描画は cairo + pango（`toast/card.h`）、画像は Vulkan のテクスチャで渡します（`third_party/frame-mic-tuner`。使えなければ `SetOverlayRaw`）。
  `libopenvr_api.so` は SteamVR に入っているもの（`/opt/steamvr/bin/linuxarm64`）を使います。
- 種類は `基本.アイコン` の形です: `care.break` / `care.water` / `care.charge`（青緑）、`warn.battery` / `warn.temp`（琥珀）、`alert.battery` / `alert.temp`（赤。表示も 1.5 倍長い）。
- 出力は「問題が無ければ何も書かない」です。ログに書くのは、出せなかったカード（SteamVR が動いていない、など）とエラーだけ。
  `--status` も問題があるときだけ書いて 1 で終わります（`-v` でいまの値も書く）。

frame-care-toast の終了コード: 0 = 全部出した、1 = エラー、2 = 使い方の間違い、3 = SteamVR が動いていない、4 = 途中で SteamVR が終わった。

値の出どころ:

| 値 | 場所 |
|---|---|
| SoC 温度 | `/sys/class/thermal/thermal_zone*` のうち `type` が `cpu*` / `gpu*` のものの最大 |
| 電池・状態 | `/sys/class/power_supply/*` の `type=Battery` の `capacity` / `status` |
| 電源 | 電池以外の power_supply の `online`（または電池が Charging / Full） |
| かぶっている | `presence.source = steamvr`: `vrserver` プロセスがある間。`proximity`: `/sys/bus/iio/devices/*/in_proximity_raw` > `proximity_threshold` |

時計は `CLOCK_BOOTTIME`（スリープ中も進む）なので、スリープ明けは「外していた」扱いになり、休憩のカウントがリセットされます。

## 設定（`~/.config/frame-care/config.json`）

書いていない項目は既定値になります。

| 項目 | 既定 | 意味 |
|---|---|---|
| `break.enabled` / `every_minutes` | true / 60 | 休憩の通知 |
| `hydration.enabled` / `every_minutes` | false / 45 | 水分補給の通知 |
| `battery.levels` / `urgent_level` | [20, 10] / 10 | 電池の通知（つなぐと再び有効） |
| `temperature.warn_c` / `urgent_c` | 85 / 90 | 注意・警告の温度 |
| `temperature.rearm_below_c` | 80 | ここまで下がったら、次にまた知らせる |
| `temperature.urgent_repeat_minutes` | 3 | 警告をくり返す間隔 |
| `charge_done.min_start_percent` | 95 | これ未満から充電したときだけ「充電完了」を出す |
| `charge_done.full_percent` | 98 | 電源につないだまま、ここ以上で充電が止まったら（Full でなくても）完了とみなす |
| `quiet_hours.enabled` / `start` / `end` / `allow_urgent` | false / 01:00 / 08:00 / true | 静かな時間 |
| `rate_limit.min_gap_seconds` / `max_per_hour` | 20 / 12 | 休憩などの回数制限（温度・電池には効かない） |
| `presence.source` / `proximity_threshold` / `absent_reset_minutes` | steamvr / null / 5 | かぶっている判定 |
| `toast.duration_seconds` | 4 | 1 枚を出す秒数（急ぎは 1.5 倍） |
| `toast.width_m` / `distance_m` / `offset_y_m` | 0.30 / 0.8 / -0.18 | カードの幅、正面の距離、上下（マイナスで下） |
| `toast.font` | Noto Sans CJK JP | フォント |
| `poll_seconds` | 30 | 値を読む間隔 |

近接センサーを使うときは、かぶったときと外したときに `--status` の「近接センサー」の値を見て、その間を `proximity_threshold` にします。

## コマンドラインオプション

`frame-care.py`: `--status [-v]` / `--test-toast` / `--config FILE` / `--fake-sensors FILE`（センサーの代わりに JSON を読む。`steamvr` が無ければ `worn` と同じ） / `--toast-bin FILE`（frame-care-toast の場所） / `--poll 秒` / `--duration 秒` / `--version`

`frame-care-toast`: `--dump-png OUT --kind K --title T --text S`（SteamVR なしでカードを PNG に） / `--self-test` / `--duration-s` / `--width-m` / `--distance-m` / `--offset-y-m` / `--font` / `--version`

## テスト

```sh
python3 tests/test_frame_care.py
```

作りものの値で判定を確かめ、本物の代わりの frame-care-toast（テストの中で作るスクリプト）で、渡すカードと、
SteamVR が動いていないとき・部品が無いときのふるまい、`--status` が問題の無いときに黙ることを確かめます。
g++ と pango があれば `toast/card.h` の自己テストもします。設定ファイルと `XDG_CONFIG_HOME` は一時フォルダにするので、本物の `~/.config` には触りません。

Frame の上では `~/.local/opt/frame-care/frame-care-toast --self-test` でも描画の自己テストができます。

## 同梱しているもの

- `vendor/openvr/openvr.h`（Valve、BSD-3-Clause、`vendor/openvr/LICENSE`）
- `third_party/frame-mic-tuner/vk_texture.*`（sasaken1102r/frame-mic-tuner、MIT、無改変。くわしくは同じフォルダの README）
