#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""frame-care のテスト。センサーは作りものの値、カードは本物の代わりの frame-care-toast で受けて確かめる。
g++ と pango があれば、カードの描画（toast/card.h）の自己テストもする。

  python3 tests/test_frame_care.py

設定ファイルや XDG_CONFIG_HOME はすべて一時フォルダにする（本物の ~/.config には触らない）。
"""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location("fc", os.path.join(ROOT, "frame-care.py"))
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)

TMP = tempfile.mkdtemp(prefix="frame-care-test.")
ENV = dict(os.environ, XDG_CONFIG_HOME=os.path.join(TMP, "xdg-config"), HOME=os.path.join(TMP, "home"))
fails = []

# 本物の frame-care-toast の代わり: 引数と受け取った行をファイルに書き、FAKE_TOAST_RC の終了コードで終わる
FAKE_TOAST = """#!%s
import os, sys
with open(os.environ["FAKE_TOAST_OUT"], "a", encoding="utf-8") as f:
    f.write("ARGS " + " ".join(sys.argv[1:]) + "\\n")
    f.write(sys.stdin.buffer.read().decode("utf-8"))
sys.exit(int(os.environ.get("FAKE_TOAST_RC", "0")))
""" % sys.executable

# 固まる frame-care-toast: SIGTERM も無視して眠り続ける（FAKE_HANG_CLOSE があれば、先に標準入力を閉じる）
HANG_TOAST = """#!%s
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if os.environ.get("FAKE_HANG_CLOSE"):
    os.close(0)
time.sleep(600)
""" % sys.executable


def script(name, body):
    path = os.path.join(TMP, name)
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, 0o755)
    return path


def quiet(f, *a):
    """f を呼び、ログ（stdout）を返す。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        f(*a)
    return buf.getvalue()


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        fails.append(what)


def R(**kw):
    base = {"temp_c": 50.0, "battery": 80, "status": "Discharging", "online": False, "worn": True}
    base.update(kw)
    return base


def keys(items):
    return [i[0] for i in items]


def cfg(**over):
    return fc.merge(fc.DEFAULTS, over)


def unit_tests():
    # 休憩: 60 分連続でかぶると 1 回、そこからまた 60 分後
    c = fc.Care(cfg())
    got = []
    for m in range(0, 125):
        got += keys(c.evaluate(m * 60.0, R()))
    check(got.count("break") == 2, "休憩: 124 分で 2 回")
    # 5 分以上外すとリセット
    c = fc.Care(cfg())
    got = []
    for m in range(0, 50):
        got += keys(c.evaluate(m * 60.0, R()))
    for m in range(50, 57):
        got += keys(c.evaluate(m * 60.0, R(worn=False)))
    for m in range(57, 100):
        got += keys(c.evaluate(m * 60.0, R()))
    check("break" not in got, "休憩: 途中で 7 分外したらカウントし直す")
    # スリープ（時計が大きく飛ぶ）でもリセット
    c = fc.Care(cfg())
    c.evaluate(0, R()); c.evaluate(59 * 60, R())
    out = keys(c.evaluate(59 * 60 + 3600, R()))
    check("break" not in out, "休憩: スリープ明けはカウントし直す")
    # 水分補給（オンのとき）
    c = fc.Care(cfg(hydration={"enabled": True, "every_minutes": 45}))
    got = []
    for m in range(0, 46):
        got += keys(c.evaluate(m * 60.0, R()))
    check(got.count("hydration") == 1, "水分補給: 45 分で 1 回")
    # 電池: 20% と 10% で 1 回ずつ、充電で再び有効
    c = fc.Care(cfg())
    got = []
    for p in [25, 21, 20, 19, 18, 15, 11, 10, 9, 8]:
        got += keys(c.evaluate(0, R(battery=p, worn=False)))
    check(got == ["battery", "battery_urgent"], "電池: 20%% と 10%% で 1 回ずつ（%s）" % got)
    c.evaluate(0, R(battery=30, online=True, status="Charging", worn=False))
    got = keys(c.evaluate(0, R(battery=19, worn=False)))
    check(got == ["battery"], "電池: 充電したあとはまた知らせる")
    c = fc.Care(cfg())
    check(keys(c.evaluate(0, R(battery=7, worn=False))) == ["battery_urgent"], "電池: 起動時すでに 7% なら緊急だけ")
    # 温度
    c = fc.Care(cfg())
    seq = [(0, 70), (30, 86), (60, 87), (90, 84), (120, 86), (150, 91), (180, 91), (360, 92), (400, 78), (430, 86)]
    got = [(t, k) for t, temp in seq for k in keys(c.evaluate(t, R(temp_c=temp, worn=False)))]
    check([k for _, k in got] == ["temp_warn", "temp_urgent", "temp_urgent", "temp_warn"],
          "温度: 85 で注意、90 で警告、3 分おきに再警告、80 未満で解除（%s）" % got)
    # 充電完了
    c = fc.Care(cfg())
    got = []
    for p, st in [(60, "Charging"), (90, "Charging"), (99, "Charging"), (100, "Full"), (100, "Full")]:
        got += keys(c.evaluate(0, R(battery=p, status=st, online=True, worn=False)))
    check(got == ["charge_done"], "充電完了: 1 回だけ")
    c = fc.Care(cfg())
    got = []
    for p, st in [(70, "Charging"), (97, "Charging"), (98, "Discharging"), (97, "Discharging")]:
        got += keys(c.evaluate(0, R(battery=p, status=st, online=True, worn=False)))
    check(got == ["charge_done"], "充電完了: つないだまま 98% で充電が止まった場合も 1 回")
    c = fc.Care(cfg())
    got = keys(c.evaluate(0, R(battery=100, status="Full", online=True, worn=False)))
    check(got == [], "充電完了: 満タンのままつないだだけなら言わない")
    # 静かな時間
    qc = cfg(quiet_hours={"enabled": True, "start": "23:00", "end": "07:00", "allow_urgent": True})
    c = fc.Care(qc)
    items = [("break", "care", "t", "x"), ("temp_urgent", "alert", "t", "x")]
    out = keys(c.filter(0, dt.datetime(2026, 10, 18, 2, 0), items))
    check(out == ["temp_urgent"], "静かな時間: 緊急だけ通す")
    check(not fc.in_quiet(qc["quiet_hours"], dt.datetime(2026, 10, 18, 12, 0)), "静かな時間: 昼は対象外")
    # 回数制限
    c = fc.Care(cfg(rate_limit={"min_gap_seconds": 20, "max_per_hour": 2}))
    w = dt.datetime(2026, 10, 18, 12, 0)
    sent = []
    for t in [0, 5, 30, 60, 90]:
        sent += keys(c.filter(t, w, [("break", "care", "t", "x")]))
    check(len(sent) == 2, "回数制限: 20 秒以内と 1 時間 2 回を超えた分は出さない")
    check(keys(c.filter(100, w, [("temp_urgent", "alert", "t", "x")])) == ["temp_urgent"], "回数制限: 高温は制限なし")
    # カードの種類と、frame-care-toast に渡す行
    check(set(fc.KINDS) == set(fc.MESSAGES) and all(v.split(".")[0] in ("alert", "warn", "care") for v in fc.KINDS.values())
          and all(fc.KINDS[k].startswith("alert.") for k in fc.URGENT), "種類: すべての通知に alert / warn / care が付き、急ぎは alert")
    check(fc.toast_line("care.break", "休憩\tしよう", "1 行目\n2 行目") == "care.break\t休憩 しよう\t1 行目 2 行目\n",
          "渡す行: タブと改行は空白にして 1 行にする")
    args = fc.toast_args(cfg(toast={"duration_seconds": 6}))
    check(args[args.index("--duration-s") + 1] == "6" and "--distance-m" in args, "渡す引数: 設定の toast から")
    check(fc.toast_bin_problem(os.path.join(TMP, "無い")) is not None, "frame-care-toast が無ければ問題として返す")
    check(fc.problems({"temp_c": 50, "battery": 80}, sys.executable) == [] and
          len(fc.problems({"temp_c": None, "battery": None}, sys.executable)) == 2, "問題の一覧: 無ければ空、読めない値は書く")
    check(not any(k.startswith("toast_") for k in fc.DEFAULTS), "UDP の送り先の設定は無い")


def proc_tests():
    """steamvr_running(): 自分の uid の vrserver だけ、ゾンビは数えない。"""
    root = os.path.join(TMP, "proc")

    def fake(pid, stat):
        os.makedirs(os.path.join(root, pid), exist_ok=True)
        with open(os.path.join(root, pid, "stat"), "w") as f:
            f.write(stat)

    os.makedirs(root, exist_ok=True)
    fake("10", "10 (bash) S 1 10 10 0")
    check(not fc.steamvr_running(root), "vrserver: いなければ False")
    fake("20", "20 (vrserver) Z 1 20 20 0")
    check(not fc.steamvr_running(root), "vrserver: ゾンビ（Z）は数えない")
    fake("30", "30 (vrserver) S 1 30 30 0")
    check(fc.steamvr_running(root), "vrserver: 動いていれば True")
    check(not fc.steamvr_running(root, uid=os.getuid() + 1), "vrserver: ほかのユーザーのものは数えない")
    check(fc.proc_name_state("5 (a) b) c) R 1") == ("a) b) c", "R"), "stat: 名前に ) が入っていても読める")
    check(fc.proc_name_state("ゴミ") == (None, None), "stat: 読めなければ None")


def toaster_tests():
    """固まった frame-care-toast を止められる・溜めすぎない・渡せなければ片付ける。"""
    hang = script("hang-toast", HANG_TOAST)
    fast = cfg(toast={"duration_seconds": 0.01})
    # 時間切れ: 枚数 × 秒数 × 1.5 + 余裕 を過ぎたら kill
    t = fc.Toaster(fast, hang)
    t.slack = 0.5
    t.send("care", "固まるテスト", "x")
    quiet(t.pump, True)
    check(t.busy() and t.deadline is not None, "時間切れ: 渡したら期限を決める")
    proc = t.proc
    time.sleep(0.8)
    t0 = time.monotonic()
    out = quiet(t.pump, True)
    check(t.proc is None and proc.returncode is not None and "時間内に終わらない" in out and "固まるテスト" in out
          and time.monotonic() - t0 < 6, "時間切れ: kill して片付け、出せなかったカードをログに書く")
    d = fc.Toaster(cfg(), hang)
    d.send("care", "a", "x"); d.send("care", "b", "x")
    quiet(d.pump, True)
    check(abs(d.deadline - time.monotonic() - (2 * 4 * 1.5 + fc.DEADLINE_SLACK)) < 1, "時間切れ: 期限は 2 枚 × 4 秒 × 1.5 + 15 秒")
    quiet(d.stop)
    # stop(): SIGTERM を無視されても、3 秒待って kill。すぐ戻る
    t = fc.Toaster(fast, hang)
    t.send("care", "止めるテスト", "x")
    quiet(t.pump, True)
    proc = t.proc
    time.sleep(0.3)
    t0 = time.monotonic()
    out = quiet(t.stop)
    took = time.monotonic() - t0
    check(t.proc is None and proc.returncode is not None and took < 5, "stop(): 固まっていても %.1f 秒で戻る（terminate → 3 秒 → kill）" % took)
    check("強制終了" in out, "stop(): 強制終了したらログに書く")
    # 溜めるのは MAX_PENDING 枚まで（古いものから捨てる）
    t = fc.Toaster(fast, hang)
    out = ""
    for i in range(fc.MAX_PENDING + 5):
        out += quiet(t.send, "care", "カード%d" % i, "x")
    check(len(t.pending) == fc.MAX_PENDING and t.pending[0][1] == "カード5" and out.count("捨てました") == 5,
          "溜めすぎ: 上限を超えたら古いものから捨てる")
    # 標準入力に書けない（読まずに閉じた）: kill して片付ける（ゾンビを残さない）
    os.environ["FAKE_HANG_CLOSE"] = "1"
    try:
        t = fc.Toaster(fast, hang)
        for i in range(fc.MAX_PENDING):
            t.send("care", "長い%d" % i, "あ" * 3000)   # パイプに入りきらない量（約 180KB）
        t0 = time.monotonic()
        out = quiet(t.pump, True)
    finally:
        del os.environ["FAKE_HANG_CLOSE"]
    check(t.proc is None and "渡せませんでした" in out and time.monotonic() - t0 < 6, "標準入力に書けなければ kill して片付ける")


def build_tests():
    """toast/build.sh --find-lib: openvrpaths.vrpath、Steam のライブラリの順に libopenvr_api.so を探す。"""
    base = tempfile.mkdtemp(dir=TMP)
    sub = "linuxarm64" if os.uname().machine in ("aarch64", "arm64") else "linux64"
    home = os.path.join(base, "home")
    vrpath_dir = os.path.join(home, ".config", "openvr")
    steamapps = os.path.join(home, ".local", "share", "Steam", "steamapps")
    rt, lib2 = os.path.join(base, "rt"), os.path.join(base, "lib2")
    for d in (os.path.join(rt, "bin", sub), os.path.join(lib2, "steamapps", "common", "SteamVR", "bin", sub)):
        os.makedirs(d)
        open(os.path.join(d, "libopenvr_api.so"), "w").close()
    os.makedirs(vrpath_dir)
    os.makedirs(steamapps)

    def find():
        env = dict(os.environ, HOME=home)
        env.pop("XDG_CONFIG_HOME", None)
        env.pop("STEAMVR_LIB", None)
        r = subprocess.run(["bash", os.path.join(ROOT, "toast", "build.sh"), "--find-lib"], capture_output=True,
                           text=True, env=env)
        return r.returncode, r.stdout.strip()

    with open(os.path.join(vrpath_dir, "openvrpaths.vrpath"), "w") as f:
        json.dump({"runtime": [os.path.join(base, "無い"), rt]}, f)
    check(find() == (0, os.path.join(rt, "bin", sub)), "build.sh: openvrpaths.vrpath の runtime から探す")
    os.remove(os.path.join(vrpath_dir, "openvrpaths.vrpath"))
    with open(os.path.join(steamapps, "libraryfolders.vdf"), "w") as f:
        f.write('"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n' % lib2)
    check(find() == (0, os.path.join(lib2, "steamapps", "common", "SteamVR", "bin", sub)), "build.sh: Steam のライブラリから探す")
    os.remove(os.path.join(steamapps, "libraryfolders.vdf"))
    check(find()[0] == 1, "build.sh: 見つからなければ 1 で終わる")


def run(args, timeout=30, **env):
    e = dict(ENV, **env)
    return subprocess.run([sys.executable, os.path.join(ROOT, "frame-care.py")] + args, capture_output=True,
                          text=True, timeout=timeout, env=e)


def e2e_tests():
    tmp = tempfile.mkdtemp(dir=TMP)
    conf, fake, out = os.path.join(tmp, "config.json"), os.path.join(tmp, "sensors.json"), os.path.join(tmp, "toast.txt")
    toast = os.path.join(tmp, "frame-care-toast")
    with open(toast, "w") as f:
        f.write(FAKE_TOAST)
    os.chmod(toast, 0o755)
    with open(conf, "w") as f:
        json.dump({"break": {"every_minutes": 0.05}, "rate_limit": {"min_gap_seconds": 0}}, f)

    def sensors(**kw):
        with open(fake, "w") as f:
            json.dump(R(**kw), f)

    def daemon(seconds=4, **env):
        if os.path.exists(out):
            os.remove(out)
        return run(["--config", conf, "--fake-sensors", fake, "--toast-bin", toast, "--poll", "0.5",
                    "--duration", str(seconds)], timeout=seconds + 40, FAKE_TOAST_OUT=out, **env)

    def got():
        return open(out, encoding="utf-8").read() if os.path.exists(out) else ""

    # SteamVR が動いている: カードを frame-care-toast に渡し、ログには何も書かない
    sensors(temp_c=91.0, battery=9, steamvr=True)
    p = daemon()
    g = got()
    lines = [l.split("\t") for l in g.splitlines() if "\t" in l]
    print("     渡した:", [l[1] for l in lines])
    check(any(l[0] == "alert.temp" and "高温" in l[1] for l in lines), "SteamVR あり: 高温のカード（alert.temp）を渡す")
    check(any(l[0] == "alert.battery" and "電池残りわずか" in l[1] for l in lines), "SteamVR あり: 電池 9% のカードを渡す")
    check(any(l[0] == "care.break" and "休憩" in l[1] for l in lines), "SteamVR あり: 休憩のカードを渡す（3 秒設定）")
    check("--duration-s 4" in g and "--distance-m 0.8" in g, "SteamVR あり: 表示の設定を引数で渡す")
    check(p.returncode == 0 and p.stdout == "" and p.stderr == "", "SteamVR あり: うまくいったときは何も出力しない（%r）" % (p.stdout + p.stderr))
    # SteamVR が動いていない: frame-care-toast を呼ばず、出せなかったカードだけログに書く
    sensors(temp_c=91.0, battery=9, steamvr=False, worn=False)
    p = daemon(3)
    check(got() == "" and "出せませんでした（SteamVR が動いていません）" in p.stdout and "高温" in p.stdout,
          "SteamVR なし: 呼ばずに、出せなかったカードをログに書く")
    # frame-care-toast が「SteamVR が動いていない」(3) で終わった
    sensors(temp_c=91.0, battery=80, steamvr=True, worn=False)
    p = daemon(3, FAKE_TOAST_RC="3")
    check("出せませんでした（SteamVR が動いていません）: 本体が高温" in p.stdout, "frame-care-toast が 3 で終わったら、出せなかったとログに書く")
    # frame-care-toast が無い: 1 回だけ理由を書き、カードはログに
    p = run(["--config", conf, "--fake-sensors", fake, "--toast-bin", os.path.join(tmp, "無い"), "--poll", "0.5",
             "--duration", "3"])
    check(p.stdout.count("frame-care-toast がありません（install.sh") == 1 and "本体が高温" in p.stdout,
          "frame-care-toast が無い: 理由を 1 回書き、カードはログに")
    # --status: 問題が無ければ何も出さない。-v で値。問題があれば書いて 1
    p = run(["--config", conf, "--fake-sensors", fake, "--toast-bin", toast, "--status"])
    check(p.returncode == 0 and p.stdout == "" and p.stderr == "", "--status: 問題が無ければ何も出さない")
    p = run(["--config", conf, "--fake-sensors", fake, "--toast-bin", toast, "--status", "-v"])
    check(p.returncode == 0 and "温度 91.0°C" in p.stdout and "SteamVR: 動いている" in p.stdout, "--status -v: いまの値を書く")
    sensors(temp_c=None, battery=80, steamvr=True)
    p = run(["--config", conf, "--fake-sensors", fake, "--toast-bin", os.path.join(tmp, "無い"), "--status"])
    check(p.returncode == 1 and "frame-care-toast がありません" in p.stdout and "温度が読めません" in p.stdout,
          "--status: 問題があれば書いて 1 で終わる")
    # --test-toast
    sensors(steamvr=True)
    if os.path.exists(out):
        os.remove(out)
    p = run(["--config", conf, "--fake-sensors", fake, "--toast-bin", toast, "--test-toast"], FAKE_TOAST_OUT=out)
    check(p.returncode == 0 and p.stdout == "" and "care\tframe-care\tテスト通知です" in got(), "--test-toast: 1 枚渡して、何も出力しない")
    sensors(steamvr=False, worn=False)
    p = run(["--config", conf, "--fake-sensors", fake, "--toast-bin", toast, "--test-toast"], FAKE_TOAST_OUT=out)
    check(p.returncode == 1 and "SteamVR が動いていない" in p.stderr, "--test-toast: SteamVR が動いていなければ 1 で終わる")
    # 本物の設定フォルダに何も作っていない
    check(not os.path.exists(os.path.join(ENV["XDG_CONFIG_HOME"], "frame-care")), "設定フォルダを作らない")


def card_tests():
    """toast/card.h の自己テスト（g++ と pangocairo があるときだけ）。"""
    if not shutil.which("g++") or subprocess.run(["pkg-config", "--exists", "pangocairo"]).returncode != 0:
        print("skip カードの描画（g++ か pangocairo がありません）")
        return
    src, exe = os.path.join(TMP, "card_test.cpp"), os.path.join(TMP, "card_test")
    with open(src, "w") as f:
        f.write('#include "card.h"\nint main() { return card::self_test() ? 1 : 0; }\n')
    flags = subprocess.run(["pkg-config", "--cflags", "--libs", "pangocairo", "cairo"], capture_output=True,
                           text=True).stdout.split()
    b = subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", os.path.join(ROOT, "toast"), "-o", exe,
                        src] + flags, capture_output=True, text=True)
    check(b.returncode == 0, "カードの描画: ビルドできる %s" % b.stderr[-500:])
    if b.returncode == 0:
        r = subprocess.run([exe], capture_output=True, text=True)
        check(r.returncode == 0, "カードの描画: 自己テスト %s" % r.stdout)


if __name__ == "__main__":
    unit_tests()
    proc_tests()
    toaster_tests()
    build_tests()
    e2e_tests()
    card_tests()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n%s" % ("すべて OK" if not fails else "失敗 %d 件" % len(fails)))
    sys.exit(1 if fails else 0)
