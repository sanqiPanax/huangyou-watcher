# -*- coding: utf-8 -*-
"""新功能自测: REQUIREMENTS P0+P1 (FR-01..FR-12) 的 model 层与报告渲染。
离线运行, 不碰网络; graph 数据用临时目录隔离, 不污染真实 graph.json。"""
import io
import json
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import model as MOD
import pull

checks = []


def ck(name, cond):
    checks.append((name, bool(cond)))


# ---- 隔离存储(必须在工作区内, 沙箱不许写系统 Temp) ----
tmp = os.path.join(BASE, "_selftest_out")
os.makedirs(tmp, exist_ok=True)
orig_graph = MOD.GRAPH_PATH
MOD.GRAPH_PATH = os.path.join(tmp, "graph.json")
if os.path.exists(MOD.GRAPH_PATH):
    os.remove(MOD.GRAPH_PATH)

try:
    # ===== FR-01 输入类型识别 + 失败留存 =====
    c1 = MOD.classify_input("https://x.com/foo/status/123")
    ck("FR-01 识别X帖子", c1["types"] == ["x_post"])
    c2 = MOD.classify_input("https://store.steampowered.com/app/5141500")
    ck("FR-01 识别Steam", c2["types"] == ["steam"])
    c3 = MOD.classify_input("https://www.dlsite.com/maniax/work/=/product_id/RJ01234567.html")
    ck("FR-01 识别DLsite", c3["types"] == ["dlsite"])
    c4 = MOD.classify_input("https://x.com/bar")
    ck("FR-01 识别裸主页", c4["types"] == ["x_profile"])
    c5 = MOD.classify_input("https://example.com/whatever")
    ck("FR-01 未知链接归unknown", c5["unknown"] and not c5["types"])

    g = MOD.load_graph()
    ck("FR-01 空骨架", g["candidates"] == [] and g["facts"] == {})
    MOD.record_input_error(g, "https://example.com/x", "没识别出 X/Steam/DLsite 链接")
    MOD.record_input_error(g, "https://example.com/x", "第二次原因")
    ck("FR-01 失败留存原链接", len(g["input_errors"]) == 1)
    ck("FR-01 失败原因滚动更新", "第二次原因" in g["input_errors"][0]["reason"])
    MOD.record_input_error(g, "别的输入", "A")
    ck("FR-01 多条失败都留", len(g["input_errors"]) == 2)

    # ===== FR-02 证据提取 + 转发不当作品 =====
    post = {"tweet": {"text": "My game! https://store.steampowered.com/app/123",
                      "author": {"screen_name": "dev1", "name": "Dev One"},
            "reposted_by": None, "replying_to": None}}
    ev = MOD.extract_x_evidence(post_json=post, src="post")
    ck("FR-02 提取外链", any(l["platform"] == "steam" for l in ev["links"]))
    ck("FR-02 提取作者名", "Dev One" in ev["names"])
    ck("FR-02 提取handle", "dev1" in ev["handles"])
    ck("FR-02 原帖不算转发", not any(l.get("retweet") for l in ev["links"]))

    rt = {"tweet": {"text": "cool https://store.steampowered.com/app/999",
                    "reposted_by": {"screen_name": "someone"}}}
    ev2 = MOD.extract_x_evidence(post_json=rt, src="post")
    ck("FR-02 转发帖链接标retweet",
       any(l.get("retweet") for l in ev2["links"] if l["platform"] == "steam"))
    ck("FR-02 平台判定", MOD.platform_of("https://x.com/a") == "x_profile")

    # ===== FR-03 候选: 低置信进待确认, 可拒绝/确认, 拒绝不复活 =====
    cand, changed = MOD.add_candidate(g, "https://store.steampowered.com/app/999",
                                      "steam", title="TestGame",
                                      evidence=[{"from": "X帖", "retweet": True}],
                                      confidence=0.35, reason="转发帖链接, 低置信")
    ck("FR-03 候选创建", changed and cand["status"] == "pending")
    ck("FR-03 低置信进入pending", len(MOD.pending_candidates(g)) == 1)
    # 同链接重复添加不产生第二个
    c_again, changed2 = MOD.add_candidate(g, "https://store.steampowered.com/app/999",
                                          "steam", confidence=0.35, reason="r2")
    ck("FR-03 候选幂等", len(g["candidates"]) == 1)
    # 拒绝
    MOD.resolve_candidate(g, cand["id"], "reject")
    ck("FR-03 拒绝后不在pending", not MOD.pending_candidates(g))
    c_re, ch3 = MOD.add_candidate(g, "https://store.steampowered.com/app/999", "steam")
    ck("FR-03 拒绝不复活", c_re["status"] == "rejected" and not ch3)
    # 确认
    cand2, _ = MOD.add_candidate(g, "https://store.steampowered.com/app/555", "steam")
    MOD.resolve_candidate(g, cand2["id"], "confirm", target="MyGame")
    ck("FR-03 确认带目标作品", cand2["status"] == "confirmed"
       and cand2["target_row"] == "MyGame")

    # ===== FR-05 事实: 来源+时间+历史, 不同来源不互相顶替 =====
    MOD.record_fact(g, "W1", "release_date", "2026-12-01", "Steam",
                    url="https://store.steampowered.com/app/1", status="confirmed")
    ch = MOD.record_fact(g, "W1", "release_date", "2026-12-01", "Steam",
                         url="https://store.steampowered.com/app/1", status="confirmed")
    ck("FR-05 同值不膨胀", ch is False and len(MOD.fact_history(g, "W1", "release_date")) == 1)
    MOD.record_fact(g, "W1", "release_date", "2027-01-15", "Steam", status="delayed")
    hist = MOD.fact_history(g, "W1", "release_date")
    ck("FR-05 变更保留历史", len(hist) == 2 and hist[0]["v"] == "2026-12-01")
    lf = MOD.latest_fact(g, "W1", "release_date", src="Steam")
    ck("FR-05 最新事实带来源与时间", lf["src"] == "Steam" and lf["at"])
    # 第二来源不顶替
    MOD.record_fact(g, "W1", "release_date", "2026-12-08", "DLsite",
                    url="https://www.dlsite.com/x")
    ck("FR-05 双来源并存",
       MOD.latest_fact(g, "W1", "release_date", src="Steam")["v"] == "2027-01-15"
       and MOD.latest_fact(g, "W1", "release_date", src="DLsite")["v"] == "2026-12-08")
    MOD.record_fact(g, "W1", "steam_feed", "抓取失败", "Steam", status="failed",
                    note="timeout")
    ck("FR-05 失败状态可查", MOD.latest_fact(g, "W1", "steam_feed")["status"] == "failed")

    # ===== FR-06 发售状态分类 =====
    ck("FR-06 无日期=未知", MOD.classify_release("", False)[0] == "unknown")
    ck("FR-06 即将推出=商店待定", MOD.classify_release("", True)[0] == "store_tbc")
    ck("FR-06 未来日期=已确认",
       MOD.classify_release("2099-01-01", False)[0] == "confirmed")
    ck("FR-06 过去日期=已发售",
       MOD.classify_release("2020-01-01", False)[0] == "released")
    ck("FR-06 日期后移=已延期",
       MOD.classify_release("2099-06-01", False, prev_date="2099-03-01")[0] == "delayed")
    ck("FR-06 作者口头=作者预告",
       MOD.classify_release("", False, author_claimed=True)[0] == "author_post")
    ck("FR-06 状态标签中文",
       MOD.RELEASE_LABEL["delayed"] == "已延期")

    # ===== FR-09/10 事件分类与跨来源去重 =====
    ck("FR-09 Demo识别", MOD.classify_event_text("Demo is out now!") == "demo")
    ck("FR-09 体験版识别", MOD.classify_event_text("体験版を公開しました") == "demo")
    ck("FR-09 延期识别", MOD.classify_event_text("很抱歉延期到下个月") == "delay")
    ck("FR-09 汉化识别", MOD.classify_event_text("简体中文汉化完成") == "lang")
    ck("FR-09 价格识别", MOD.classify_event_text("首发 ¥1980 优惠中") == "price")
    ck("FR-09 发售识别", MOD.classify_event_text("明日発売します") == "release")
    ck("FR-09 普通帖归x_post",
       MOD.classify_event_text("今天做了个可爱的表情") == "x_post")
    ck("FR-09 Steam来源默认listing", MOD.classify_event_text("whatever", "Steam") == "listing")

    ev1, new1 = MOD.upsert_event(g, "W1", "demo", "Steam 出现 Demo",
                                 {"src": "Steam", "url": "u1", "text": "demos=1"},
                                 norm="demo")
    ck("FR-10 首个事件is_new", new1 is True)
    ev2b, new2 = MOD.upsert_event(g, "W1", "demo", "X 帖也说 Demo",
                                  {"src": "X", "url": "u2", "text": "demo!"},
                                  norm="demo")
    ck("FR-10 跨来源合并同事件", new2 is False and ev2b is ev1)
    ck("FR-10 证据合并进sources",
       len(ev1["sources"]) == 2
       and {s["src"] for s in ev1["sources"]} == {"Steam", "X"})
    ck("FR-10 计数增加", ev1["count"] == 2)
    # 不同 norm 不合并
    ev3, new3 = MOD.upsert_event(g, "W1", "release", "发售 2099-01-01",
                                 {"src": "Steam", "url": "u3"}, norm="2099-01-01")
    ck("FR-10 不同事件独立", new3 is True and len(MOD.row_events(g, "W1")) == 2)

    # ===== FR-11 提醒偏好 =====
    prefs = MOD.alert_prefs(g, "W1", "dev1")
    ck("FR-11 事件默认开关: demo开", prefs["demo"] is True)
    ck("FR-11 普通帖默认关(低噪声)", prefs["x_post"] is False)
    ck("FR-11 来源默认开", prefs["src_x"] is True)
    ck("FR-11 默认不静音", prefs["mute"] is True)
    ck("FR-11 demo事件应提醒",
       MOD.should_notify(g, "W1", "demo", "x", "dev1") is True)
    ck("FR-11 普通帖不提醒",
       MOD.should_notify(g, "W1", "x_post", "x", "dev1") is False)
    MOD.set_alert(g, "W1", "mute", False, scope="row")
    ck("FR-11 单作品静音生效",
       MOD.should_notify(g, "W1", "demo", "x", "dev1") is False)
    ck("FR-11 静音不影响其它作品",
       MOD.should_notify(g, "W2", "demo", "x", "dev1") is True)
    MOD.set_alert(g, "W1", "mute", True, scope="row")
    MOD.set_alert(g, "", "src_steam", False, scope="global")
    ck("FR-11 关来源开关生效",
       MOD.should_notify(g, "W1", "demo", "steam", "dev1") is False)
    ck("FR-11 关来源不影响X", MOD.should_notify(g, "W1", "demo", "x", "dev1") is True)
    MOD.set_alert(g, "", "src_steam", True, scope="global")
    MOD.set_alert(g, "", "demo", False, scope="global")
    ck("FR-11 关事件类型生效",
       MOD.should_notify(g, "W1", "demo", "x", "dev1") is False)
    MOD.set_alert(g, "", "demo", True, scope="global")
    ck("FR-11 未知key拒绝", MOD.set_alert(g, "W1", "nope", True)[0] is False)

    # ===== FR-04 同作者多作品 =====
    rows = [{"name": "GameA", "x_handle": "dev1"},
            {"name": "GameB", "x_handle": "dev1"},
            {"name": "Other", "x_handle": "dev2"}]
    sibs = MOD.works_by_creator(rows, "dev1")
    ck("FR-04 同作者多作品", len(sibs) == 2)
    ck("FR-04 不同作者隔离", len(MOD.works_by_creator(rows, "dev2")) == 1)
    ck("FR-04 大小写不敏感",
       len(MOD.works_by_creator(rows, "@DEV1")) == 2)

    # ===== FR-12 Demo 状态 =====
    ck("FR-12 Steam demos字段=独立Demo页",
       MOD.demo_state_from_steam([{"appid": 123}]) == "page")
    ck("FR-12 Steam无demos", MOD.demo_state_from_steam(None) == "none")
    ck("FR-12 DLsite有体験版", MOD.demo_state_from_dlsite(True) == "trial")
    ck("FR-12 DLsite无体験版", MOD.demo_state_from_dlsite(False) == "none")
    ck("FR-12 Demo标签中文", MOD.DEMO_LABEL["trial"] == "体験版")

    # ===== 持久化 =====
    MOD.save_graph(g)
    g2 = MOD.load_graph()
    ck("持久化 facts", "W1" in g2["facts"])
    ck("持久化 events", len(g2["events"]) >= 2)
    ck("持久化 candidates", len(g2["candidates"]) == 2)  # 999(拒绝) + 555(确认)
    ck("持久化 alerts", "rows" in g2["alerts"])

    # 渲染前置: W1 作为作品行 + 一个仍 pending 的候选 + 发售状态事实
    rows = rows + [{"name": "W1", "x_handle": "dev1"}]
    MOD.add_candidate(g, "https://store.steampowered.com/app/777", "steam",
                      title="PendingGame", evidence=[{"from": "X简介", "retweet": False}],
                      confidence=0.5, reason="简介里的外链", handle="dev1")
    MOD.record_fact(g, "W1", "release_status", "delayed", "Steam",
                    url="https://store.steampowered.com/app/1",
                    status="delayed", note="发售日 2099-03-01 -> 2099-06-01")

    # ===== 报告渲染: 新面板/证据区 =====
    real_report = pull.REPORT_PATH
    pull.REPORT_PATH = os.path.join(tmp, "report.html")
    upd = {"W1": [{"t": "2026-10-07 10:00:00", "src": "X",
                   "text": "demo! https://x.com/a/status/1"}]}
    pull.build_report(rows, {}, [], {"time": "2026-10-07 10:00:00", "elapsed": "1s"},
                      upd, 5, g)
    out = open(pull.REPORT_PATH, encoding="utf-8").read()
    pull.REPORT_PATH = real_report

    ck("渲染 待确认面板", "p-cand" in out and "cand-ok" in out and "cand-no" in out)
    ck("渲染 候选置信度", "置信" in out)
    ck("渲染 提醒设置面板", "p-alert" in out and "al-chk" in out and "al-save" in out)
    ck("渲染 事件时间线", "事件时间线" in out and "evrow" in out)
    ck("渲染 事件多证据", "证据×2" in out)
    ck("渲染 证据详情", "evdetail" in out)
    ck("渲染 发售状态中文", "已延期" in out)
    ck("渲染 来源抓取时间", "抓取" in out)
    ck("渲染 静音按钮", "mutebtn" in out)
    ck("渲染 同作者其它作品", "其它作品" in out)
    ck("渲染 未识别输入", "inputerr" in out and "第二次原因" in out)
    ck("渲染 类型标签", "ptag" in out)
    ck("渲染 候选计数", "cand-cnt" in out)
    ck("渲染 JS: 候选提交", "/api/candidate" in out)
    ck("渲染 JS: 提醒保存", "/api/alerts" in out)
    ck("渲染 JS: 静音接口", "/api/mute" in out)
    ck("渲染 JS: 证据展开", "evmore" in out and "evdetail" in out)
    opens = open(pull.REPORT_PATH, encoding="utf-8").read().count("<div")
    closes = open(pull.REPORT_PATH, encoding="utf-8").read().count("</div>")
    ck("报告 div 配平 (%d/%d)" % (opens, closes), opens == closes)
    pull.REPORT_PATH = real_report
finally:
    MOD.GRAPH_PATH = orig_graph
    # 自测产物留在 _selftest_out/ 便于排查, 只清旧报告
    try:
        if os.path.exists(os.path.join(tmp, "report.html")):
            os.remove(os.path.join(tmp, "report.html"))
    except OSError:
        pass

ok = 0
for name, passed in checks:
    print(("PASS " if passed else "FAIL ") + name)
    if passed:
        ok += 1
print("%d/%d" % (ok, len(checks)))
sys.exit(0 if ok == len(checks) else 1)
