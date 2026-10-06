# -*- coding: utf-8 -*-
"""渲染自测 v2: 封面 + 更新媒体(图/视频) + 裁剪 + 已读交互结构"""
import sys, os, io, json

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import pull

game = "Sword Saint Soriel Demo"
# 构造带媒体的更新历史
updates = {game: [
    {"t": "2026-10-05 18:00:00", "src": "X",
     "text": "X @XIVYR0 新帖: demo release https://x.com/XIVYR0/status/2106414871533568417",
     "media": {"images": ["media/2106414871533568417_1.jpg"],
               "video": {"mp4": "https://video.twimg.com/x.mp4?tag=29", "poster": "media/p.jpg"}}},
    {"t": "2026-10-04 18:00:00", "src": "Steam", "text": "Steam 发售日变化: a -> b"},
    {"t": "2026-10-03 18:00:00", "src": "DLsite", "text": "DLsite 販売日: 2024-01-01 -> 2024-02-01"},
]}

rows = pull.load_rows()
# 给第一行假封面测渲染
rows[0]["cover"] = "covers/test_cover.jpg"
changes = {id(rows[0]): [("X", "X @XIVYR0 新帖: hi https://x.com/XIVYR0/status/1",
                          {"images": ["media/x_1.jpg"], "video": None})]}
pull.build_report(rows, changes, [], {"time": "2026-10-05 18:00:00", "elapsed": "1.0s"},
                  updates, 5)

out = open(pull.REPORT_PATH, encoding="utf-8").read()
checks = [
    ("封面img渲染", "class='cover' src='covers/test_cover.jpg'" in out),
    ("封面onerror兜底", "onerror=" in out and "this.style.display='none'" in out),
    ("cbody结构", "class='cbody'" in out),
    ("更新图片渲染", "class='um' src='media/2106414871533568417_1.jpg'" in out),
    ("视频poster+mp4", "<video class='uv' controls" in out and "poster='media/p.jpg'" in out
     and "https://video.twimg.com/x.mp4?tag=29" in out),
    ("媒体包裹umed", "class='umed'" in out),
    ("upd条目数=3", out.count("data-uid=") == 3),
    ("已读JS在", "hy_read_v1" in out and "mark-all" in out),
    ("未读chip", "unread-chip" in out),
    ("无需关注声明", "无需关注" in out),
    # v3: 排序 / 星星 / 取消关注 / 分类
    ("cards容器", "id='cards'" in out),
    ("排序按钮组", "data-sort='default'" in out and "data-sort='stars'" in out
     and "data-sort='freq'" in out),
    ("卡片排序属性", "data-idx=" in out and "data-stars=" in out and "data-freq=" in out),
    ("星星控件", "class='stars'" in out and out.count("data-n='") >= 3 * 5),
    ("取消关注按钮", "class='unbtn'" in out),
    ("分类徽章类", "catb" in out),
    ("服务提示位", "svc-hint" in out),
    ("API JS在", "/api/stars" in out and "/api/unfollow" in out and "/api/ping" in out),
    ("服务未启动提示", "本地服务未启动" in out),
    ("近30天频率", "freq30" not in out and "近30天" in out),
    # v4: 分类筛选
    ("筛选栏", "class='filterbar'" in out and "data-cat=''" in out),
    ("筛选按钮带计数", out.count("class='fbtn'") >= 3 and "class='cnt'" in out),
    ("卡片data-cat", out.count("data-cat='") >= 20),
    ("过滤JS", "applyCat" in out and "hy_cat_v1" in out and "cat-hidden" in out),
    ("未读计数只算可见", "hidden" in out and "!hidden) n++" in out),
    ("分类枚举含3D", "3D" in out),
]
ok = 0
for name, passed in checks:
    print(("PASS " if passed else "FAIL ") + name)
    if passed:
        ok += 1

# HTML 粗校验: div 配平
opens = out.count("<div")
closes = out.count("</div>")
print("div open=%d close=%d %s" % (opens, closes, "PASS 配平" if opens == closes else "FAIL 不配平"))
if opens == closes:
    ok += 1
total = len(checks) + 1
print("%d/%d" % (ok, total))
sys.exit(0 if ok == total else 1)
