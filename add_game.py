# -*- coding: utf-8 -*-
"""huangyou-watcher 添加游戏
用法:
  python add_game.py "<链接1>" "<链接2>" ...     (批量, 一次可传多个)
  python add_game.py "<含链接的混合文本>"          (自动拆出其中所有链接)
每个链接独立入库。支持 X 帖子(解析作者+文中链接+封面)、Steam 链接、DLsite 链接。
幂等: 同 appid / 同 DLsite 号 / 同 handle 已存在则不重复添加。
封面: Steam header > 帖图 > 视频缩略图 > 作者头像(存 covers/ 本地)。
"""
import csv
import io
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
            url = url + "\n" + body
            for fc in facets:
                rep = fc.get("replacement") or ""
                if rep.startswith("http"):
                    url += "\n" + rep
            print("  解析 X 帖 @%s | %s" % (handle, body.split("\n")[0][:70]))
        except Exception as ex:
            print("  X 帖解析失败(%s), 用链接其余信息" % ex)
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
                url = url + "\n" + bio
                print("  解析 X 主页 @%s | bio: %s" % (handle, bio.replace("\n", " ")[:60]))
            except Exception as ex:
                print("  主页资料获取失败(%s), 仅用 handle" % ex)
                notes = "来源主页 https://x.com/%s" % handle

    for am in re.finditer(r"store\.steampowered\.com/app/(\d+)", url):
        if am.group(1) not in steam_apps:
            steam_apps.append(am.group(1))
    for dm in re.finditer(r"dlsite\.com/[a-z]+/work/=/product_id/([A-Za-z]{2}\d+)", url):
        if dm.group(1) not in dlsite_ids:
            dlsite_ids.append(dm.group(1))
    if not dlsite_ids:
        for dm in re.finditer(r"(?<![\w/])((?:RJ|RG|VJ)\d{6,})(?![\d])", url):
            if dm.group(1) not in dlsite_ids:
                dlsite_ids.append(dm.group(1))

    if not (handle or steam_apps or dlsite_ids):
        print("  跳过(没识别出 X/Steam/DLsite): %s" % url.split("\n")[0][:80])
        return False

    # 查重: handle 单独作为强信号(黄油作者一号一游戏)
    for r in rows:
        r_apps = {a.strip() for a in (r.get("steam_appid") or "").split(",") if a.strip()}
        if steam_apps and r_apps.intersection(steam_apps):
            print("  已存在(Steam appid): %s" % r.get("name"))
            return False
        if dlsite_ids and (r.get("dlsite_id") or "").strip() in dlsite_ids:
            print("  已存在(DLsite): %s" % r.get("name"))
            return False
        if handle and (r.get("x_handle") or "").strip().lower() == handle.lower():
            print("  已存在(handle @%s): %s" % (handle, r.get("name")))
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
    print("  已添加: %s" % name)
    return True


def main():
    if len(sys.argv) < 2 or not "".join(sys.argv[1:]).strip():
        print('用法: python add_game.py "<链接1>" "<链接2>" ...')
        return 2
    links = extract_links(" ".join(sys.argv[1:]))
    if not links:
        print("没在输入里找到 X/Steam/DLsite 链接。")
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
