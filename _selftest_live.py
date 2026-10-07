# -*- coding: utf-8 -*-
"""联网冒烟: 真实平台调用验证 fetch_steam/fetch_dlsite 新字段(FR-05/07/08/12)。"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import pull
import model as MOD

checks = []


def ck(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


# 1. 免费 demo appid
s = pull.fetch_steam("5141500")
ck("Steam 5141500 名称", s["name"] == "Sword Saint Soriel Demo")
ck("Steam 5141500 type=demo(is_free)", s["is_free"] is True)
ck("Steam 5141500 genres 有值", len(s["genres"]) >= 3)
ck("Steam 5141500 语言有值", "English" in s["languages"])
ck("Steam 5141500 demos字段存在", isinstance(s["demos"], list))
ck("Steam 5141500 发售日解析", pull.parse_steam_date(s["date"]) == "2026-09-01")

# 2. 本体带 demo
s2 = pull.fetch_steam("3448460")
ck("Steam 3448460 本体有 demo", s2["demos"] == ["5141500"])
ck("Steam 3448460 coming soon", s2["coming"] is True)
ck("FR-12 本体demo状态=page", MOD.demo_state_from_steam(s2["demos"]) == "page")
ck("FR-12 无demo本体=none", MOD.demo_state_from_steam(s["demos"]) == "none")

# 3. 付费游戏价格(带 cc)
s3 = pull.fetch_steam("367520", cc="cn")
ck("Steam 367520 价格有currency", s3["price"] and s3["price"]["currency"] == "CNY")
ck("Steam 367520 价格地区=CN", s3["price"]["region"] == "CN")
pt = pull.price_text(s3["price"])
ck("FR-07 价格文案含货币地区", "CNY" in pt and "CN" in pt)
ck("FR-07 未标价不算免费", "未标价" in pull.price_text(None) or pull.price_text(None) == "")

# 4. DLsite
d = pull.fetch_dlsite("RJ01724797")
ck("DLsite 販売日", d["date"] == "2026-10-06")
ck("DLsite 标题去尾", "DLsite" not in d["title"] and d["title"])
ck("FR-07 DLsite 价格 JPY/JP",
   d["price"] and d["price"]["currency"] == "JPY" and d["price"]["region"] == "JP")
ck("DLsite 现价2400", d["price"]["final"] == 2400)
ck("DLsite 定价3300", d["price"]["initial"] == 3300)
ck("DLsite 折扣率计算", d["price"]["discount"] > 0)
ck("FR-12 DLsite 体験版", d["trial"] is True and "trial.dlsite.com" in d["trial_url"])
ck("FR-08 DLsite 分类标签", len(d["genres"]) >= 1)
ck("FR-08 DLsite 语言", "JPN" in d["languages"] and "ENG" in d["languages"])

# 5. 无体験版对照
d2 = pull.fetch_dlsite("RJ01724124")
ck("DLsite 对照页价格", d2["price"] and d2["price"]["final"] > 0)

# 6. 事实落库全链路(用临时 graph)
out = os.path.join(BASE, "_selftest_out")
os.makedirs(out, exist_ok=True)
orig = MOD.GRAPH_PATH
MOD.GRAPH_PATH = os.path.join(out, "graph_live.json")
if os.path.exists(MOD.GRAPH_PATH):
    os.remove(MOD.GRAPH_PATH)
try:
    g = MOD.load_graph()
    MOD.record_fact(g, "SmokeW", "release_date", "2026-09-01", "Steam",
                    url="https://store.steampowered.com/app/5141500",
                    status="released")
    MOD.record_fact(g, "SmokeW", "price", pull.price_text(s3["price"]), "Steam",
                    url="https://store.steampowered.com/app/367520", status="ok")
    MOD.record_fact(g, "SmokeW", "demo", "page", "Steam", status="page",
                    note=",".join(s2["demos"]))
    MOD.record_fact(g, "SmokeW", "genres", ", ".join(s["genres"]), "Steam")
    MOD.save_graph(g)
    g2 = MOD.load_graph()
    ck("事实落库回读", MOD.latest_fact(g2, "SmokeW", "price", src="Steam")["v"] == pt)
finally:
    MOD.GRAPH_PATH = orig

ok = 0
for _, p in checks:
    if p:
        ok += 1
print("%d/%d" % (ok, len(checks)))
sys.exit(0 if ok == len(checks) else 1)
