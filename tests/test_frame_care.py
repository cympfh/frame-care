#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""frame-care のテスト。センサーは作りものの値、通知は localhost の UDP で受けて確かめる。

  python3 tests/test_frame_care.py
"""
import datetime as dt
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location("fc", os.path.join(ROOT, "frame-care.py"))
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)

TEST_PORT = int(os.environ.get("FRAME_CARE_TEST_PORT", "39580"))  # 本物の 39570 は使わない
fails = []


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


def decode(buf):
    parts, i = [], 0
    while i < len(buf):
        end = buf.index(b"\0", i)
        parts.append(buf[i:end].decode("utf-8"))
        i = (end + 4) & ~3
    return parts


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
    # OSC
    msg = fc.osc_message("/wrist/notify", "care", "休憩しよう", "本文")
    check(len(msg) % 4 == 0 and decode(msg) == ["/wrist/notify", ",sss", "care", "休憩しよう", "本文"], "OSC: /wrist/notify ,sss")


def run_daemon(conf, fake, seconds):
    return subprocess.run([sys.executable, os.path.join(ROOT, "frame-care.py"), "--config", conf,
                           "--fake-sensors", fake, "--port", str(TEST_PORT), "--poll", "0.5",
                           "--duration", str(seconds)], capture_output=True, text=True, timeout=seconds + 20)


def e2e_tests():
    tmp = tempfile.mkdtemp(prefix="frame-care-test.")
    conf, fake = os.path.join(tmp, "config.json"), os.path.join(tmp, "sensors.json")
    with open(conf, "w") as f:
        json.dump({"break": {"every_minutes": 0.05}, "rate_limit": {"min_gap_seconds": 0}}, f)
    with open(fake, "w") as f:
        json.dump(R(temp_c=91.0, battery=9), f)
    # 受け手なし: ログだけ
    if fc.udp_port_bound(TEST_PORT):
        check(False, "テスト用ポート %d が使用中" % TEST_PORT)
        return
    p = run_daemon(conf, fake, 4)
    check("ログのみ" in p.stdout and "動いていない" in p.stdout, "受け手なし: ログに書くだけ")
    # 受け手あり
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", TEST_PORT))
    s.settimeout(0.2)
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "frame-care.py"), "--config", conf,
                             "--fake-sensors", fake, "--port", str(TEST_PORT), "--poll", "0.5", "--duration", "6"],
                            stdout=subprocess.PIPE, text=True)
    got, t0 = [], time.time()
    while time.time() - t0 < 8:
        try:
            got.append(decode(s.recvfrom(4096)[0]))
        except socket.timeout:
            if proc.poll() is not None:
                break
    out = proc.communicate()[0]
    titles = [g[3] for g in got]
    print("     受け取った:", titles)
    check(any(g[0] == "/wrist/notify" and g[2] == "alert" and "高温" in g[3] for g in got), "受け手あり: 高温の通知が届く")
    check(any("電池残りわずか" in t for t in titles), "受け手あり: 電池 9% の通知が届く")
    check(any("休憩" in t for t in titles), "受け手あり: 休憩の通知が届く（3 秒設定）")
    check("通知: " in out, "受け手あり: ログにも残る")


if __name__ == "__main__":
    unit_tests()
    e2e_tests()
    print("\n%s" % ("すべて OK" if not fails else "失敗 %d 件" % len(fails)))
    sys.exit(1 if fails else 0)
