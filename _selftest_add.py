# -*- coding: utf-8 -*-
"""add_game 自测: FR-01 失败留存 / FR-03 候选 / FR-04 同作者多作品查重。
联网函数全部 monkeypatch, 离线运行; 写入隔离的 watchlist/graph/events。"""
import csv
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import add_game
import model as MOD

checks = []


def ck(name, cond):
    checks.append((name, bool(cond)))


# ---- 隔离文件(工作区内, 沙箱不许写系统 Temp) ----
out = os.path.join(BASE, "_selftest_out")
os.makedirs(out, exist_ok=True)
orig = {"csv": add_game.CSV_PATH, "events": add_game.EVENTS_PATH,
        "graph": MOD.GRAPH_PATH}
add_game.CSV_PATH = os.path.join(out, "watchlist.csv")
add_game.EVENTS_PATH = os.path.join(out, "events.csv")
MOD.GRAPH_PATH = os.path.join(out, "graph.json")
for p in (add_game.CSV_PATH, add_game.EVENTS_PATH, MOD.GRAPH_PATH):
    if os.path.exists(p):
        os.remove(p)

# ---- monkeypatch 联网 ----
add_game.steam_info = lambda appid: ("Fake Game %s" % appid,
                                     "https://example.com/h.jpg")
add_game.M.pick_cover = lambda *a, **k: "covers/fake.jpg"
add_game.M._save = lambda *a, **k: "covers/fake.jpg"


class _FakeCLS:
    @staticmethod
    def classify(*a, **k):
        return "游戏"


sys.modules["classify"] = _FakeCLS

try:
    rows = []

    # ===== FR-04: 同作者 + 新平台页 = 新作品(不再被 handle 拦) =====
    # 先建一个已有作品(同 handle, 无平台页的作者行)
    ok1 = add_game.add_one("https://x.com/devx", rows)
    ck("作者主页入库", ok1 and rows[0]["x_handle"] == "devx")
    # 同 handle 再来纯主页 -> 拦截
    ok2 = add_game.add_one("https://x.com/devx", rows)
    ck("FR-04 纯主页重复拦截", ok2 is False and len(rows) == 1)
    # 同 handle + Steam 页 -> 视为新作品, 允许入库
    ok3 = add_game.add_one("https://store.steampowered.com/app/111222", rows)
    ck("FR-04 同作者新作品放行", ok3 is True and len(rows) == 2)
    ck("FR-04 原作者行保留", rows[0]["x_handle"] == "devx")
    ck("FR-04 纯Steam输入无handle(不冒认作者)", rows[1]["x_handle"] == "")
    ck("FR-04 新作品带平台页", rows[1]["steam_appid"] == "111222")
    # 同 Steam appid 再来 -> 平台 id 强查重
    ok4 = add_game.add_one("https://store.steampowered.com/app/111222", rows)
    ck("FR-04 平台id强查重", ok4 is False and len(rows) == 2)

    # ===== FR-01: 未识别输入留存 =====
    g = MOD.load_graph()
    ok5 = add_game.add_one("https://example.com/not-a-platform", rows)
    ck("FR-01 未识别返回False", ok5 is False)
    g2 = MOD.load_graph()
    ck("FR-01 未识别输入已留存",
       any("example.com/not-a-platform" in it.get("input", "")
           for it in g2.get("input_errors", [])))
    ck("FR-01 留存带原因",
       any("没识别出" in it.get("reason", "")
           for it in g2.get("input_errors", [])))

    # ===== FR-05: 入库记录事实(页面/作者) =====
    g3 = MOD.load_graph()
    ck("FR-05 入库记 Steam 页事实",
       MOD.latest_fact(g3, rows[1]["name"], "page", src="add") is not None)
    ck("FR-05 入库记作者事实",
       any(f.get("v") == "@devx"
           for f in MOD.fact_history(g3, rows[0]["name"], "creator")))

    # ===== FR-02: 简介外链 -> 候选(通过 fxtwitter_user 注入) =====
    add_game.M.fxtwitter_user = lambda h, tries=3: {
        "screen_name": h, "name": "Dev X",
        "description": "My game https://store.steampowered.com/app/999888 bye"}
    rows2 = []
    ok6 = add_game.add_one("https://x.com/devy", rows2)
    ck("FR-02 简介外链入库识别", ok6 is True)
    g4 = MOD.load_graph()
    cands = [c for c in g4["candidates"]
             if "999888" in (c.get("url") or "")]
    ck("FR-02 简介外链进候选", len(cands) == 1)
    ck("FR-02 简介候选证据来自简介",
       cands and any("简介" in str(x.get("from", ""))
                     for x in (cands[0].get("evidence") or [])))
    ck("FR-02 简介外链直接并入本行", rows2 and "999888" in (rows2[0]["steam_appid"] or ""))

    # ===== FR-02: 转发帖链接只进候选(低置信), 不并入本行 =====
    add_game.M.fxtwitter_post = lambda u, s, tries=3: {
        "tweet": {"text": "check this https://store.steampowered.com/app/555111",
                  "author": {"screen_name": "devz", "name": "Dev Z"},
                  "reposted_by": {"screen_name": "someone"},
                  "replying_to": None}}
    rows3 = []
    ok7 = add_game.add_one("https://x.com/devz/status/999999", rows3)
    g5 = MOD.load_graph()
    rt_cands = [c for c in g5["candidates"] if "555111" in (c.get("url") or "")]
    ck("FR-02 转发链接进候选", len(rt_cands) == 1)
    ck("FR-02 转发候选低置信", rt_cands and rt_cands[0]["confidence"] < 0.5)
    ck("FR-02 转发链接不并入本行",
       rows3 and "555111" not in (rows3[0]["steam_appid"] or ""))

    # ===== extract_links 仍工作(回归) =====
    lk = add_game.extract_links("a https://x.com/x b https://store.steampowered.com/app/1 c")
    ck("extract_links 回归", len(lk) == 2)
finally:
    add_game.CSV_PATH = orig["csv"]
    add_game.EVENTS_PATH = orig["events"]
    MOD.GRAPH_PATH = orig["graph"]

ok = 0
for name, passed in checks:
    print(("PASS " if passed else "FAIL ") + name)
    if passed:
        ok += 1
print("%d/%d" % (ok, len(checks)))
sys.exit(0 if ok == len(checks) else 1)
