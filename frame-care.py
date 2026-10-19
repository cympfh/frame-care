#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""frame-care: Steam Frame をかぶっている間の「休憩・水分・電池・温度・充電完了」をそっと知らせる。

カードは同じフォルダの frame-care-toast（C++、install.sh がビルドする）が SteamVR のオーバーレイとして視界の正面に出す。
SteamVR が動いていないときは出さない（SteamVR を起動することはない）。出せなかったカードだけログ（journalctl）に書く。
問題がなければ何も出力しない。この Python 部分は標準ライブラリだけで動く。

  frame-care.py                  常駐（systemd --user から起動する想定）
  frame-care.py --status         問題があるときだけ書く（-v でいまの値も）
  frame-care.py --test-toast     テストのカードを 1 枚出す
"""
import argparse
import datetime as dt
import glob
import json
import os
import signal
import subprocess
import sys
import time

VERSION = "0.2.0"
HERE = os.path.dirname(os.path.abspath(__file__))
TOAST_BIN = os.path.join(HERE, "frame-care-toast")

DEFAULTS = {
    # カードの出し方（frame-care-toast に渡す）。急ぎ（alert）は 1.5 倍の時間出す
    "toast": {"duration_seconds": 4, "width_m": 0.30, "distance_m": 0.8, "offset_y_m": -0.18,
              "font": "Noto Sans CJK JP"},
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
    "charge_done": {"enabled": True, "min_start_percent": 95, "full_percent": 98},
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
# カードの種類: alert = 赤（表示も長め）、warn = 琥珀、care = 青緑。「.」のあとはアイコン
KINDS = {
    "break": "care.break", "hydration": "care.water", "charge_done": "care.charge",
    "battery": "warn.battery", "temp_warn": "warn.temp",
    "battery_urgent": "alert.battery", "temp_urgent": "alert.temp",
    "test": "care",
}
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


# ------------------------------------------------------------------ カード
def toast_line(kind, title, text):
    """frame-care-toast に渡す 1 行（種類 TAB タイトル TAB 本文）。タブと改行は空白にする。"""
    def one(x):
        return " ".join(str(x).replace("\t", " ").splitlines())
    return "%s\t%s\t%s\n" % (one(kind), one(title), one(text))


def toast_args(cfg):
    t = cfg["toast"]
    return ["--duration-s", str(t["duration_seconds"]), "--width-m", str(t["width_m"]),
            "--distance-m", str(t["distance_m"]), "--offset-y-m", str(t["offset_y_m"]), "--font", str(t["font"])]


def toast_bin_problem(path):
    """frame-care-toast が使えなければその理由、使えれば None。"""
    if not os.path.isfile(path):
        return "カードを出す frame-care-toast がありません（install.sh で入れ直してください）: " + path
    if not os.access(path, os.X_OK):
        return "frame-care-toast を実行できません: " + path
    return None


NO_STEAMVR, STEAMVR_QUIT = 3, 4   # frame-care-toast の終了コード
MAX_PENDING = 20                   # 溜めておくカードの上限（超えたら古いものから捨てる）
DEADLINE_SLACK = 15                # frame-care-toast を待つ時間 = 枚数 × 表示秒数 × 1.5 + これ（秒）


class Toaster:
    """カードを frame-care-toast に渡して SteamVR に出す。

    frame-care-toast は渡された分を出し終わると SteamVR から切断して終わる。出している間に来たカードは溜めておき、
    終わってからまとめて渡す（同時に 2 つ動かさない）。SteamVR の再起動をまたいでも、次のカードで新しくつなぐだけ。
    """

    def __init__(self, cfg, binary=TOAST_BIN):
        self.binary = binary
        self.args = toast_args(cfg)
        self.seconds = float(cfg["toast"]["duration_seconds"])
        self.slack = DEADLINE_SLACK
        self.pending = []    # [(kind, title, text)]
        self.proc = None
        self.batch = []      # いま frame-care-toast に渡している分
        self.deadline = None
        self.missing_logged = False

    def send(self, kind, title, text):
        self.pending.append((kind, title, text))
        if len(self.pending) > MAX_PENDING:
            dropped, self.pending = self.pending[:-MAX_PENDING], self.pending[-MAX_PENDING:]
            self._lost(dropped, "溜まりすぎたので古いものを捨てました")

    def busy(self):
        return self.proc is not None and self.proc.poll() is None

    def _lost(self, cards, why):
        for _, title, text in cards:
            log("出せませんでした（%s）: %s / %s" % (why, title, text))

    def _finish(self):
        rc = self.proc.returncode
        self.deadline = None
        if rc == NO_STEAMVR:
            self._lost(self.batch, "SteamVR が動いていません")
        elif rc == STEAMVR_QUIT:
            self._lost(self.batch, "途中で SteamVR が終わりました")
        elif rc != 0:
            self._lost(self.batch, "frame-care-toast がエラー %s で終わりました" % rc)
        self.proc, self.batch = None, []

    def _reap(self, why):
        """frame-care-toast を強制終了して片付ける（渡していたカードは出せなかったとログに書く）。"""
        try:
            self.proc.kill()
            self.proc.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        self._lost(self.batch, why)
        self.proc, self.batch, self.deadline = None, [], None

    def pump(self, steamvr):
        """前のカードが終わっていれば、溜まっている分を渡す。steamvr: SteamVR が動いているか。"""
        if self.busy():
            if time.monotonic() > self.deadline:
                self._reap("frame-care-toast が時間内に終わらないので止めました")
            return
        if self.proc is not None:
            self._finish()
        if not self.pending:
            return
        cards, self.pending = self.pending, []
        if not steamvr:
            self._lost(cards, "SteamVR が動いていません")
            return
        problem = toast_bin_problem(self.binary)
        if problem:
            if not self.missing_logged:
                log(problem)
                self.missing_logged = True
            self._lost(cards, "frame-care-toast がありません")
            return
        self.missing_logged = False
        try:
            self.proc = subprocess.Popen([self.binary] + self.args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        except OSError as e:
            log("frame-care-toast を起動できません:", e)
            self._lost(cards, "frame-care-toast を起動できません")
            return
        self.batch = cards
        self.deadline = time.monotonic() + len(cards) * self.seconds * 1.5 + self.slack
        try:
            self.proc.stdin.write("".join(toast_line(*c) for c in cards).encode("utf-8"))
            self.proc.stdin.close()
        except OSError:   # 読まずに終わった・閉じた: 残さず片付ける
            self._reap("frame-care-toast にカードを渡せませんでした")

    def wait(self, timeout=None):
        """出し終わるまで待つ（timeout を過ぎたら stop()）。全部出せたら True。"""
        if self.proc is None:
            return not self.pending
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.stop()
            return False
        ok = self.proc.returncode == 0
        self._finish()
        return ok

    def stop(self):
        """出している途中でも止める: SIGTERM（frame-care-toast は切断して終わる）→ 3 秒待つ → SIGKILL。"""
        if self.proc is None:
            return
        if self.busy():
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self._reap("frame-care-toast が止まらないので強制終了しました")
                return
        self._finish()


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


def proc_name_state(stat):
    """/proc/PID/stat の中身から (名前, 状態)。名前に空白や ) が入っていても読めるように、最後の ) で分ける。"""
    try:
        name = stat[stat.index("(") + 1:stat.rindex(")")]
        return name, stat[stat.rindex(")") + 1:].split()[0]
    except (ValueError, IndexError):
        return None, None


def steamvr_running(proc="/proc", uid=None):
    """自分（同じ uid）の vrserver が動いているか。ゾンビ（状態 Z）は数えない。"""
    uid = os.getuid() if uid is None else uid
    for d in os.listdir(proc):
        if not d.isdigit():
            continue
        path = os.path.join(proc, d)
        try:
            if os.stat(path).st_uid != uid:
                continue
        except OSError:
            continue
        name, state = proc_name_state(read(os.path.join(path, "stat")) or "")
        if name == "vrserver" and state != "Z":
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
        if self.fake:  # テスト用: JSON ファイルの値を毎回読み直す（steamvr が無ければ worn と同じ）
            with open(self.fake, encoding="utf-8") as f:
                r = json.load(f)
            r.setdefault("steamvr", bool(r.get("worn")))
            return r
        percent, status, online = power()
        pres = self.cfg["presence"]
        prox = proximity_raw()
        steamvr = steamvr_running()
        if pres.get("source") == "proximity" and pres.get("proximity_threshold") is not None and prox is not None:
            worn = prox > float(pres["proximity_threshold"])
        else:
            worn = steamvr
        return {"temp_c": soc_temp_c(), "battery": percent, "status": status, "online": online,
                "worn": worn, "proximity": prox, "steamvr": steamvr}


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
                out.append(("break", KINDS["break"]) + self._msg("break", minutes=minutes))
                self.break_mark = now
            h = c["hydration"]
            if h.get("enabled") and now - self.hydration_mark >= h["every_minutes"] * 60:
                out.append(("hydration", KINDS["hydration"]) + self._msg("hydration"))
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
                            out.append((key, KINDS[key]) + self._msg(key, level=level, percent=pct))
                        self.battery_fired.update(l for l in bc["levels"] if l >= level)
                        break

        # --- 温度（下がりきるまで同じ通知はくり返さない。高温だけは数分おきにくり返す）
        t = r.get("temp_c")
        tc = c["temperature"]
        if tc.get("enabled") and t is not None:
            if t >= tc["urgent_c"]:
                if self.temp_state != "urgent" or now - self.temp_urgent_at >= tc["urgent_repeat_minutes"] * 60:
                    out.append(("temp_urgent", KINDS["temp_urgent"]) + self._msg("temp_urgent", temp=t))
                    self.temp_urgent_at = now
                self.temp_state = "urgent"
            elif t >= tc["warn_c"]:
                if self.temp_state == "ok":
                    out.append(("temp_warn", KINDS["temp_warn"]) + self._msg("temp_warn", temp=t))
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
                # Frame はつないだままでも満タン近くで電池が Discharging になる（充電を休む）ので、それも完了とみなす
                full = r.get("status") == "Full" or pct >= 100 or (
                    pct >= cd.get("full_percent", 98) and r.get("status") != "Charging")
                if s["low_seen"] and not s["done"] and full:
                    out.append(("charge_done", KINDS["charge_done"]) + self._msg("charge_done", percent=pct))
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
                continue
            if key not in NO_RATE_LIMIT:
                if self.last_sent is not None and now - self.last_sent < rl["min_gap_seconds"]:
                    continue
                if len(self.sent_times) >= rl["max_per_hour"]:
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
    return "温度 %s / 電池 %s%% (%s, 電源%s) / かぶっている: %s / SteamVR: %s / 近接センサー %s" % (
        "%.1f°C" % r["temp_c"] if r.get("temp_c") is not None else "?", r.get("battery"), r.get("status"),
        "あり" if r.get("online") else "なし", "はい" if r.get("worn") else "いいえ",
        "動いている" if r.get("steamvr") else "動いていない", r.get("proximity"))


def problems(r, toast_bin):
    """知らせが正しく出せない理由の一覧（問題が無ければ空）。"""
    out = []
    p = toast_bin_problem(toast_bin)
    if p:
        out.append(p)
    if r.get("temp_c") is None:
        out.append("本体の温度が読めません（温度の知らせは出ません）")
    if r.get("battery") is None:
        out.append("電池の残りが読めません（電池と充電完了の知らせは出ません）")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Steam Frame の休憩・電池・温度のお知らせ")
    ap.add_argument("--config", default=default_config_path())
    ap.add_argument("--status", action="store_true", help="問題があるときだけ書いて終わる（問題が無ければ何も出さない）")
    ap.add_argument("-v", "--verbose", action="store_true", help="--status でいまの値も書く")
    ap.add_argument("--test-toast", action="store_true", help="テストのカードを 1 枚出す")
    ap.add_argument("--fake-sensors", help="テスト用: センサーの代わりにこの JSON を読む")
    ap.add_argument("--toast-bin", default=TOAST_BIN, help="テスト用: frame-care-toast の場所")
    ap.add_argument("--poll", type=float, help="値を読む間隔（秒）を上書き")
    ap.add_argument("--duration", type=float, help="この秒数で終わる（テスト用）")
    ap.add_argument("--version", action="version", version=VERSION)
    a = ap.parse_args(argv)

    try:
        cfg = load_config(a.config)
    except (OSError, ValueError) as e:
        print("設定ファイルが読めません: %s（%s）" % (a.config, e), file=sys.stderr)
        return 1
    if a.poll:
        cfg["poll_seconds"] = a.poll
    toaster = Toaster(cfg, a.toast_bin)
    sensors = Sensors(cfg, a.fake_sensors)

    if a.status:
        r = sensors.reading()
        found = problems(r, a.toast_bin)
        if a.verbose:
            print(describe(r))
            print("静かな時間:", "いま静かな時間です" if in_quiet(cfg["quiet_hours"], dt.datetime.now()) else "いいえ")
            print("設定:", a.config if os.path.exists(a.config) else "既定値")
        for p in found:
            print(p)
        return 1 if found else 0
    if a.test_toast:
        if not sensors.reading().get("steamvr"):
            print("SteamVR が動いていないので出せません", file=sys.stderr)
            return 1
        problem = toast_bin_problem(a.toast_bin)
        if problem:
            print(problem, file=sys.stderr)
            return 1
        toaster.send(KINDS["test"], *MESSAGES["test"])
        toaster.pump(True)
        return 0 if toaster.wait(30) else 1

    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    care = Care(cfg)
    t_end = time.monotonic() + a.duration if a.duration else None
    steamvr = False
    while not stop and (t_end is None or time.monotonic() < t_end):
        try:
            r = sensors.reading()
            steamvr = bool(r.get("steamvr"))
            now = clock()
            for key, kind, title, text in care.filter(now, dt.datetime.now(), care.evaluate(now, r)):
                toaster.send(kind, title, text)
        except Exception as e:  # センサーが一時的に読めなくても止まらない
            log("エラー:", repr(e))
        slept = 0.0
        while not stop and slept < cfg["poll_seconds"]:
            toaster.pump(steamvr)
            time.sleep(min(0.5, cfg["poll_seconds"]))
            slept += 0.5
    toaster.pump(steamvr)
    if stop:
        toaster.stop()      # systemd などに止められた: 出している途中でも切断して終わる
    else:
        toaster.wait(30)    # --duration で終わるとき（テスト）は出し終わるのを待つ
    return 0


if __name__ == "__main__":
    sys.exit(main())
