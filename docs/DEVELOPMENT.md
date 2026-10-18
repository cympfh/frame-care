# 開発者向けメモ

## 仕組み

`poll_seconds`（30 秒）ごとに値を読み、知らせることがあれば `127.0.0.1:39570` に OSC `/wrist/notify ,sss 種類 タイトル 本文` を送ります。
受け取って表示するのは [vrc-wrist-notifier](https://github.com/cympfh/vrc-wrist-notifier) の Frame 側（wrist-toast）です。
種類は `care`（ふつう）と `alert`（高温・電池わずか）。

送る前に `/proc/net/udp` を見て、そのポートで待ち受けているものが無ければ送らず、ログ（journal）に書くだけにします。

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
| `toast_host` / `toast_port` / `check_toast_running` | 127.0.0.1 / 39570 / true | 送り先 |
| `poll_seconds` | 30 | 値を読む間隔 |

近接センサーを使うときは、かぶったときと外したときに `--status` の「近接センサー」の値を見て、その間を `proximity_threshold` にします。

## コマンドラインオプション

`--status` / `--test-toast` / `--config FILE` / `--fake-sensors FILE`（センサーの代わりに JSON を読む） / `--port N` / `--poll 秒` / `--duration 秒` / `--version`

## テスト

```sh
python3 tests/test_frame_care.py
```

作りものの値で判定を確かめ、localhost の UDP 39580 で受けて送信まで確かめます（本物の 39570 は使いません）。
