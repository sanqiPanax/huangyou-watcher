# -*- coding: utf-8 -*-
"""FR-03 闭环实测: 建候选 -> POST /api/candidate confirm -> 验证挂载与事实 -> 回滚。"""
import io
import json
import os
import sys
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import model as MOD
import add_game

API = "http://127.0.0.1:8790"
checks = []


def ck(n, c):
    checks.append((n, bool(c)))
    print(("PASS " if c else "FAIL ") + n)


def post(path, obj):
    req = urllib.request.Request(API + path, data=json.dumps(obj).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


# 1. 建一个 pending 候选(假 appid, 不会真抓)
g = MOD.load_graph()
rows = add_game.load_rows()
target = rows[0]["name"]
before_apps = rows[0].get("steam_appid") or ""
cand, _ = MOD.add_candidate(g, "https://store.steampowered.com/app/99999999", "steam",
                            title="FakeForTest", evidence=[{"from": "自测", "retweet": False}],
                            confidence=0.5, reason="自测候选")
MOD.save_graph(g)
ck("候选已建", cand and cand["status"] == "pending")

# 2. API confirm 到已有作品
r = post("/api/candidate", {"id": cand["id"], "action": "confirm", "target": target})
ck("API confirm 返回ok", r.get("ok") is True and r.get("appended") == "Steam 99999999")

# 3. 回读验证 watchlist + graph
rows2 = add_game.load_rows()
hit = next(x for x in rows2 if x["name"] == target)
ck("平台页已挂到作品", "99999999" in (hit.get("steam_appid") or ""))
g2 = MOD.load_graph()
c2 = next(c for c in g2["candidates"] if c["id"] == cand["id"])
ck("候选状态=confirmed", c2["status"] == "confirmed" and c2["target_row"] == target)
pf = MOD.latest_fact(g2, target, "page", src="confirm")
ck("确认动作留事实证据", pf is not None and "99999999" in (pf.get("v") or ""))

# 4. 回滚 watchlist(去掉假 appid)
new_apps = [a for a in hit.get("steam_appid", "").split(",") if a and a != "99999999"]
hit["steam_appid"] = ",".join(new_apps)
add_game.save_rows(rows2)
rows3 = add_game.load_rows()
ck("回滚后 watchlist 复原", (rows3[0].get("steam_appid") or "") == before_apps)

# 5. reject 路径
g3 = MOD.load_graph()
cand2, _ = MOD.add_candidate(g3, "https://store.steampowered.com/app/88888888", "steam")
MOD.save_graph(g3)
r2 = post("/api/candidate", {"id": cand2["id"], "action": "reject"})
ck("API reject 返回ok", r2.get("ok") is True)
g4 = MOD.load_graph()
ck("reject 状态落盘", next(c for c in g4["candidates"]
                            if c["id"] == cand2["id"])["status"] == "rejected")

# 6. 清理两个测试候选(不给真实数据留垃圾)
g5 = MOD.load_graph()
g5["candidates"] = [c for c in g5["candidates"]
                    if c.get("title") not in ("FakeForTest",) and
                    "88888888" not in (c.get("url") or "")]
MOD.save_graph(g5)
g6 = MOD.load_graph()
ck("清理后无测试候选残留",
   not any("99999999" in (c.get("url") or "") or "88888888" in (c.get("url") or "")
           for c in g6["candidates"]))
# confirm 留下的 page 事实也清掉
if target in g6.get("facts", {}):
    lst = g6["facts"][target].get("page") or []
    g6["facts"][target]["page"] = [f for f in lst if "99999999" not in (f.get("v") or "")]
    MOD.save_graph(g6)
ck("清理后无假事实残留",
   not any("99999999" in (f.get("v") or "")
           for f in (MOD.load_graph()["facts"].get(target, {}).get("page") or [])))

ok = sum(1 for _, p in checks if p)
print("%d/%d" % (ok, len(checks)))
sys.exit(0 if ok == len(checks) else 1)
