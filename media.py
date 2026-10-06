# -*- coding: utf-8 -*-
"""huangyou-watcher 媒体模块: 封面抓取 + 帖子图片/视频抓取
约定:
- 图片一律下载到本地(covers/ 与 media/), 报告离线可看;
- 视频不下载(体积大), 存远程 mp4 URL + 本地缩略图(poster);
- 下载失败回退为远程 URL(cover/media 字段里直接存 URL)。
"""
import json
import os
import re
import ssl
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
COVERS_DIR = os.path.join(BASE, "covers")
MEDIA_DIR = os.path.join(BASE, "media")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"


def http_bytes(url, timeout=25, tries=2, proxy_once=True):
    """GET bytes。直连失败自动试一次 Clash 代理(pbs/X 图床国内直连偶发不通)。"""
    last = None
    modes = [False, True] if proxy_once else [False]
    for use_proxy in modes:
        for i in range(tries):
            handlers = []
            if use_proxy:
                handlers.append(urllib.request.ProxyHandler(
                    {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}))
            else:
                handlers.append(urllib.request.ProxyHandler({}))
            handlers.append(urllib.request.HTTPSHandler(context=CTX))
            op = urllib.request.build_opener(*handlers)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with op.open(req, timeout=timeout) as r:
                    return r.read()
            except Exception as ex:
                last = ex
                time.sleep(0.8 * (i + 1))
    raise RuntimeError("download fail %s: %r" % (url[:120], last))


def fxtwitter_user(handle, tries=3):
    """裸主页链接 -> 用户资料 JSON(头像/简介/名字)。api.fxtwitter.com/<handle>"""
    url = "https://api.fxtwitter.com/%s" % handle
    last = None
    for i in range(tries):
        try:
            d = json.loads(http_bytes(url, timeout=25, tries=1))
            user = d.get("user") or {}
            if user.get("screen_name"):
                return user
        except Exception as ex:
            last = ex
        time.sleep(1.0 * (i + 1))
    if last:
        raise RuntimeError("fxtwitter user fail: %r" % last)
    return d.get("user") or {}


def fxtwitter_post(user, status_id, tries=3):
    """取单帖 JSON, media/author 在 tweet.* 下(2026-10 结构)。
    media 偶发返回空数组, 重试到拿满或耗尽。"""
    url = "https://api.fxtwitter.com/%s/status/%s" % (user, status_id)
    last = None
    for i in range(tries):
        try:
            d = json.loads(http_bytes(url, timeout=25, tries=1))
            tweet = d.get("tweet") or {}
            if tweet.get("media") or i == tries - 1:
                return d
        except Exception as ex:
            last = ex
        time.sleep(1.0 * (i + 1))
    if last:
        raise RuntimeError("fxtwitter fail: %r" % last)
    return d


def _safe_ext(url, default=".jpg"):
    m = re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", url, re.I)
    if m:
        return "." + m.group(1).lower().replace("jpeg", "jpg")
    return default


def _save(url, dest_path, max_bytes=12 * 1024 * 1024):
    """下载并落盘, 返回相对路径(BASE 内用 / 分隔)。失败返回 None。"""
    try:
        data = http_bytes(url)
        if not data or len(data) > max_bytes:
            return None
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
        return os.path.relpath(dest_path, BASE).replace("\\", "/")
    except Exception:
        return None


def pick_cover(appids, post_json, handle):
    """封面优先级: Steam header_image > 帖子第一张图 > 视频缩略图 > 作者头像。
    返回 cover 字段值(本地相对路径或远程 URL或空)。"""
    # 1) Steam header_image(appdetails 真实带 hash URL, 直接拼 CDN 路径会 404)
    for app in appids:
        try:
            data = json.loads(http_bytes(
                "https://store.steampowered.com/api/appdetails?appids=%s&l=english" % app,
                timeout=20, tries=2))
            node = data.get(app) or {}
            if node.get("success"):
                hi = (node["data"] or {}).get("header_image")
                if hi:
                    p = _save(hi, os.path.join(COVERS_DIR, "%s.jpg" % (app)))
                    if p:
                        return p
                    return hi
        except Exception:
            continue

    tweet = (post_json or {}).get("tweet") or {}
    media = tweet.get("media") or {}
    items = media.get("all") or []

    # 2) 帖子第一张图
    for it in items:
        if it.get("type") == "photo" and it.get("url"):
            u = re.sub(r"\?name=\w+$", "?name=medium", it["url"])
            p = _save(u, os.path.join(COVERS_DIR, "%s%s" % (handle or "unknown", _safe_ext(u))))
            return p or u

    # 3) 视频缩略图
    for it in items:
        th = it.get("thumbnail_url")
        if th:
            p = _save(th, os.path.join(COVERS_DIR, "%s_thumb.jpg" % (handle or "unknown")))
            return p or th

    # 4) 作者头像
    au = tweet.get("author") or (post_json or {}).get("author") or {}
    av = au.get("avatar_url")
    if av:
        p = _save(av, os.path.join(COVERS_DIR, "%s_avatar%s" % (handle or "unknown", _safe_ext(av, ".jpg"))))
        return p or av
    return ""


def post_media(post_json, post_id):
    """帖子媒体 -> {images:[cover值...], video:{mp4, poster}|None}
    图片下载到 media/<postid>_N.jpg; 视频存远程 mp4 + 本地 poster。"""
    out = {"images": [], "video": None}
    tweet = (post_json or {}).get("tweet") or {}
    media = tweet.get("media") or {}
    items = media.get("all") or []
    n = 0
    for it in items:
        t = it.get("type")
        if t == "photo" and it.get("url"):
            n += 1
            u = re.sub(r"\?name=\w+$", "?name=medium", it["url"])
            p = _save(u, os.path.join(MEDIA_DIR, "%s_%d%s" % (post_id, n, _safe_ext(u))))
            out["images"].append(p or u)
        elif t == "video" and out["video"] is None:
            mp4 = None
            # 优先 variants 里最高清晰度 mp4(过滤 HLS)
            best = (0, None)
            for v in (it.get("variants") or []):
                if v.get("content_type") == "video/mp4" and v.get("url"):
                    m = re.search(r"/(\d+)x(\d+)/", v["url"])
                    px = int(m.group(1)) * int(m.group(2)) if m else 0
                    if px >= best[0]:
                        best = (px, v["url"])
            mp4 = best[1] or (it.get("url") if (it.get("url") or "").endswith(".mp4")
                              or ".mp4?" in (it.get("url") or "") else None)
            poster = None
            th = it.get("thumbnail_url")
            if th:
                poster = _save(th, os.path.join(MEDIA_DIR, "%s_poster.jpg" % post_id)) or th
            if mp4:
                out["video"] = {"mp4": mp4, "poster": poster}
    return out


def ensure_cover(row, post_json=None):
    """给已有行补封面(cover 为空时)。post_json 可选, 无则只试 Steam/头像。
    返回是否发生了变化。"""
    if (row.get("cover") or "").strip():
        return False
    handle = (row.get("x_handle") or "").strip().lstrip("@")
    appids = [a.strip() for a in (row.get("steam_appid") or "").split(",") if a.strip()]
    cover = pick_cover(appids, post_json, handle)
    if not cover and post_json:
        # 兜底再试头像(put_cover 内已含), 空就作罢
        pass
    if cover:
        row["cover"] = cover
        return True
    return False
