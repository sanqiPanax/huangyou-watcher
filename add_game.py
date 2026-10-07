# -*- coding: utf-8 -*-
"""huangyou-watcher 添加游戏
用法:
  python add_game.py "<链接1>" "<链接2>" ...     (批量, 一次可传多个)
  python add_game.py "<含链接的混合文本>"          (自动拆出其中所有链接)
每个链接独立入库。支持 X 帖子(解析作者+文中链接+封面)、Steam 链接、DLsite 链接。

REQUIREMENTS P0:
  FR-01 输入类型识别; 解析失败保留原链接与错误原因(graph.json)
  FR-02 从 X 内容提取官方外链/作品名/作者名, 标注证据; 转发/回复链接不当作品
  FR-03 低置信度候选进待确认列表, 由用户确认/拒绝
  FR-04 handle 只代表作者: 同一作者可有多部作品(有平台页时不再因 handle 查重拦掉)
幂等: 同 appid / 同 DLsite 号已存在则不重复添加; 拒绝过的候选不复活。
封面: Steam header > 帖图 > 视频缩略图 > 作者头像(存 covers/ 本地)。
"""
import csv
import io
import json
import os
import re
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE, "watchlist.csv")
EVENTS_PATH = os.path.join(BASE, "events.csv")
FIELDS = ["name", "x_handle", "steam_appid", "dlsite_id", "expected_release",
          "last_update", "update_count", "x_last_id", "steam_last_release",
          "dlsite_last_release", "cover", "stars", "category", "notes"]

sys.path.insert(0, BASE)
import media as M
import model as MOD

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def steam_info(appid):
    import json
    url = "https://store.steampowered.com/api/appdetails?appids=%s&l=english" % appid
    try:
        d = json.loads(M.http_bytes(url, timeout=20, tries=2))
        node = d.get(appid, {})
        if node.get("success"):
            data = node["data"]
            return data.get("name") or "", data.get("header_image") or ""
    except Exception:
        pass
    return "", ""


def load_rows():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def save_rows(rows):
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def extract_links(text):
    """从任意文本拆出所有 X(帖子或裸主页)/Steam/DLsite 链接(去重保序)。
    裸主页链接用负向前瞻, 不会把 /status/ URL 拆成两半。"""
    pat = (r"https?://(?:www\.)?(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}/status/\d+"
           r"|https?://(?:www\.)?(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}(?![\w/-])"
           r"|https?://store\.steampowered\.com/app/\d+"
           r"|https?://www\.dlsite\.com/[a-z]+/work/=/product_id/[A-Za-z]{2}\d+")
    seen, out = set(), []
    for u in re.findall(pat, text):
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _graph_add_candidate(g, url, platform, title, evidence, confidence, reason, row, handle):
    """FR-03 低置信度候选入 graph.json 待确认列表。"""
    try:
        cand, changed = MOD.add_candidate(g, url, platform, title=title, evidence=evidence,
                                          confidence=confidence, reason=reason,
                                          row=row, handle=handle)
        return changed
    except Exception as ex:
        print("  [warn] 候选记录失败: %r" % ex)
        return False


def add_one(url, rows):
    """处理单个链接, 成功追加到 rows 返回 True; 重复/失败返回 False。"""
    handle = ""
    name = ""
    notes = ""
    steam_apps = []
    dlsite_ids = []
    post_json = None
    user_info = None
    header_image = ""

    # FR-01: 先识别输入类型, 解析失败时把原链接与原因留在 graph.json
    cin = MOD.classify_input(url.split("\n", 1)[0])
    type_label = "/".join(MOD.TYPE_LABEL.get(t, t) for t in cin["types"]) or "未识别"
    print("  输入类型: %s" % type_label)

    g = MOD.load_graph()
    graph_dirty = False
    rt_block = set()   # 转发/回复里的平台 id: 只进候选, 不并入本行(FR-02)

    m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d+)", url)
    if m:
        try:
            post_json = M.fxtwitter_post(m.group(1), m.group(2))
            tweet = post_json.get("tweet") or {}
            # 2026-10 结构: author 在 tweet.author; 老结构在顶层 author
            author = tweet.get("author") or post_json.get("author") or {}
            handle = author.get("screen_name") or m.group(1)
            raw = tweet.get("raw_text")
            body = ""
            facets = []
            if isinstance(raw, dict):
                body = raw.get("text") or ""
                facets = raw.get("facets") or []
            elif isinstance(raw, str):
                body = raw
            if not body:
                tx = tweet.get("text")
                body = tx if isinstance(tx, str) else (tx or {}).get("text") or ""
            if not facets and isinstance(tweet.get("facets"), list):
                facets = tweet["facets"]
            notes = "来源帖 https://x.com/%s/status/%s" % (m.group(1), m.group(2))
            # FR-02: 结构化提取证据(外链/作品名/作者名), 转发与回复链接单独标记
            ev = MOD.extract_x_evidence(post_json=post_json, body=body, src="post")
            for fc in facets:
                rep = fc.get("replacement") or ""
                if rep.startswith("http"):
                    ev["links"].append({"url": rep, "platform": MOD.platform_of(rep),
                                        "from": "post", "retweet": False})
            is_rt = bool(tweet.get("reposted_by") or tweet.get("retweeted_by")
                         or tweet.get("replying_to"))
            if is_rt:
                print("  注意: 这是转发/回复, 其中的链接不当作作者作品(FR-02)")
            # 官方外链里的平台页 -> 候选证据
            for lk in ev["links"]:
                if lk["platform"] in ("steam", "dlsite"):
                    steam_apps2 = re.findall(r"store\.steampowered\.com/app/(\d+)", lk["url"])
                    dl2 = re.findall(r"dlsite\.com/[a-z]+/work/=/product_id/([A-Za-z]{2}\d+)", lk["url"])
                    if lk.get("retweet"):
                        # 转发/回复链接: 只留候选证据, 记入屏蔽集
                        rt_block.update(steam_apps2)
                        rt_block.update(dl2)
                    conf = 0.35 if lk.get("retweet") else 0.9
                    _graph_add_candidate(
                        g, lk["url"], lk["platform"], title=",".join(ev["names"][:2]),
                        evidence=[{"from": "X帖 %s" % notes, "retweet": lk.get("retweet", False)}],
                        confidence=conf,
                        reason="转发帖链接, 低置信" if lk.get("retweet") else "作者帖内外链",
                        row="", handle=handle)
                    graph_dirty = True
                    # 非转发的平台外链直接并入本行 id 列表(同一作品多页, FR-04)
                    if not lk.get("retweet"):
                        steam_apps += [a for a in steam_apps2 if a not in steam_apps]
                        dlsite_ids += [d for d in dl2 if d not in dlsite_ids]
            url = url + "\n" + body
            for fc in facets:
                rep = fc.get("replacement") or ""
                if rep.startswith("http"):
                    url += "\n" + rep
            print("  解析 X 帖 @%s | %s" % (handle, body.split("\n")[0][:70]))
        except Exception as ex:
            print("  X 帖解析失败(%s), 用链接其余信息" % ex)
            MOD.record_input_error(g, url.split("\n", 1)[0], "X帖解析失败: %r" % ex)
            graph_dirty = True
            handle = m.group(1)
    else:
        # 裸主页链接(无 /status/): 拉用户资料拿名字/头像/简介
        mb = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?![\w/-])", url)
        if mb:
            handle = mb.group(1)
            try:
                user_info = M.fxtwitter_user(handle)
                notes = "来源主页 https://x.com/%s" % handle
                bio = user_info.get("description") or ""
                # FR-02: 简介里的外链是官方链接来源
                ev = MOD.extract_x_evidence(user_info=user_info, src="bio")
                for lk in ev["links"]:
                    if lk["platform"] in ("steam", "dlsite"):
                        _graph_add_candidate(
                            g, lk["url"], lk["platform"], title=",".join(ev["names"][:2]),
                            evidence=[{"from": "X简介", "retweet": False}],
                            confidence=0.85, reason="作者简介里的官方外链",
                            row="", handle=handle)
                        graph_dirty = True
                        for a in re.findall(r"store\.steampowered\.com/app/(\d+)", lk["url"]):
                            if a not in steam_apps:
                                steam_apps.append(a)
                        for d in re.findall(r"dlsite\.com/[a-z]+/work/=/product_id/([A-Za-z]{2}\d+)", lk["url"]):
                            if d not in dlsite_ids:
                                dlsite_ids.append(d)
                url = url + "\n" + bio
                print("  解析 X 主页 @%s | bio: %s" % (handle, bio.replace("\n", " ")[:60]))
            except Exception as ex:
                print("  主页资料获取失败(%s), 仅用 handle" % ex)
                MOD.record_input_error(g, url.split("\n", 1)[0], "主页资料获取失败: %r" % ex)
                graph_dirty = True
                notes = "来源主页 https://x.com/%s" % handle

    for am in re.finditer(r"store\.steampowered\.com/app/(\d+)", url):
        if am.group(1) not in steam_apps and am.group(1) not in rt_block:
            steam_apps.append(am.group(1))
    for dm in re.finditer(r"dlsite\.com/[a-z]+/work/=/product_id/([A-Za-z]{2}\d+)", url):
        if dm.group(1) not in dlsite_ids and dm.group(1) not in rt_block:
            dlsite_ids.append(dm.group(1))
    if not dlsite_ids:
        for dm in re.finditer(r"(?<![\w/])((?:RJ|RG|VJ)\d{6,})(?![\d])", url):
            if dm.group(1) not in dlsite_ids and dm.group(1) not in rt_block:
                dlsite_ids.append(dm.group(1))

    if not (handle or steam_apps or dlsite_ids):
        # FR-01: 未识别的输入保留原链接与原因, 不再只 print 后丢弃
        first_line = url.split("\n")[0][:120]
        MOD.record_input_error(g, first_line, "没识别出 X/Steam/DLsite 链接(类型: %s)" % type_label)
        try:
            MOD.save_graph(g)
        except Exception:
            pass
        print("  跳过(没识别出 X/Steam/DLsite): %s" % first_line)
        return False

    # ---- FR-04 查重: 平台 id 强查重; handle 只代表作者, 不再拦同作者新作品 ----
    dup_reason = ""
    for r in rows:
        r_apps = {a.strip() for a in (r.get("steam_appid") or "").split(",") if a.strip()}
        if steam_apps and r_apps.intersection(steam_apps):
            dup_reason = "已存在(Steam appid): %s" % r.get("name")
            break
        if dlsite_ids and (r.get("dlsite_id") or "").strip() in dlsite_ids:
            dup_reason = "已存在(DLsite): %s" % r.get("name")
            break
    if not dup_reason and handle:
        same_h = [r for r in rows
                  if (r.get("x_handle") or "").lower() == handle.lower()]
        # 纯作者主页(无平台页): 已关注过该作者才算重复;
        # 带平台页的输入 = 同作者的新作品, 允许入库(FR-04)
        if same_h and not (steam_apps or dlsite_ids):
            dup_reason = "已关注该作者 @%s: %s" % (handle, same_h[0].get("name"))
    if dup_reason:
        print("  " + dup_reason)
        if graph_dirty:
            try:
                MOD.save_graph(g)
            except Exception:
                pass
        return False


    # 名称: Steam 优先 > 正文首行 > 主页显示名 > handle
    if steam_apps:
        name, header_image = steam_info(steam_apps[0])
        if name:
            print("  Steam 名称: %s" % name)
    if not name and post_json:
        tweet = post_json.get("tweet") or {}
        tx = tweet.get("text")
        first = tx if isinstance(tx, str) else (tx or {}).get("text") or ""
        first = first.split("\n")[0].strip()
        if first:
            name = first[:40]
    if not name and user_info:
        disp = (user_info.get("name") or "").strip()
        if disp:
            name = disp[:40]
    if not name:
        name = handle or ",".join(steam_apps or dlsite_ids)

    # 封面: Steam header > 帖图 > 视频缩略图 > 头像(裸主页直接存头像)
    cover = ""
    if header_image:
        cover = M._save(header_image, os.path.join(M.COVERS_DIR, "%s.jpg" % (steam_apps[0]))) or header_image
    if not cover and user_info and user_info.get("avatar_url"):
        av = user_info["avatar_url"]
        cover = M._save(av, os.path.join(M.COVERS_DIR, "%s_avatar%s" % (handle, M._safe_ext(av)))) or av
    if not cover:
        cover = M.pick_cover(steam_apps, post_json, handle)
    print("  封面: %s" % (cover or "(无)"))

    # AI 分类(只在添加时分这一次, 拉取不再分类)
    category = ""
    try:
        import classify as CLS
        category = CLS.classify(handle, url.split("\n", 1)[-1][:600], name)
        print("  分类: %s" % (category or "(AI未返回)"))
    except Exception as ex:
        print("  分类失败(%r)" % ex)

    row = {k: "" for k in FIELDS}
    row["name"] = name
    row["x_handle"] = handle
    row["steam_appid"] = ",".join(steam_apps)
    row["dlsite_id"] = dlsite_ids[0] if dlsite_ids else ""
    row["update_count"] = "0"
    row["cover"] = cover
    row["stars"] = "1"  # 默认 1 星, 报告里点星星即改
    row["category"] = category
    row["notes"] = notes
    rows.append(row)

    exists = os.path.exists(EVENTS_PATH) and os.path.getsize(EVENTS_PATH) > 0
    with open(EVENTS_PATH, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["time", "game", "kind", "detail"])
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), name, "Add",
                    "入库 x=@%s steam=%s dlsite=%s" % (row["x_handle"], row["steam_appid"], row["dlsite_id"])])
    # FR-05: 记录来源事实(入库链接即证据); FR-03: 本链接对应的候选标为已确认
    try:
        if steam_apps:
            MOD.record_fact(g, name, "page", "steam:%s" % ",".join(steam_apps), "add",
                            url="https://store.steampowered.com/app/%s" % steam_apps[0])
        if dlsite_ids:
            MOD.record_fact(g, name, "page", "dlsite:%s" % dlsite_ids[0], "add",
                            url="https://www.dlsite.com/maniax/work/=/product_id/%s.html" % dlsite_ids[0])
        if handle:
            MOD.record_fact(g, name, "creator", "@%s" % handle, "add",
                            url="https://x.com/%s" % handle)
        first_url = url.split("\n", 1)[0]
        for c in g["candidates"]:
            if c.get("url") and c["url"] in first_url and c.get("status") == "pending":
                c["status"] = "confirmed"
                c["target_row"] = name
                c["resolved"] = MOD.now_str()
        MOD.save_graph(g)
    except Exception as ex:
        print("  [warn] graph 落盘失败: %r" % ex)
    print("  已添加: %s" % name)
    return True


def main():
    if len(sys.argv) < 2 or not "".join(sys.argv[1:]).strip():
        print('用法: python add_game.py "<链接1>" "<链接2>" ...')
        return 2
    raw_input_text = " ".join(sys.argv[1:])
    links = extract_links(raw_input_text)
    if not links:
        # FR-01: 解析失败保留原链接与错误原因
        try:
            g = MOD.load_graph()
            cin = MOD.classify_input(raw_input_text)
            reason = ("没找到 X/Steam/DLsite 链接; 识别到的其它链接 %d 个"
                      % len(cin["unknown"])) if cin["unknown"] else "没找到 X/Steam/DLsite 链接"
            MOD.record_input_error(g, raw_input_text[:300], reason)
            MOD.save_graph(g)
        except Exception:
            pass
        print("没在输入里找到 X/Steam/DLsite 链接(已记录到 graph.json 的待处理输入)。")
        return 2
    print("识别到 %d 个链接" % len(links))
    rows = load_rows()
    added = 0
    for i, u in enumerate(links, 1):
        print("[%d/%d] %s" % (i, len(links), u))
        try:
            if add_one(u, rows):
                added += 1
                save_rows(rows)  # 每条即时落盘: 批量中途被杀不丢已入库的
        except Exception as ex:
            print("  异常: %r" % ex)
    save_rows(rows)
    print()
    print("完成: 新增 %d/%d, 表内共 %d 行 -> %s" % (added, len(links), len(rows), CSV_PATH))
    if added:
        print("下一步: 双击 拉取.cmd 建立基线(首次只记录不报更新)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
