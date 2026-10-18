#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""frame-care: Steam Frame をかぶっている間の「休憩・水分・電池・温度・充電完了」をそっと知らせる。

通知は vrc-wrist-notifier の表示係（UDP 39570 の OSC /wrist/notify）に送ってカードとして出す。
表示係が動いていなければ、ログ（journalctl）に書くだけにする。Python 3 の標準ライブラリだけで動く。

  frame-care.py                  常駐（systemd --user から起動する想定）
  frame-care.py --status         いまの値と、通知がどうなるかを表示して終わる
  frame-care.py --test-toast     テストのカードを 1 枚送って終わる
"""
import argparse
import datetime as dt
import glob
import json
import os
import signal
import socket
import struct
import sys
import time

VERSION = "0.1.0"

DEFAULTS = {
    "toast_host": "127.0.0.1",
    "toast_port": 39570,
    "check_toast_running": True,   # localhost の表示係が動いていないときはログだけにする
    "poll_seconds": 30,
    "presence": {
        "source": "steamvr",          # steamvr = SteamVR が動いている間をかぶっている時間とみなす / proximity = 近接センサー
        "proximity_threshold": None,   # source が proximity のとき、この値より大きければかぶっている
        "absent_reset_minutes": 5,     # これだけ外していたら、連続時間をリセット
    },
    "break": {"enabled": True, "every_minutes": 60},
    "hydration": {"enabled": False, "every_minutes": 45},
    "battery": {"enabled": True, "levels": [20, 10], "urgent_level": 10},
    "temperature": {"enabled": True, "warn_c": 85, "urgent_c": 90, "rearm_below_c": 80, "urgent_repeat_minutes": 3},
    "charge_done": {"enabled": True, "min_start_percent": 95},
    "quiet_hours": {"enabled": False, "start": "01:00", "end": "08:00", "allow_urgent": True},
    "rate_limit": {"min_gap_seconds": 20, "max_per_hour": 12},
}

MESSAGES = {
    "break": ("休憩しよう", "連続 {minutes} 分かぶっています。少し外して目と首を休めませんか"),
    "hydration": ("水分補給", "そろそろ一口どうぞ 💧"),
    "battery": ("電池残り {level}%", "電池が {percent}% です。早めに充電を"),
    "battery_urgent": ("電池残りわずか", "電池が {percent}% です。もうすぐ切れます"),
    "temp_warn": ("本体が熱め {temp:.0f}°C", "重い処理を控えるか、少し休ませてください"),
    "temp_urgent": ("本体が高温 {temp:.0f}°C", "いったん休ませてください。95°C 前後で電源が落ちることがあります"),
    "charge_done": ("充電完了", "電池 {percent}%。ケーブルを抜いても大丈夫です"),
    "test": ("frame-care", "テスト通知です。これが見えれば準備OK"),
}
URGENT = {"battery_urgent", "temp_urgent"}          # 静かな時間でも出す
NO_RATE_LIMIT = URGENT | {"battery", "temp_warn"}  # 安全に関わるものは回数制限をかけない


def log(*args):
    print(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)


# ------------------------------------------------------------------ 設定
def default_config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "frame-care", "config.json")


def merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        elif not k.startswith("_"):
            out[k] = v
    return out


def load_config(path):
    cfg = DEFAULTS
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = merge(DEFAULTS, json.load(f))
    return cfg


# ------------------------------------------------------------------ OSC
def _pad(b):
    return b + b"\0" * (4 - len(b) % 4)


def osc_message(address, *strings):
    msg = _pad(address.encode()) + _pad(("," + "s" * len(strings)).encode())
    for s in strings:
        msg += _pad(str(s).encode("utf-8"))
    return msg


def udp_port_bound(port, tables=("/proc/net/udp", "/proc/net/udp6")):
    """この Frame の中で、だれかがその UDP ポートで待ち受けているか。"""
    want = ":%04X" % port
    for t in tables:
        try:
            with open(t) as f:
                next(f)
                for line in f:
                    local = line.split()[1]
                    if local.endswith(want):
                        return True
        except (OSError, StopIteration, IndexError):
            pass
    return False


class Toaster:
    def __init__(self, cfg):
        self.host = cfg["toast_host"]
        self.port = int(cfg["toast_port"])
        self.check = cfg.get("check_toast_running", True) and self.host in ("127.0.0.1", "localhost")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.last_state = None

    def available(self):
        ok = (not self.check) or udp_port_bound(self.port)
        if ok != self.last_state:
            log("表示係（UDP %d）: %s" % (self.port, "見つかりました。カードで知らせます" if ok
                                          else "動いていないので、ログに書くだけにします"))
            self.last_state = ok
        return ok

    def send(self, kind, title, text):
        if not self.available():
            return False
        try:
            self.sock.sendto(osc_message("/wrist/notify", kind, title, text), (self.host, self.port))
            return True
        except OSError as e:
            log("送れませんでした:", e)
            return False


# ------------------------------------------------------------------ センサー
def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def soc_temp_c(base="/sys/class/thermal"):
    best = None
    for z in glob.glob(os.path.join(base, "thermal_zone*")):
        name = read(os.path.join(z, "type")) or ""
        if not name.startswith(("cpu", "gpu")):
            continue
        v = read(os.path.join(z, "temp"))
        try:
            c = int(v) / 1000.0
        except (TypeError, ValueError):
            continue
        best = c if best is None else max(best, c)
    return best


def power(base="/sys/class/power_supply"):
    """(残量 %, 電池の status, 電源につながっているか)"""
    percent = status = None
    online = False
    for p in sorted(glob.glob(os.path.join(base, "*"))):
        typ = read(os.path.join(p, "type"))
        if typ == "Battery" and percent is None:
            try:
                percent = int(read(os.path.join(p, "capacity")))
            except (TypeError, ValueError):
                pass
            status = read(os.path.join(p, "status"))
        elif read(os.path.join(p, "online")) == "1" and typ in ("USB", "Mains", "Unknown"):
            online = True
    if status in ("Charging", "Full"):
        online = True
    return percent, status, online


def steamvr_running(proc="/proc"):
    for d in os.listdir(proc):
        if d.isdigit() and read(os.path.join(proc, d, "comm")) == "vrserver":
            return True
    return False


def proximity_raw():
    for d in glob.glob("/sys/bus/iio/devices/iio:device*"):
        v = read(os.path.join(d, "in_proximity_raw"))
        if v is not None:
            try:
                return float(v)
            except ValueError:
                pass
    return None


class Sensors:
    def __init__(self, cfg, fake=None):
        self.cfg = cfg
        self.fake = fake

    def reading(self):
        if self.fake:  # テスト用: JSON ファイルの値を毎回読み直す
            with open(self.fake, encoding="utf-8") as f:
                return json.load(f)
        percent, status, online = power()
        pres = self.cfg["presence"]
        prox = proximity_raw()
        if pres.get("source") == "proximity" and pres.get("proximity_threshold") is not None and prox is not None:
            worn = prox > float(pres["proximity_threshold"])
        else:
            worn = steamvr_running()
        return {"temp_c": soc_temp_c(), "battery": percent, "status": status, "online": online,
                "worn": worn, "proximity": prox}


# ------------------------------------------------------------------ 判定
def parse_hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_quiet(qh, when):
    if not qh.get("enabled"):
        return False
    start, end, now = parse_hhmm(qh["start"]), parse_hhmm(qh["end"]), when.hour * 60 + when.minute
    return start <= now < end if start <= end else (now >= start or now < end)


class Care:
    """読み取った値から「いま知らせること」を決める。時刻は外から渡す（テストしやすくするため）。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.worn_since = None      # 連続装着の開始
        self.last_worn = None       # 最後にかぶっていた時刻
        self.break_mark = None      # 次の休憩カウントの起点
        self.hydration_mark = None
        self.battery_fired = set()
        self.temp_state = "ok"      # ok / warn / urgent
        self.temp_urgent_at = None
        self.plug_session = None    # {"low_seen": bool, "done": bool}
        self.sent_times = []
        self.last_sent = None
        self.last_tick = None

    # 通知の候補を返す: [(key, kind, title, text)]
    def evaluate(self, now, r):
        out = []
        c = self.cfg
        # --- 装着時間（スリープなどで間が空いたら外していた扱い）
        gap = now - self.last_tick if self.last_tick is not None else 0
        self.last_tick = now
        absent_reset = c["presence"]["absent_reset_minutes"] * 60
        if gap > max(absent_reset, c["poll_seconds"] * 4):
            self.worn_since = None
        if r.get("worn"):
            if self.worn_since is None or (self.last_worn is not None and now - self.last_worn > absent_reset):
                self.worn_since = self.break_mark = self.hydration_mark = now
            self.last_worn = now
            b = c["break"]
            if b.get("enabled") and now - self.break_mark >= b["every_minutes"] * 60:
                minutes = int(round((now - self.worn_since) / 60))
                out.append(("break", "care") + self._msg("break", minutes=minutes))
                self.break_mark = now
            h = c["hydration"]
            if h.get("enabled") and now - self.hydration_mark >= h["every_minutes"] * 60:
                out.append(("hydration", "care") + self._msg("hydration"))
                self.hydration_mark = now
        elif self.last_worn is not None and now - self.last_worn > absent_reset:
            self.worn_since = None

        # --- 電池
        pct, online = r.get("battery"), r.get("online")
        bc = c["battery"]
        if bc.get("enabled") and pct is not None:
            if online:
                self.battery_fired.clear()
            else:
                for level in sorted(bc["levels"]):
                    if pct > level + 3:
                        self.battery_fired.discard(level)
                for level in sorted(bc["levels"]):   # いちばん低い段だけ知らせる
                    if pct <= level:
                        if level not in self.battery_fired:
                            key = "battery_urgent" if level <= bc.get("urgent_level", 10) else "battery"
                            out.append((key, "alert" if key in URGENT else "care") + self._msg(key, level=level, percent=pct))
                        self.battery_fired.update(l for l in bc["levels"] if l >= level)
                        break

        # --- 温度（下がりきるまで同じ通知はくり返さない。高温だけは数分おきにくり返す）
        t = r.get("temp_c")
        tc = c["temperature"]
        if tc.get("enabled") and t is not None:
            if t >= tc["urgent_c"]:
                if self.temp_state != "urgent" or now - self.temp_urgent_at >= tc["urgent_repeat_minutes"] * 60:
                    out.append(("temp_urgent", "alert") + self._msg("temp_urgent", temp=t))
                    self.temp_urgent_at = now
                self.temp_state = "urgent"
            elif t >= tc["warn_c"]:
                if self.temp_state == "ok":
                    out.append(("temp_warn", "care") + self._msg("temp_warn", temp=t))
                    self.temp_state = "warn"
            elif t < tc["rearm_below_c"]:
                self.temp_state = "ok"

        # --- 充電完了（つないでいる間に一度だけ。ほぼ満タンでつないだときは言わない）
        cd = c["charge_done"]
        if cd.get("enabled") and pct is not None:
            if online:
                if self.plug_session is None:
                    self.plug_session = {"low_seen": pct < cd["min_start_percent"], "done": False}
                s = self.plug_session
                if pct < cd["min_start_percent"]:
                    s["low_seen"] = True
                full = r.get("status") == "Full" or pct >= 100
                if s["low_seen"] and not s["done"] and full:
                    out.append(("charge_done", "care") + self._msg("charge_done", percent=pct))
                    s["done"] = True
            else:
                self.plug_session = None
        return out

    def _msg(self, key, **kw):
        title, text = MESSAGES[key]
        return title.format(**kw), text.format(**kw)

    # 静かな時間と回数制限を通ったものだけ返す
    def filter(self, now, wall, items):
        passed = []
        qh, rl = self.cfg["quiet_hours"], self.cfg["rate_limit"]
        self.sent_times = [x for x in self.sent_times if now - x < 3600]
        for key, kind, title, text in items:
            urgent = key in URGENT
            if in_quiet(qh, wall) and not (urgent and qh.get("allow_urgent", True)):
                log("静かな時間なので出しません:", title)
                continue
            if key not in NO_RATE_LIMIT:
                if self.last_sent is not None and now - self.last_sent < rl["min_gap_seconds"]:
                    log("間隔が短いので出しません:", title)
                    continue
                if len(self.sent_times) >= rl["max_per_hour"]:
                    log("1 時間の上限なので出しません:", title)
                    continue
            self.last_sent = now
            self.sent_times.append(now)
            passed.append((key, kind, title, text))
        return passed


# ------------------------------------------------------------------ main
def clock():
    """スリープ中も進む時計。スリープ明けは「外していた」と判定できる。"""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return time.monotonic()


def describe(r):
    return "温度 %s / 電池 %s%% (%s, 電源%s) / かぶっている: %s / 近接センサー %s" % (
        "%.1f°C" % r["temp_c"] if r.get("temp_c") is not None else "?", r.get("battery"), r.get("status"),
        "あり" if r.get("online") else "なし", "はい" if r.get("worn") else "いいえ", r.get("proximity"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Steam Frame の休憩・電池・温度のお知らせ")
    ap.add_argument("--config", default=default_config_path())
    ap.add_argument("--status", action="store_true", help="いまの値を表示して終わる")
    ap.add_argument("--test-toast", action="store_true", help="テストのカードを 1 枚送る")
    ap.add_argument("--fake-sensors", help="テスト用: センサーの代わりにこの JSON を読む")
    ap.add_argument("--port", type=int, help="送り先ポートを上書き（テスト用）")
    ap.add_argument("--poll", type=float, help="値を読む間隔（秒）を上書き")
    ap.add_argument("--duration", type=float, help="この秒数で終わる（テスト用）")
    ap.add_argument("--version", action="version", version=VERSION)
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    if a.port:
        cfg["toast_port"] = a.port
    if a.poll:
        cfg["poll_seconds"] = a.poll
    toaster = Toaster(cfg)
    sensors = Sensors(cfg, a.fake_sensors)

    if a.status:
        print(describe(sensors.reading()))
        print("表示係（UDP %d）: %s" % (cfg["toast_port"], "動いています" if udp_port_bound(cfg["toast_port"]) else "見つかりません（ログだけになります）"))
        print("静かな時間:", "いま静かな時間です" if in_quiet(cfg["quiet_hours"], dt.datetime.now()) else "いいえ")
        return 0
    if a.test_toast:
        ok = toaster.send("care", *MESSAGES["test"])
        print("送りました" if ok else "表示係が動いていないので送っていません")
        return 0 if ok else 1

    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    care = Care(cfg)
    log("frame-care %s を開始（設定 %s）" % (VERSION, a.config if os.path.exists(a.config) else "既定値"))
    t_end = time.monotonic() + a.duration if a.duration else None
    first = True
    while not stop and (t_end is None or time.monotonic() < t_end):
        try:
            r = sensors.reading()
            if first:
                log("いまの値:", describe(r))
                toaster.available()
                first = False
            now = clock()
            for key, kind, title, text in care.filter(now, dt.datetime.now(), care.evaluate(now, r)):
                sent = toaster.send(kind, title, text)
                log("%s: %s / %s" % ("通知" if sent else "通知（ログのみ）", title, text))
        except Exception as e:  # センサーが一時的に読めなくても止まらない
            log("エラー:", repr(e))
        slept = 0.0
        while not stop and slept < cfg["poll_seconds"]:
            time.sleep(min(0.5, cfg["poll_seconds"]))
            slept += 0.5
    log("終了します")
    return 0


if __name__ == "__main__":
    sys.exit(main())
