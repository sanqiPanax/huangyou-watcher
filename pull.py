# -*- coding: utf-8 -*-
"""huangyou-watcher 主拉取脚本
用法: 双击 拉取.cmd, 或 python pull.py
流程: 读 watchlist.csv -> 逐行拉 X/Steam/DLsite -> 更新表 -> 写 events.csv + report.html -> 有变化/错误弹窗
"""
import csv
import hashlib
import html
import io
import json
import os
import re
import ssl
import subprocess
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE, "watchlist.csv")
EVENTS_PATH = os.path.join(BASE, "events.csv")
REPORT_PATH = os.path.join(BASE, "report.html")
CONFIG_PATH = os.path.join(BASE, "config.json")
UPDATES_PATH = os.path.join(BASE, "updates.json")
X_CONFIG = os.path.join(os.path.expanduser("~"), ".agent-reach", "config.yaml")
PROXY = "http://127.0.0.1:7897"
FIELDS = ["name", "x_handle", "steam_appid", "dlsite_id", "expected_release",
          "last_update", "update_count", "x_last_id", "steam_last_release",
          "dlsite_last_release", "cover", "stars", "category", "notes"]

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import media as M
import model as MOD

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE
_DIRECT_FAIL = False  # auto_proxy: 直连一旦失败, 本会话内其余请求直接走代理

import urllib.request
import urllib.error


# ---------- 基础工具 ----------

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_config():
    """config.json: keep_updates = 每个游戏保留最近几次更新(报告展示+存储裁剪)。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        n = int(cfg.get("keep_updates", 5))
        return {"keep_updates": max(1, min(n, 50))}
    except Exception:
        return {"keep_updates": 5}


def load_updates():
    """updates.json: {game: [{t, src, text}, ...]} 每游戏保留最近 keep_updates 条。"""
    try:
        with open(UPDATES_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_updates(d):
    with open(UPDATES_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def http_get(url, timeout=25, tries=3, proxy=False, headers=None, auto_proxy=False):
    """带重试的 GET, 返回 bytes。
    proxy=True: 全程走 Clash。
    auto_proxy=True: 先直连一次(短超时), 失败自动切代理重试 —— DLsite 直连常超时但代理秒通。"""
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
         "Accept-Language": "ja,en;q=0.8"}
    if headers:
        h.update(headers)

    def attempt(use_proxy, timeout_s):
        if use_proxy:
            handlers = [urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})]
        else:
            handlers = [urllib.request.ProxyHandler({})]
        handlers.append(urllib.request.HTTPSHandler(context=SSL_CTX))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, headers=h)
        with opener.open(req, timeout=timeout_s) as r:
            return r.read()

    if auto_proxy:
        global _DIRECT_FAIL
        if not _DIRECT_FAIL:
            try:
                return attempt(False, min(timeout, 10))
            except Exception:
                _DIRECT_FAIL = True  # 本次会话内直连已证明不通, 后续不再浪费 10s
        last = None
        for i in range(tries):
            try:
                return attempt(True, timeout)
            except Exception as e:
                last = e
                time.sleep(1.5 * (i + 1))
        raise RuntimeError("GET fail %s (direct+proxy): %r" % (url, last))

    last = None
    for i in range(tries):
        try:
            return attempt(proxy, timeout)
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError("GET fail %s: %r" % (url, last))


def load_x_cookies():
    """从 agent-reach 配置读 X cookie; 兼容无 yaml 环境的手写解析。"""
    try:
        import yaml
        d = yaml.safe_load(open(X_CONFIG, encoding="utf-8"))
        return str(d["twitter_auth_token"]), str(d["twitter_ct0"])
    except Exception:
        tok = ct0 = None
        for line in open(X_CONFIG, encoding="utf-8"):
            m = re.match(r"\s*twitter_auth_token\s*:\s*(\S+)", line)
            if m:
                tok = m.group(1).strip("'\"")
            m = re.match(r"\s*twitter_ct0\s*:\s*(\S+)", line)
            if m:
                ct0 = m.group(1).strip("'\"")
        if not tok or not ct0:
            raise RuntimeError("X cookie 缺失: %s" % X_CONFIG)
        return tok, ct0


# ---------- 各源拉取 ----------

def fetch_x_posts(handle, n=20):
    """twitter-cli 拉作者时间线, 返回 [{id,text,time}, ...] 按时间倒序。"""
    if not re.match(r"^[A-Za-z0-9_]{1,15}$", handle):
        raise RuntimeError("非法 handle: %r" % handle)
    tok, ct0 = load_x_cookies()
    env = os.environ.copy()
    env["TWITTER_AUTH_TOKEN"] = tok
    env["TWITTER_CT0"] = ct0
    env["PYTHONUTF8"] = "1"
    env["HTTP_PROXY"] = PROXY
    env["HTTPS_PROXY"] = PROXY
    out_p = os.path.join(BASE, "_x_out.json")
    err_p = os.path.join(BASE, "_x_err.txt")
    code = ("import sys; from twitter_cli.cli import cli; "
            "sys.argv=['twitter','-c','user-posts','%s','-n','%d']; cli()" % (handle, n))
    # 沙箱内禁止管道 stdio, 用文件句柄; 真机同样可用
    with open(out_p, "w", encoding="utf-8") as fo, open(err_p, "w", encoding="utf-8") as fe:
        r = subprocess.run([sys.executable, "-c", code], stdout=fo, stderr=fe,
                           timeout=120, env=env, cwd=BASE)
    if r.returncode != 0:
        tail = ""
        try:
            tail = open(err_p, encoding="utf-8", errors="replace").read()[-400:]
        except Exception:
            pass
        raise RuntimeError("twitter-cli rc=%s: %s" % (r.returncode, tail))
    raw = open(out_p, encoding="utf-8", errors="replace").read()
    try:
        data = json.loads(raw)
    except Exception:
        raise RuntimeError("twitter-cli 输出非 JSON: %s" % raw[:200])
    return [{"id": str(t.get("id")), "text": t.get("text") or "",
             "time": t.get("time") or ""} for t in data]


def fetch_steam(appid, cc="cn"):
    """Steam appdetails -> 名称/发售/价格/类型/Demo/语言 一次拿全(FR-05/07/08/12)。
    返回 {'name','date','coming','price':{...}|None,'genres':[..],'demos':[appid..],
          'languages':[..],'is_free':bool}
    价格需带 cc 才返回(price_overview); 未发售/免费返回 None。"""
    url = "https://store.steampowered.com/api/appdetails?appids=%s&l=english&cc=%s" % (appid, cc)
    d = json.loads(http_get(url, tries=3, auto_proxy=True))
    node = d.get(appid, {})
    if not node.get("success"):
        raise RuntimeError("Steam appid %s 查询失败" % appid)
    data = node["data"]
    rd = data.get("release_date") or {}
    price = data.get("price_overview")
    sl = data.get("supported_languages")
    if isinstance(sl, dict):
        langs = list(sl.keys())
    elif isinstance(sl, list):
        langs = [str(x) for x in sl]
    elif isinstance(sl, str):
        langs = [sl]
    else:
        langs = []
    out = {"name": data.get("name") or "",
           "date": rd.get("date") or "",
           "coming": bool(rd.get("coming_soon")),
           "is_free": bool(data.get("is_free")),
           "genres": [g.get("description") for g in (data.get("genres") or [])
                      if g.get("description")],
           "demos": [str(x.get("appid")) for x in (data.get("demos") or [])
                     if isinstance(x, dict) and x.get("appid")],
           "languages": langs,
           "price": None}
    if price:
        out["price"] = {"currency": price.get("currency") or "",
                        "initial": price.get("initial"),
                        "final": price.get("final"),
                        "discount": price.get("discount_percent") or 0,
                        "formatted": price.get("final_formatted") or "",
                        "region": (cc or "").upper()}
    return out


def price_text(p):
    """FR-07: 价格显示为 '原价/现价 + 货币 + 地区'; 缺价格不算免费。"""
    if not p:
        return ""
    cur = p.get("currency") or ""
    reg = p.get("region") or ""
    cury = "%s·%s" % (cur, reg) if cur and reg else (cur or reg)
    fin = p.get("formatted") or ""
    if p.get("discount") and p.get("initial") and p["initial"] != p.get("final"):
        # 原价以货币最小单位计, 只展示数值区间避免货币换算歧义
        return "%s (原价 %s %s, -%d%%) [%s]" % (fin or p.get("final"),
                                                p["initial"], cur, p["discount"], cury)
    return "%s [%s]" % (fin or (p.get("final") if p.get("final") is not None else ""), cury)


def parse_steam_date(s):
    """'Dec 1, 2026' / '2026年9月1日' -> '2026-12-01'/'2026-09-01'; 'Coming soon'/无法解析 -> None"""
    if not s or s.lower() == "coming soon":
        return None
    m = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$", s.strip())
    if m:
        return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
    for fmt in ("%b %d, %Y", "%d %b, %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def date_key(s):
    """比较用归一化: 英文/日文日期归一, 解析不了则原样返回。"""
    return parse_steam_date(s) or (s or "")


def fetch_dlsite(dlsite_id):
    """DLsite 作品页 -> 发售日/标题/价格/分类/体験版/语言 一次解析(FR-05/07/08/12)。
    返回 {'date','title','url','price':{...}|None,'genres':[..],
          'trial':bool,'trial_url':str,'languages':[..]}
    价格取页面 var contents 里的 price/official_price(円, 地区=JP); 体験版看
    trial_download 区块(独立试玩文件, 有则 trial=True)。"""
    urls = []
    if dlsite_id.startswith("http"):
        urls = [dlsite_id]
    elif re.match(r"^[A-Za-z]{2}\d+$", dlsite_id):
        for site in ("maniax", "home", "doujin", "girls"):
            urls.append("https://www.dlsite.com/%s/work/=/product_id/%s.html" % (site, dlsite_id))
    else:
        raise RuntimeError("非法 DLsite 标识: %r" % dlsite_id)
    last_err = None
    for u in urls:
        try:
            page = http_get(u, tries=3, auto_proxy=True).decode("utf-8", "replace")
        except Exception as e:
            last_err = e
            continue
        if "<title>" not in page:
            last_err = RuntimeError("no title in %s" % u)
            continue
        m = re.search(r"販売日</th>\s*<td>.{0,400}?(\d{4})年(\d{2})月(\d{2})日", page, re.S)
        date = "%s-%s-%s" % m.groups() if m else None
        mt = re.search(r"<title>([^<]+)</title>", page)
        title = mt.group(1).strip() if mt else ""
        title = re.split(r"\s*[|｜]\s*DLsite", title)[0].strip()
        # 价格: var contents 里 price(现价)/official_price(定价), 单位円
        price = None
        mp = re.search(r'"price"\s*:\s*([0-9.]+)', page)
        mo = re.search(r'"official_price"\s*:\s*([0-9.]+)', page)
        if mp:
            price = {"currency": "JPY", "region": "JP",
                     "final": int(float(mp.group(1))),
                     "initial": int(float(mo.group(1))) if mo else None,
                     "discount": 0, "formatted": "%s 円" % mp.group(1)}
            if price["initial"] and price["initial"] > price["final"]:
                price["discount"] = round(
                    (1 - price["final"] / float(price["initial"])) * 100)
        # 分类: genre 链接(work.genre 来源) 与 作品形式 图标
        genres = []
        for gm in re.finditer(r'href="[^"]*fsr/=/genre/\d+/from/work\.genre[^"]*"[^>]*>'
                              r'([^<]{1,30})<', page):
            genres.append(gm.group(1).strip())
        for gm in re.finditer(r'href="[^"]*works/type/=/work_type/([A-Z0-9]+)/from/icon\.work"'
                              r'[^>]*>\s*<span[^>]*title="([^"]{1,30})"', page):
            genres.append(gm.group(2).strip())
        # 体験版(FR-12): trial_download 区块 + 下载直链
        trial, trial_url = False, ""
        mt2 = re.search(r'<div class="trial_download[^"]*".{0,600}?'
                        r'href="(//trial\.dlsite\.com/[^"]+)"', page, re.S)
        if mt2:
            trial, trial_url = True, "https:" + mt2.group(1)
        # 语言(FR-08 汉化/语言支持事件用)
        languages = []
        ml = re.search(r"data-supported-languages='(\[[^\]]*\])'", page)
        if ml:
            try:
                languages = json.loads(ml.group(1))
            except Exception:
                languages = []
        return {"date": date, "title": title, "url": u, "price": price,
                "genres": genres, "trial": trial, "trial_url": trial_url,
                "languages": languages}
    raise RuntimeError("DLsite %s 拉取失败: %r" % (dlsite_id, last_err))


# ---------- 通知 ----------

def notify(title, text, icon="Information"):
    """Windows 气球/toast 通知。文本经 PowerShell 单引号转义, Unicode 走 CreateProcessW。"""
    def esc(s):
        return s.replace("'", "''").replace("\r", " ").replace("\n", " ")
    ps = ("Add-Type -AssemblyName System.Windows.Forms; "
          "Add-Type -AssemblyName System.Drawing; "
          "$n=New-Object System.Windows.Forms.NotifyIcon; "
          "$n.Icon=[System.Drawing.SystemIcons]::%s; "
          "$n.Visible=$true; "
          "$n.ShowBalloonTip(7000,'%s','%s','Info'); "
          "Start-Sleep 8; $n.Dispose()" % (icon, esc(title), esc(text)))
    try:
        with open(os.devnull, "w") as dn:
            subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                            "-Command", ps],
                           stdout=dn, stderr=dn, timeout=30)
    except Exception as e:
        print("[warn] 通知失败:", e)


# ---------- 事件与报告 ----------

def append_events(rows_events):
    exists = os.path.exists(EVENTS_PATH) and os.path.getsize(EVENTS_PATH) > 0
    with open(EVENTS_PATH, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["time", "game", "kind", "detail"])
        for ev in rows_events:
            w.writerow(ev)


CSS = """
body{background:#12151c;color:#dbe2ef;font-family:'Segoe UI',system-ui,sans-serif;margin:0;padding:28px;}
.wrap{max-width:1080px;margin:0 auto;}
h1{font-size:26px;margin:0 0 6px;color:#fff;}
.sub{color:#8b94a7;font-size:14px;margin-bottom:14px;}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap;}
.btn{background:#232a38;color:#c9d2e3;border:1px solid #36405270;border-radius:8px;padding:8px 16px;font-size:13px;cursor:pointer;}
.btn:hover{background:#2c3546;}
.sortbtn.active{background:#2c3a56;border-color:#4a6a9a;color:#cfe0ff;}
.sortbar{display:flex;gap:8px;align-items:center;margin-bottom:14px;flex-wrap:wrap;}
.filterbar{display:flex;gap:8px;align-items:center;margin-bottom:16px;flex-wrap:wrap;padding:10px 12px;background:#171c26;border:1px solid #2a3142;border-radius:10px;}
.fbtn{background:#232a38;color:#c9d2e3;border:1px solid #364052;border-radius:20px;padding:6px 15px;font-size:13px;cursor:pointer;}
.fbtn:hover{background:#2c3546;}
.fbtn.active{background:#1d4d33;border-color:#3fae6a;color:#b8f5d4;font-weight:600;}
.fbtn .cnt{opacity:.65;font-size:11.5px;margin-left:4px;}
.card.cat-hidden{display:none;}
.toolbtn{background:#232a38;color:#c9d2e3;border:1px solid #364052;border-radius:8px;padding:7px 14px;font-size:13px;cursor:pointer;}
.toolbtn:hover{background:#2c3546;}
.toolbtn.open{background:#2c3a56;border-color:#4a6a9a;color:#cfe0ff;}
.panel{display:none;background:#171c26;border:1px solid #2a3142;border-radius:10px;padding:16px;margin-bottom:16px;}
.panel.show{display:block;}
.panel h3{margin:0 0 10px;font-size:15px;color:#e7ecf5;}
.panel textarea{width:100%;min-height:150px;background:#12151c;color:#dbe2ef;border:1px solid #364052;border-radius:8px;padding:12px;font-size:13.5px;font-family:Consolas,monospace;resize:vertical;box-sizing:border-box;}
.panel textarea::placeholder{color:#5b6474;}
.panel input{background:#12151c;color:#dbe2ef;border:1px solid #364052;border-radius:8px;padding:9px 12px;font-size:13.5px;width:100%;box-sizing:border-box;}
.panel label{display:block;font-size:12.5px;color:#8b94a7;margin:10px 0 4px;}
.panel .row{display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap;}
.panel .msg{font-size:13px;}
.msg.ok{color:#6fe0a0;}
.msg.err{color:#ff9aa4;}
.msg.warn{color:#f5c451;}
.hint{font-size:12px;color:#5b6474;line-height:1.6;margin-top:8px;}
.stat{display:flex;gap:14px;margin-bottom:24px;flex-wrap:wrap;}
.chip{background:#1b202b;border:1px solid #2a3142;border-radius:10px;padding:12px 18px;}
.chip b{font-size:24px;color:#fff;display:block;}
.chip span{font-size:12px;color:#8b94a7;}
.chip.unread b{color:#f5c451;}
.card{background:#1b202b;border:1px solid #2a3142;border-radius:14px;padding:18px 20px;margin-bottom:16px;display:flex;gap:16px;align-items:flex-start;}
.card.hot{border-color:#3fae6a;}
.card.has-unread{border-color:#8a6d1f;}
.cover{flex:none;width:150px;height:84px;object-fit:cover;border-radius:8px;border:1px solid #2a3142;background:#12151c;}
.cbody{flex:1;min-width:0;}
.badge{display:inline-block;font-size:12px;border-radius:20px;padding:3px 12px;margin-left:10px;vertical-align:middle;}
.b-up{background:#1d4d33;color:#6fe0a0;}
.b-none{background:#252b38;color:#7a8398;}
.b-unread{background:#44360f;color:#f5c451;}
.gname{font-size:20px;font-weight:600;color:#fff;}
.meta{color:#8b94a7;font-size:13px;margin:8px 0 4px;}
.meta a{color:#6ab7ff;text-decoration:none;}
.updhead{margin:12px 0 4px;font-size:12px;color:#7a8398;}
.updlist{margin-top:6px;}
.upd{display:flex;gap:10px;align-items:baseline;padding:7px 10px;border-radius:8px;font-size:13.5px;cursor:pointer;border-left:3px solid transparent;margin:4px 0;line-height:1.5;flex-wrap:wrap;}
.upd:hover{filter:brightness(1.25);}
.upd.unread{background:#2a2415;border-left-color:#f5c451;color:#f7e6bb;}
.upd.read{background:#171b24;border-left-color:#2a3142;color:#6b7488;}
.upd .dot{flex:none;width:8px;height:8px;border-radius:50%;align-self:center;}
.upd.unread .dot{background:#f5c451;box-shadow:0 0 6px #f5c45180;}
.upd.read .dot{background:#3a4152;}
.upd .ts{flex:none;font-size:11.5px;color:#8b94a7;white-space:nowrap;}
.upd.read .ts{color:#4d5568;}
.upd .src{flex:none;font-size:10.5px;border-radius:4px;padding:1px 7px;background:#252b38;color:#9aa4b8;}
.upd.unread .src{background:#44360f;color:#f5c451;}
.upd.read .src{background:#1e232e;color:#4d5568;}
.upd .txt{flex:1;min-width:0;word-break:break-word;}
.upd a{color:#6ab7ff;text-decoration:none;}
.upd.read a{color:#4d5568;}
.upd .state{flex:none;font-size:11px;color:#8b94a7;opacity:.7;}
.umed{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;align-items:flex-start;flex-basis:100%;}
.umed .um{height:120px;width:auto;max-width:280px;object-fit:cover;border-radius:6px;border:1px solid #2a3142;display:block;}
.upd.read .umed .um{opacity:.45;filter:grayscale(.6);}
.umed .uv{width:min(380px,100%);border-radius:6px;background:#000;display:block;}
.upd.read .umed .uv{opacity:.55;}
.kv{display:flex;gap:24px;flex-wrap:wrap;margin-top:10px;font-size:13px;color:#a9b2c3;}
.kv b{color:#e7ecf5;}
.errbox{background:#331a1d;border:1px solid #6e2b32;border-radius:10px;padding:12px 16px;color:#ff9aa4;font-size:14px;margin-bottom:18px;}
.errbox summary{cursor:pointer;font-weight:600;}
h2{font-size:17px;color:#c6cede;margin:28px 0 10px;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th,td{padding:7px 10px;text-align:left;border-bottom:1px solid #2a3142;color:#a9b2c3;}
th{color:#7a8398;font-weight:600;}
.cardhead{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;width:100%;}
.hleft{flex:1;min-width:0;}
.hright{flex:none;display:flex;flex-direction:column;align-items:flex-end;gap:6px;}
.stars{display:inline-flex;gap:3px;font-size:17px;user-select:none;line-height:1;}
.star{color:#39404f;cursor:pointer;transition:color .08s,transform .08s;}
.star:hover{transform:scale(1.2);}
.star.on{color:#f5c451;text-shadow:0 0 6px #f5c45150;}
.unbtn{background:none;border:1px solid #4a3038;color:#a06b75;font-size:11px;border-radius:6px;padding:3px 10px;cursor:pointer;}
.unbtn:hover{background:#3a1f24;color:#ff9aa4;border-color:#8a4048;}
.catb{display:inline-block;font-size:11px;border-radius:4px;padding:2px 9px;background:#243044;color:#7fa8e0;margin-left:8px;vertical-align:middle;}
.catb.c-game{background:#1d3a2a;color:#6fe0a0;}
.catb.c-video{background:#3a2a1d;color:#f0b46f;}
.catb.c-art{background:#33204a;color:#c69af0;}
.catb.c-nsfw{background:#4a1d2a;color:#f08aa0;}
.catb.c-other{background:#252b38;color:#9aa4b8;}
.freq{font-size:11.5px;color:#8b94a7;}
footer{color:#5b6474;font-size:12px;margin-top:30px;line-height:1.7;}
/* ---- REQUIREMENTS P0/P1: 证据 / 事件 / 候选 / 提醒 ---- */
.evhead{margin:14px 0 4px;font-size:12px;color:#7a8398;display:flex;gap:10px;align-items:center;flex-wrap:wrap;}
.factbox{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px;}
.fact{background:#171c26;border:1px solid #2a3142;border-radius:8px;padding:7px 11px;font-size:12.5px;color:#a9b2c3;max-width:100%;}
.fact b{color:#e7ecf5;font-weight:600;}
.fact .fsrc{display:block;font-size:11px;color:#5b6474;margin-top:3px;}
.fact .fsrc a{color:#6ab7ff;text-decoration:none;}
.fact.st-failed{border-color:#6e2b32;background:#26151a;}
.fact.st-failed b{color:#ff9aa4;}
.fact.st-delayed{border-color:#6e5a2b;background:#261f14;}
.fact.st-delayed b{color:#f5c451;}
.fact.st-confirmed b,.fact.st-released b{color:#6fe0a0;}
.tagrow{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px;}
.ptag{font-size:11.5px;border-radius:4px;padding:2px 8px;background:#243044;color:#7fa8e0;}
.ptag.ai{background:#33204a;color:#c69af0;}
.ptag.demo{background:#1d4d33;color:#6fe0a0;}
.ptag.demo.none{background:#252b38;color:#7a8398;}
.evlist{margin-top:6px;}
.evrow{border-left:3px solid #2a3142;background:#171b24;border-radius:0 8px 8px 0;padding:7px 11px;margin:5px 0;font-size:13px;color:#a9b2c3;}
.evrow .evt{font-size:11.5px;color:#8b94a7;margin-right:8px;}
.evrow .evk{display:inline-block;font-size:11px;border-radius:4px;padding:1px 7px;background:#1d3a2a;color:#6fe0a0;margin-right:8px;}
.evrow .evk.k-delay,.evrow .evk.k-price{background:#44360f;color:#f5c451;}
.evrow .evk.k-x_post,.evrow .evk.k-other{background:#252b38;color:#9aa4b8;}
.evrow .evsrc{font-size:11.5px;color:#5b6474;}
.evrow .evsrc a{color:#6ab7ff;text-decoration:none;}
.evrow .evmore{font-size:11.5px;color:#7a8398;cursor:pointer;}
.evmore:hover{color:#6ab7ff;}
.evdetail{display:none;font-size:11.5px;color:#7a8398;margin-top:5px;border-top:1px dashed #2a3142;padding-top:5px;}
.evdetail.show{display:block;}
.panel h4{margin:14px 0 6px;font-size:13.5px;color:#c6cede;}
.cand{background:#12151c;border:1px solid #2a3142;border-radius:10px;padding:12px 14px;margin:8px 0;}
.cand .ctitle{font-size:14px;color:#e7ecf5;font-weight:600;}
.cand .cmeta{font-size:12px;color:#8b94a7;margin-top:4px;word-break:break-all;}
.cand .cmeta a{color:#6ab7ff;text-decoration:none;}
.cand .cev{font-size:12px;color:#9aa4b8;margin-top:5px;}
.cand .crow{display:flex;gap:8px;margin-top:9px;align-items:center;flex-wrap:wrap;}
.cand select{background:#1b202b;color:#dbe2ef;border:1px solid #364052;border-radius:6px;padding:6px 8px;font-size:12.5px;max-width:280px;}
.conf{font-size:11px;border-radius:4px;padding:1px 7px;background:#243044;color:#7fa8e0;}
.conf.low{background:#44360f;color:#f5c451;}
.alertrow{display:flex;gap:8px;align-items:center;padding:5px 0;font-size:13px;color:#a9b2c3;flex-wrap:wrap;}
.alertrow input{accent-color:#3fae6a;width:15px;height:15px;}
.alertrow .scope{font-size:11.5px;color:#5b6474;}
.otherworks{font-size:12px;color:#8b94a7;margin-top:7px;}
.otherworks a{color:#6ab7ff;text-decoration:none;margin-right:8px;}
.pendingbtn{background:#2c3a56;border:1px solid #4a6a9a;color:#cfe0ff;font-size:12px;border-radius:6px;padding:4px 12px;cursor:pointer;margin-left:8px;}
.pendingbtn .pcnt{background:#f5c451;color:#12151c;border-radius:9px;padding:0 6px;font-weight:700;margin-left:4px;}
.inputerr{background:#26151a;border:1px solid #6e2b32;border-radius:8px;padding:9px 12px;font-size:12.5px;color:#ff9aa4;margin:6px 0;word-break:break-all;}
.inputerr .why{color:#c98b93;font-size:11.5px;}
"""

READ_JS = """
(function(){
  var KEY='hy_read_v1';
  var read={};
  try{read=JSON.parse(localStorage.getItem(KEY)||'{}');}catch(e){read={};}
  function save(){try{localStorage.setItem(KEY,JSON.stringify(read));}catch(e){}}
  function refresh(){
    var n=0;
    document.querySelectorAll('.upd').forEach(function(el){
      var uid=el.getAttribute('data-uid');
      var isRead=!!read[uid];
      el.classList.toggle('read',isRead);
      el.classList.toggle('unread',!isRead);
      var st=el.querySelector('.state');
      if(st) st.textContent=isRead?'已读':'未读';
      var card=el.closest('.card');
      var hidden=card&&card.classList.contains('cat-hidden');
      if(!isRead&&!hidden) n++;   // 未读计数只算当前分类可见的
    });
    var chip=document.getElementById('unread-chip');
    if(chip) chip.textContent=n;
    document.querySelectorAll('.card').forEach(function(card){
      var un=card.querySelectorAll('.upd.unread').length;
      var b=card.querySelector('.b-unread');
      if(b){
        if(un>0){b.style.display='';b.textContent='未读 '+un;}
        else{b.style.display='none';}
      }
      card.classList.toggle('has-unread',un>0);
    });
  }
  document.querySelectorAll('.upd').forEach(function(el){
    el.addEventListener('click',function(ev){
      var uid=el.getAttribute('data-uid');
      if(!read[uid]){read[uid]=1;save();refresh();}
      var a=ev.target.closest('a');
      if(a){ev.stopPropagation();return;}
    });
    el.querySelectorAll('a').forEach(function(a){a.target='_blank';a.rel='noopener';});
  });
  var btn=document.getElementById('mark-all');
  if(btn) btn.addEventListener('click',function(){
    document.querySelectorAll('.upd').forEach(function(el){read[el.getAttribute('data-uid')]=1;});
    save();refresh();
  });

  /* ---- 本地服务 API(星星/取消关注), 服务未启动时给出提示 ---- */
  var API='http://127.0.0.1:8790';
  var HYW_TOKEN='__TOKEN__';   // 本地接口认证(构建时嵌入, FR-17~20 收紧后必带)
  var svcOk=null; // null=未探测
  function svcHint(msg){
    var h=document.getElementById('svc-hint');
    if(!h) return;
    if(msg){h.innerHTML=msg;h.style.display='';}
    else{h.style.display='none';}
  }
  function post(path, data){
    return fetch(API+path,{method:'POST',headers:{'Content-Type':'application/json',
      'X-Hyw-Token':HYW_TOKEN},
      body:JSON.stringify(data)}).then(function(r){
      if(r.status===0) throw new Error('down');
      return r.json().then(function(j){
        if(!r.ok){var e=new Error(j.error||('HTTP '+r.status));e.status=r.status;throw e;}
        return j;});
    });
  }
  function getAuth(path){
    return fetch(API+path,{headers:{'X-Hyw-Token':HYW_TOKEN}}).then(function(r){
      if(r.status===0) throw new Error('down');
      return r.json().then(function(j){
        if(!r.ok){var e=new Error(j.error||('HTTP '+r.status));e.status=r.status;throw e;}
        return j;});
    });
  }
  fetch(API+'/api/ping').then(function(r){svcOk=r.ok;}).catch(function(){svcOk=false;});

  /* ---- 星星评分 ---- */
  document.querySelectorAll('.stars').forEach(function(box){
    var game=box.getAttribute('data-game');
    var cur=parseInt(box.getAttribute('data-stars')||'1',10);
    function paint(n){
      box.querySelectorAll('.star').forEach(function(s){
        s.classList.toggle('on', parseInt(s.getAttribute('data-n'),10)<=n);
      });
    }
    paint(cur);
    box.querySelectorAll('.star').forEach(function(s){
      s.addEventListener('click',function(ev){
        ev.stopPropagation();
        var n=parseInt(s.getAttribute('data-n'),10);
        var old=cur;
        cur=n;paint(n); // 乐观更新
        post('/api/stars',{game:game,stars:n}).then(function(){
          var card=box.closest('.card');
          if(card){card.setAttribute('data-stars',n);}
          svcHint('');
          applySort(currentSort,true);
        }).catch(function(e){
          cur=old;paint(old);
          if(e.message==='down'){
            svcHint('<b>本地服务未启动</b>: 点保存没反应。双击 <b>拉取.cmd</b> 跑一次(会自动带起服务), 星星就能存了。');
          }else{
            svcHint('保存失败: '+e.message+(e.status===409?' (拉取进行中, 稍后再点)':''));
          }
        });
      });
    });
  });

  /* ---- 取消关注 ---- */
  document.querySelectorAll('.unbtn').forEach(function(b){
    b.addEventListener('click',function(ev){
      ev.stopPropagation();
      var game=b.getAttribute('data-game');
      if(!window.confirm('取消关注「'+game+'」?\\n(会从关注表移除, 记录备份到 unfollowed.csv)')) return;
      post('/api/unfollow',{game:game}).then(function(){
        var card=b.closest('.card');
        card.style.transition='opacity .3s';card.style.opacity='0';
        setTimeout(function(){card.remove();updateCount();},300);
        svcHint('');
      }).catch(function(e){
        if(e.message==='down'){
          svcHint('<b>本地服务未启动</b>: 双击 <b>拉取.cmd</b> 跑一次带起服务后再取消。');
        }else{
          svcHint('取消失败: '+e.message);
        }
      });
    });
  });
  function updateCount(){
    var chip=document.querySelectorAll('.stat .chip b')[0];
    if(!chip) return;
    var vis=document.querySelectorAll('.card:not(.cat-hidden)').length;
    chip.textContent=vis;
  }

  /* ---- 分类筛选: 只看某类, 回到卡片顶部; 选择存浏览器本地 ---- */
  var currentCat='';
  var CAT_KEY='hy_cat_v1';
  try{currentCat=localStorage.getItem(CAT_KEY)||'';}catch(e){}
  function applyCat(cat,scrollTop){
    currentCat=cat;
    document.querySelectorAll('.card').forEach(function(card){
      var c=card.getAttribute('data-cat')||'未分类';
      card.classList.toggle('cat-hidden', !!cat && c!==cat);
    });
    document.querySelectorAll('.fbtn').forEach(function(b){
      b.classList.toggle('active', b.getAttribute('data-cat')===cat);
    });
    try{localStorage.setItem(CAT_KEY,cat);}catch(e){}
    updateCount();
    refresh();
    if(scrollTop){
      var cards=document.getElementById('cards');
      if(cards){
        var y=cards.getBoundingClientRect().top+window.pageYOffset-70;
        window.scrollTo({top:y,behavior:'smooth'});
      }
    }
  }
  document.querySelectorAll('.fbtn').forEach(function(b){
    b.addEventListener('click',function(){applyCat(b.getAttribute('data-cat'),true);});
  });

  /* ---- 排序: 默认/关注度/更新频率 ---- */
  var currentSort='default';
  var SORT_KEY='hy_sort_v1';
  try{currentSort=localStorage.getItem(SORT_KEY)||'default';}catch(e){}
  function applySort(mode,keepFocus){
    currentSort=mode;
    var wrap=document.getElementById('cards');
    if(!wrap) return;
    var cards=Array.prototype.slice.call(wrap.querySelectorAll('.card'));
    cards.sort(function(a,b){
      if(mode==='stars'){
        var d=parseInt(b.getAttribute('data-stars')||'1',10)-parseInt(a.getAttribute('data-stars')||'1',10);
        if(d) return d;
      }
      if(mode==='freq'){
        var f=parseInt(b.getAttribute('data-freq')||'0',10)-parseInt(a.getAttribute('data-freq')||'0',10);
        if(f) return f;
      }
      return parseInt(a.getAttribute('data-idx')||'0',10)-parseInt(b.getAttribute('data-idx')||'0',10);
    });
    cards.forEach(function(c){wrap.appendChild(c);});
    document.querySelectorAll('.sortbtn').forEach(function(b2){
      b2.classList.toggle('active', b2.getAttribute('data-sort')===mode);
    });
    try{localStorage.setItem(SORT_KEY,mode);}catch(e){}
  }
  document.querySelectorAll('.sortbtn').forEach(function(b){
    b.addEventListener('click',function(){applySort(b.getAttribute('data-sort'));});
  });
  applySort(currentSort);
  applyCat(currentCat,false);   // 启动恢复上次分类筛选(不滚动)

  /* ---- 面板: 批量添加 / 模型 API ---- */
  function togglePanel(btnId,panelId){
    var b=document.getElementById(btnId), p=document.getElementById(panelId);
    if(!b||!p) return;
    b.addEventListener('click',function(){
      var on=p.classList.toggle('show');
      b.classList.toggle('open',on);
    });
  }
  togglePanel('tg-add','p-add');
  togglePanel('tg-llm','p-llm');
  togglePanel('tg-cand','p-cand');
  togglePanel('tg-alert','p-alert');
  function setMsg(id,txt,cls){
    var el=document.getElementById(id);
    if(!el) return;
    el.textContent=txt; el.className='msg '+(cls||'');
  }

  // 批量添加
  var addGo=document.getElementById('add-go');
  if(addGo) addGo.addEventListener('click',function(){
    var ta=document.getElementById('add-ta');
    var text=(ta&&ta.value||'').trim();
    if(!text){setMsg('add-msg','先把链接粘进输入框','warn');return;}
    addGo.disabled=true; setMsg('add-msg','提交中...','');
    post('/api/batch_add',{text:text}).then(function(j){
      setMsg('add-msg','已提交 '+j.count+' 个链接, 后台添加中(点"查看后台日志"看进度)','ok');
      ta.value='';
    }).catch(function(e){
      addGo.disabled=false;
      if(e.message==='down') setMsg('add-msg','本地服务未启动: 双击 拉取.cmd 带起后再试','err');
      else setMsg('add-msg','提交失败: '+e.message,'err');
    });
  });
  var addLogBtn=document.getElementById('add-log-btn');
  if(addLogBtn) addLogBtn.addEventListener('click',function(){
    var box=document.getElementById('add-log');
    if(!box) return;
    getAuth('/api/add_log').then(function(j){
      box.style.display='block';
      box.textContent=j.log||'(空)';
      box.scrollTop=box.scrollHeight;
    }).catch(function(){setMsg('add-msg','日志读取失败(本地服务未启动)','err');});
  });

  // 模型 API 配置
  function loadLlm(){
    getAuth('/api/llm_config').then(function(j){
      var c=j.config||{};
      var u=document.getElementById('llm-url'),k=document.getElementById('llm-key'),
          m=document.getElementById('llm-model'),h=document.getElementById('llm-hdr');
      if(u) u.value=c.base_url||''; if(k) k.value=c.api_key||''; if(m) m.value=c.model||'';
      if(h) h.value=c.headers?JSON.stringify(c.headers):'';
      if(c.base_url) setMsg('llm-msg','已配置: '+c.base_url+' ('+(c.model||'?')+')','ok');
    }).catch(function(){setMsg('llm-msg','读取失败(本地服务未启动)','err');});
  }
  function llmVal(id){var el=document.getElementById(id);return el?(el.value||'').trim():'';}
  function llmPayload(){
    var p={base_url:llmVal('llm-url'),api_key:llmVal('llm-key'),model:llmVal('llm-model')};
    var hs=llmVal('llm-hdr');
    if(hs && hs!=='{}'){
      try{ p.headers=JSON.parse(hs); }
      catch(e){ setMsg('llm-msg','额外 Header 不是合法 JSON','warn'); return null; }
    }
    return p;
  }
  var llmSave=document.getElementById('llm-save');
  if(llmSave) llmSave.addEventListener('click',function(){
    var payload=llmPayload(); if(!payload) return;
    if(!payload.base_url){setMsg('llm-msg','Base URL 为空(要清除请点"清除")','warn');return;}
    post('/api/llm_config',payload).then(function(){
      setMsg('llm-msg','已保存到 llm.json (仅本地, 不进 Git)','ok');
    }).catch(function(e){
      setMsg('llm-msg', e.message==='down'?'本地服务未启动':'保存失败: '+e.message,'err');
    });
  });
  var llmTest=document.getElementById('llm-test');
  if(llmTest) llmTest.addEventListener('click',function(){
    var payload=llmPayload(); if(!payload) return;
    if(!payload.base_url){setMsg('llm-msg','先填 Base URL','warn');return;}
    llmTest.disabled=true; setMsg('llm-msg','测试中(最长40秒)...','');
    post('/api/llm_test',payload).then(function(j){
      llmTest.disabled=false;
      setMsg('llm-msg','✔ 连通! 模型='+j.model+' 回复: '+j.reply,'ok');
    }).catch(function(e){
      llmTest.disabled=false;
      setMsg('llm-msg','✘ '+(e.message==='down'?'本地服务未启动':e.message),'err');
    });
  });
  var llmClear=document.getElementById('llm-clear');
  if(llmClear) llmClear.addEventListener('click',function(){
    post('/api/llm_config',{base_url:''}).then(function(){
      var u=document.getElementById('llm-url'),k=document.getElementById('llm-key'),
          m=document.getElementById('llm-model'),h=document.getElementById('llm-hdr');
      if(u)u.value='';if(k)k.value='';if(m)m.value='';if(h)h.value='';
      setMsg('llm-msg','已清除, 回退 DeepSeek','ok');
    }).catch(function(e){setMsg('llm-msg','清除失败: '+e.message,'err');});
  });
  // 打开模型面板时自动回填已存配置
  var tgLlm=document.getElementById('tg-llm');
  if(tgLlm) tgLlm.addEventListener('click',function(){
    if(document.getElementById('p-llm').classList.contains('show')) loadLlm();
  });

  /* ---- FR-03 候选确认/拒绝 ---- */
  document.querySelectorAll('.cand-ok').forEach(function(b){
    b.addEventListener('click',function(){
      var box=b.closest('.cand'); var cid=box.getAttribute('data-cid');
      var sel=box.querySelector('.cand-target');
      var msg=box.querySelector('.cand-msg');
      b.disabled=true; if(msg){msg.textContent='提交中...';msg.className='msg cand-msg';}
      post('/api/candidate',{id:cid,action:'confirm',target:sel?sel.value:''})
        .then(function(){
          if(msg){msg.textContent='已确认';msg.className='msg cand-msg ok';}
          box.style.transition='opacity .3s';box.style.opacity='0';
          setTimeout(function(){box.remove();bumpCandCnt(-1);},300);
        }).catch(function(e2){
          b.disabled=false;
          if(msg){
            msg.textContent = e2.message==='down'
              ? '本地服务未启动: 双击 拉取.cmd 带起后再试' : '失败: '+e2.message;
            msg.className='msg cand-msg err';
          }
        });
    });
  });
  document.querySelectorAll('.cand-no').forEach(function(b){
    b.addEventListener('click',function(){
      var box=b.closest('.cand'); var cid=box.getAttribute('data-cid');
      if(!window.confirm('拒绝该候选? (之后不再提示这条链接)')) return;
      var msg=box.querySelector('.cand-msg');
      post('/api/candidate',{id:cid,action:'reject'})
        .then(function(){
          if(msg){msg.textContent='已拒绝';msg.className='msg cand-msg ok';}
          box.style.transition='opacity .3s';box.style.opacity='0';
          setTimeout(function(){box.remove();bumpCandCnt(-1);},300);
        }).catch(function(e2){
          if(msg){
            msg.textContent = e2.message==='down'?'本地服务未启动':'失败: '+e2.message;
            msg.className='msg cand-msg err';
          }
        });
    });
  });
  function bumpCandCnt(d){
    var el=document.getElementById('cand-cnt'); if(!el) return;
    var n=Math.max(0,(parseInt(el.textContent||'0',10)||0)+d);
    el.textContent=n;
    var btn=document.getElementById('tg-cand'); if(btn) btn.style.display=n?'':'none';
  }

  /* ---- FR-11 提醒设置保存 ---- */
  var alSave=document.getElementById('al-save');
  if(alSave) alSave.addEventListener('click',function(){
    var picks=[];
    document.querySelectorAll('#p-alert .al-chk').forEach(function(c){
      picks.push({key:c.getAttribute('data-key'),value:c.checked});
    });
    var msg=document.getElementById('al-msg');
    alSave.disabled=true; if(msg){msg.textContent='保存中...';msg.className='msg';}
    post('/api/alerts',{items:picks}).then(function(){
      alSave.disabled=false;
      if(msg){msg.textContent='已保存('+picks.length+' 项)';msg.className='msg ok';}
    }).catch(function(e2){
      alSave.disabled=false;
      if(msg){
        msg.textContent = e2.message==='down'
          ? '本地服务未启动: 双击 拉取.cmd 带起后再试' : '失败: '+e2.message;
        msg.className='msg err';
      }
    });
  });

  /* ---- FR-11 单作品静音 ---- */
  document.querySelectorAll('.mutebtn').forEach(function(b){
    b.addEventListener('click',function(ev){
      ev.stopPropagation();
      var muted=b.textContent.indexOf('已静音')>=0;
      post('/api/mute',{game:b.getAttribute('data-game'),mute:!muted})
        .then(function(){
          b.textContent = !muted ? '🔕 已静音' : '🔔 提醒';
        }).catch(function(e2){
          alert(e2.message==='down'
            ? '本地服务未启动: 双击 拉取.cmd 带起后再试' : '失败: '+e2.message);
        });
    });
  });

  /* ---- 事件证据展开 ---- */
  document.querySelectorAll('.evmore').forEach(function(sp){
    sp.addEventListener('click',function(ev){
      ev.stopPropagation();
      var d=document.getElementById(sp.getAttribute('data-evd'));
      if(d) d.classList.toggle('show');
    });
  });

  /* ---- FR-08 同作者其它作品跳转 ---- */
  document.querySelectorAll('[data-jump]').forEach(function(a){
    a.addEventListener('click',function(ev){
      ev.preventDefault();
      var target=a.getAttribute('data-jump');
      var cards=document.querySelectorAll('#cards .card');
      for(var i=0;i<cards.length;i++){
        var nm=cards[i].querySelector('.gname');
        if(nm && nm.textContent===target){
          cards[i].scrollIntoView({behavior:'smooth',block:'center'});
          cards[i].style.outline='2px solid #4a6a9a';
          setTimeout(function(c){return function(){c.style.outline='';};}(cards[i]),1600);
          break;
        }
      }
    });
  });

  refresh();
})();
"""


def upd_uid(game, t, src, text):
    """更新条目稳定 ID: 报告重新生成后已读状态仍能对上。"""
    return hashlib.md5(("%s|%s|%s|%s" % (game, t, src, text)).encode("utf-8")).hexdigest()[:16]


def linkify(s):
    """把文本里的 URL 变成 <a>(已 html-escape 的文本上操作)。"""
    return re.sub(r"(https?://[^\s<]+)",
                  lambda m: "<a href='%s'>%s</a>" % (m.group(1), m.group(1)), s)


def _facts_html(g, gname, e):
    """FR-05/06/07/08/12: 把一个作品的最新事实渲染成证据块。
    每块显示 值 + 来源 + 抓取时间; 抓取失败/延期单独配色(§4 状态有时间)。"""
    facts = (g.get("facts", {}).get(gname) or {})
    if not facts:
        return ""
    order = [("release_status", "发售状态"), ("release_date", "发售日"),
             ("price", "价格"), ("demo", "Demo"), ("genres", "类型"),
             ("language", "语言")]
    blocks, tags = [], []
    for field, label in order:
        lst = facts.get(field) or []
        if not lst:
            continue
        # 最新一条; 同字段多来源(Steam/DLsite)各展示一条最新(FR-07 不跨地区比较)
        latest_by_src = {}
        for it in reversed(lst):
            s = it.get("src") or "?"
            if s not in latest_by_src:
                latest_by_src[s] = it
        if field == "genres":
            # 类型用标签展示(FR-08 平台原始标签 + AI 归类分开)
            for s, it in latest_by_src.items():
                for tag in [t.strip() for t in (it.get("v") or "").split(",") if t.strip()]:
                    tags.append("<span class='ptag' title='%s · %s'>%s</span>"
                                % (e(s), e(it.get("at") or ""), e(tag)))
            continue
        for s, it in list(latest_by_src.items()):
            v = it.get("v") or ""
            if v in ("", "unknown", "none") and field != "release_status":
                continue
            st = it.get("status") or ""
            cls = ""
            if st == "failed":
                cls = " st-failed"
            elif st in ("delayed", "author_post", "store_tbc"):
                cls = " st-delayed"
            elif st in ("confirmed", "released"):
                cls = " st-confirmed"
            if field == "release_status":
                v = MOD.RELEASE_LABEL.get(v, v)
            if field == "demo":
                v = MOD.DEMO_LABEL.get(v, v)
                if v == "无Demo":
                    continue  # 默认不显示"无", 避免噪声(有 Demo 才显示)
            url = it.get("url") or ""
            src_html = ("%s · 抓取 %s" % (e(s), e(it.get("at") or "")))
            if url:
                src_html = "<a href='%s' target='_blank' rel='noopener'>%s</a>" % (
                    e(url), src_html)
            if it.get("note") and field in ("release_status", "demo"):
                src_html += " · %s" % e(it["note"][:60])
            if st == "failed":
                v = "抓取失败"
            blocks.append("<div class='fact%s'><b>%s</b> %s<span class='fsrc'>%s</span></div>"
                          % (cls, e(label), e(v or "—"), src_html))
    ai = ""
    # AI 归类单独标注(FR-08: 平台原始标签与 AI 归类分开)
    parts = []
    if tags:
        parts.append("<div class='evhead'>类型(平台原始标签)</div>"
                     "<div class='tagrow'>%s</div>" % "".join(tags))
    if blocks:
        parts.append("<div class='evhead'>作品档案(每项可点来源核对)</div>"
                     "<div class='factbox'>%s</div>" % "".join(blocks))
    if not parts:
        return ""
    return "".join(parts)


def build_report(rows, changes_map, errors, run_meta, updates, keep, g=None):
    """生成 report.html。
    changes_map: id(row) -> [(src, text)] 本轮变化
    updates: {game: [{t,src,text}]} 每游戏最近 keep 条更新历史
    g: graph.json 证据库(事实/事件/候选/提醒), 用于证据与待确认面板
    已读状态存浏览器 localStorage(按 uid), 不受报告重新生成影响。"""
    if g is None:
        g = MOD.load_graph()
    e = html.escape
    updated = sum(1 for r in rows if changes_map.get(id(r)))
    total_updates = sum(int(r.get("update_count") or 0) for r in rows)

    parts = ["<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>",
             "<meta name='viewport' content='width=device-width,initial-scale=1'>",
             "<title>黄油关注提示</title><style>%s</style></head><body><div class='wrap'>" % CSS,
             "<h1>黄油关注提示</h1>",
             "<div class='sub'>拉取时间 %s &nbsp;·&nbsp; 耗时 %s &nbsp;·&nbsp; 每游戏保留最近 %d 次更新 "
             "&nbsp;·&nbsp; <span style='color:#8b94a7'>X 走公开时间线, <b style='color:#6fe0a0'>无需关注</b>, 推荐流零污染</span></div>"
             % (e(run_meta["time"]), e(run_meta["elapsed"]), keep)]

    parts.append("<div class='toolbar'>"
                 "<button class='btn' id='mark-all'>全部标为已读</button>"
                 "<button class='toolbtn' id='tg-add'>＋ 批量添加链接</button>"
                 "<button class='toolbtn' id='tg-llm'>⚙ 模型 API</button>"
                 "<span style='font-size:12.5px;color:#8b94a7'>点条目=标已读;未读琥珀高亮,状态存浏览器本地</span>"
                 "</div>")

    # ---- 批量添加面板(逗号/换行分隔多链接) ----
    parts.append(
        "<div class='panel' id='p-add'><h3>批量添加链接</h3>"
        "<textarea id='add-ta' spellcheck='false' placeholder='把链接粘进来,用逗号分隔,可一次多个。&#10;&#10;例:"
        " https://x.com/someone/status/123, https://x.com/another, https://store.steampowered.com/app/12345&#10;&#10;"
        "支持: X帖子链接 / X裸主页 / Steam页 / DLsite页; 也支持逗号+换行混排'></textarea>"
        "<div class='row'>"
        "<button class='btn' id='add-go'>开始添加</button>"
        "<button class='btn' id='add-log-btn'>查看后台日志</button>"
        "<span class='msg' id='add-msg'></span></div>"
        "<div class='hint'>后台执行(每条约 30秒~2分钟, 含抓封面+AI分类), 关掉本页也不影响;"
        "完成后需重新拉取或刷新才进报告。已有游戏自动跳过(按 handle/Steam/DLsite 查重)。</div>"
        "<pre id='add-log' style='display:none;margin-top:10px;max-height:260px;overflow:auto;"
        "background:#12151c;border:1px solid #2a3142;border-radius:8px;padding:10px;"
        "font-size:12px;color:#9aa4b8;white-space:pre-wrap;'></pre>"
        "</div>")

    # ---- 模型 API 面板 ----
    parts.append(
        "<div class='panel' id='p-llm'><h3>模型 API(OpenAI 兼容, 用于 AI 分类)</h3>"
        "<label>Base URL(例: http://127.0.0.1:4096/v1 或 https://api.deepseek.com/v1)</label>"
        "<input id='llm-url' spellcheck='false' placeholder='http://127.0.0.1:端口/v1'>"
        "<label>API Key(本地服务可留空)</label>"
        "<input id='llm-key' type='password' spellcheck='false' placeholder='sk-...'>"
        "<label>模型名</label>"
        "<input id='llm-model' spellcheck='false' placeholder='例: qwen3-coder / deepseek-chat'>"
        "<label>额外 Header (JSON, 可选 — 某些网关要求, 如 opencode-go 的 session 头)</label>"
        "<input id='llm-hdr' spellcheck='false' placeholder='例如: {&quot;x-opencode-session&quot;: &quot;xxx&quot;}'>"
        "<div class='row'>"
        "<button class='btn' id='llm-save'>保存</button>"
        "<button class='btn' id='llm-test'>测试连接</button>"
        "<button class='btn' id='llm-clear'>清除(回退DeepSeek)</button>"
        "<span class='msg' id='llm-msg'></span></div>"
        "<div class='hint'>配置存本地 llm.json(<b>不会上传 Git</b>); 保存后下次添加/分类即走此模型,"
        "连不上时自动回退 DeepSeek。测试连接会发一条 2 字短消息验证。</div>"
        "</div>")

    # ---- FR-03 待确认候选面板 ----
    pend = MOD.pending_candidates(g)
    parts.append("<div class='sortbar'>"
                 "<span style='font-size:12.5px;color:#8b94a7'>排序:</span>"
                 "<button class='btn sortbtn' data-sort='default'>默认顺序</button>"
                 "<button class='btn sortbtn' data-sort='stars'>关注度 ★</button>"
                 "<button class='btn sortbtn' data-sort='freq'>更新频率</button>"
                 "<button class='toolbtn' id='tg-cand'%s>⚑ 待确认<span class='pcnt' id='cand-cnt'>%d</span></button>"
                 "<button class='toolbtn' id='tg-alert'>🔔 提醒设置</button>"
                 "<span id='svc-hint' class='svc-off' style='display:none'></span>"
                 "</div>" % (" style='display:none'" if not pend else "",
                             len(pend)))

    # ---- FR-03 待确认候选面板 ----
    cand_parts = ["<div class='panel' id='p-cand'><h3>待确认候选(FR-03)</h3>"]
    if not pend:
        cand_parts.append("<div class='hint'>暂无待确认候选。添加链接时若匹配不确定,"
                          "会先进这里, 不会自动写进关注表。</div>")
    for c in pend:
        conf = float(c.get("confidence") or 0)
        cand_parts.append(
            "<div class='cand' data-cid='%s'>"
            "<div class='ctitle'>%s <span class='conf%s'>置信 %.0f%%</span></div>"
            "<div class='cmeta'><a href='%s' target='_blank' rel='noopener'>%s</a></div>"
            "<div class='cev'>%s</div>"
            "<div class='crow'>挂到作品: <select class='cand-target'>"
            "<option value='(新作品)'>＋ 作为新作品入库</option>%s</select>"
            "<button class='btn cand-ok'>确认</button>"
            "<button class='btn cand-no'>拒绝</button>"
            "<span class='msg cand-msg'></span></div>"
            "</div>" % (
                e(c.get("id")), e(c.get("title") or c.get("platform") or "候选"),
                " low" if conf < 0.6 else "", conf * 100,
                e(c.get("url")), e(c.get("url")),
                e("%s · %s" % (c.get("reason") or "", "证据: " + "; ".join(
                    "%s%s" % (x.get("from", ""), "(转发)" if x.get("retweet") else "")
                    for x in (c.get("evidence") or []))
                    if c.get("evidence") else "")),
                "".join("<option value='%s'>%s</option>" % (e(r2.get("name") or ""),
                                                            e(r2.get("name") or ""))
                        for r2 in rows)))
    cand_parts.append("</div>")

    # ---- FR-11 提醒设置面板 ----
    ap = MOD.alert_prefs(g)
    alert_parts = ["<div class='panel' id='p-alert'><h3>提醒设置(FR-11)</h3>",
                   "<div class='hint'>勾选的事件类型才弹 Windows 通知; 不勾的只进报告。"
                   "普通 X 帖默认不提醒(低噪声)。来源开关单独控制。</div>",
                   "<h4>事件类型(全局)</h4>"]
    for k in ("demo", "release", "delay", "listing", "price", "version", "lang",
              "x_post", "other"):
        alert_parts.append(
            "<label class='alertrow'><input type='checkbox' class='al-chk' data-key='%s' "
            "data-scope='global' %s> %s<span class='scope'>%s</span></label>"
            % (k, "checked" if ap.get(k) else "", e(MOD.EVENT_LABEL.get(k, k)),
               " · 默认关" if k in ("x_post", "other") else ""))
    alert_parts.append("<h4>来源(全局)</h4>")
    for k, lab in (("src_x", "X 帖"), ("src_steam", "Steam"), ("src_dlsite", "DLsite")):
        alert_parts.append(
            "<label class='alertrow'><input type='checkbox' class='al-chk' data-key='%s' "
            "data-scope='global' %s> %s</label>" % (k, "checked" if ap.get(k) else "", lab))
    alert_parts.append("<div class='row'><button class='btn' id='al-save'>保存提醒设置</button>"
                       "<span class='msg' id='al-msg'></span></div>")
    alert_parts.append("<h4>单个作品静音</h4><div class='hint'>卡片右上角 🔔 按钮 = "
                       "静音/恢复该作品的全部提醒(按作品单独控制)。</div></div>")

    # ---- FR-01 未识别输入 ----
    inputerr_parts = []
    if g.get("input_errors"):
        inputerr_parts.append("<h2>未识别的输入(FR-01, 保留原链接与原因)</h2>")
        for it in g["input_errors"][-30:]:
            inputerr_parts.append(
                "<div class='inputerr'>%s<div class='why'>%s · %s</div></div>"
                % (e(it.get("input", "")), e(it.get("reason", "")), e(it.get("at", ""))))

    # 三个面板在 sortbar 之后、统计区之前插入
    parts.append("".join(cand_parts))
    parts.append("".join(alert_parts))

    # 更新频率 = 最近 30 天事件数(events.csv 统计, 无事件=0)
    freq30 = {}
    if os.path.exists(EVENTS_PATH) and os.path.getsize(EVENTS_PATH) > 0:
        cutoff = (datetime.now().timestamp() - 30 * 86400)
        with open(EVENTS_PATH, encoding="utf-8-sig", newline="") as f:
            for ev in csv.DictReader(f):
                if ev.get("kind") in ("X", "Steam", "DLsite"):
                    try:
                        ets = datetime.strptime(ev.get("time", ""), "%Y-%m-%d %H:%M:%S").timestamp()
                    except ValueError:
                        continue
                    if ets >= cutoff:
                        gm = ev.get("game", "")
                        freq30[gm] = freq30.get(gm, 0) + 1

    parts.append("<div class='stat'>"
                 "<div class='chip'><b>%d</b><span>关注游戏</span></div>"
                 "<div class='chip unread'><b id='unread-chip'>0</b><span>未读更新</span></div>"
                 "<div class='chip'><b>%d</b><span>本轮有更新</span></div>"
                 "<div class='chip'><b>%d</b><span>累计更新总次数</span></div>"
                 "</div>" % (len(rows), updated, total_updates))

    if errors:
        parts.append("<div class='errbox'><details open><summary>⚠ %d 个源拉取失败</summary><ul>" % len(errors))
        for err in errors:
            parts.append("<li>%s</li>" % e(err))
        parts.append("</ul></details></div>")

    parts.append("".join(inputerr_parts))

    # ---- 分类筛选栏(动态统计) ----
    cat_order = ["游戏", "插画", "3D", "卖肉", "视频", "其他"]
    cat_counts = {}
    for r in rows:
        c = (r.get("category") or "").strip() or "未分类"
        cat_counts[c] = cat_counts.get(c, 0) + 1
    shown_cats = [c for c in cat_order if cat_counts.get(c)]
    shown_cats += [c for c in sorted(cat_counts) if c not in cat_order]  # 未知/未分类兜底

    parts.append("<div class='filterbar'>"
                 "<span style='font-size:12.5px;color:#8b94a7'>分类:</span>"
                 "<button class='fbtn active' data-cat=''>全部<span class='cnt'>%d</span></button>"
                 % len(rows))
    for c in shown_cats:
        parts.append("<button class='fbtn' data-cat='%s'>%s<span class='cnt'>%d</span></button>"
                     % (e(c), e(c), cat_counts[c]))
    parts.append("<span style='font-size:12px;color:#5b6474;margin-left:4px'>"
                 "点分类只看该类;选择记在浏览器本地</span></div>")

    parts.append("<div id='cards'>")
    for idx, r in enumerate(rows):
        chg = changes_map.get(id(r)) or []
        gname = r.get("name") or "(未命名)"
        hot = bool(chg)
        hist = list(reversed(updates.get(gname) or []))  # 新→旧
        stars = max(1, min(5, int(r.get("stars") or "1" or 1)))
        try:
            stars = max(1, min(5, int(str(r.get("stars") or "1"))))
        except ValueError:
            stars = 1
        freq = freq30.get(gname, 0)
        cat = (r.get("category") or "").strip()
        cat_cls = {"游戏": "c-game", "视频": "c-video", "绘画": "c-art",
                   "卖肉": "c-nsfw"}.get(cat, "c-other")
        parts.append("<div class='card%s' data-idx='%d' data-stars='%d' data-freq='%d' data-cat='%s'>"
                     % (" hot" if hot else "", idx, stars, freq, e(cat or "未分类")))
        cover = (r.get("cover") or "").strip()
        if cover:
            parts.append("<img class='cover' src='%s' alt='' loading='lazy' "
                         "onerror=\"this.style.display='none'\">" % e(cover))
        parts.append("<div class='cbody'>")
        # 头部: 左=名称+徽章, 右=星星+取消关注
        parts.append("<div class='cardhead'><div class='hleft'>")
        parts.append("<span class='gname'>%s</span>" % e(gname))
        if cat:
            parts.append("<span class='catb %s'>%s</span>" % (cat_cls, e(cat)))
        parts.append("<span class='badge %s'>%s</span>" % ("b-up" if hot else "b-none",
                                                          "本轮 +%d" % len(chg) if hot else "无更新"))
        parts.append("<span class='badge b-unread' style='display:none'>0</span>")
        if freq:
            parts.append("<span class='freq'>近30天 %d 次</span>" % freq)
        parts.append("</div><div class='hright'>")
        star_html = "".join(
            "<span class='star' data-n='%d'>★</span>" % n for n in range(1, 6))
        parts.append("<span class='stars' data-game='%s' data-stars='%d'>%s</span>"
                     % (e(gname), stars, star_html))
        # FR-11: 按作品静音开关
        muted = not MOD.alert_prefs(g, gname, r.get("x_handle") or "").get("mute", True)
        parts.append("<button class='unbtn mutebtn' data-game='%s' title='静音/恢复该作品提醒'>%s</button>"
                     % (e(gname), "🔕 已静音" if muted else "🔔 提醒"))
        parts.append("<button class='unbtn' data-game='%s'>取消关注</button>" % e(gname))
        parts.append("</div></div>")  # /cardhead
        links = []
        if r.get("x_handle"):
            links.append("<a href='https://x.com/%s' target='_blank' rel='noopener'>@%s</a>"
                         % (e(r["x_handle"]), e(r["x_handle"])))
        for app in [a.strip() for a in (r.get("steam_appid") or "").split(",") if a.strip()]:
            links.append("<a href='https://store.steampowered.com/app/%s' target='_blank' rel='noopener'>Steam %s</a>"
                         % (e(app), e(app)))
        if r.get("dlsite_id"):
            did = r["dlsite_id"]
            du = did if did.startswith("http") else "https://www.dlsite.com/maniax/work/=/product_id/%s.html" % e(did)
            links.append("<a href='%s' target='_blank' rel='noopener'>DLsite %s</a>" % (du, e(did)))
        if links:
            parts.append("<div class='meta'>%s</div>" % " &nbsp;·&nbsp; ".join(links))

        # ---- 作品档案证据区(FR-05/06/07/08/12): 每项带来源与抓取时间 ----
        parts.append(_facts_html(g, gname, e))

        # ---- 事件时间线(FR-09/10): 跨来源合并后的事件, 附全部证据 ----
        evs = MOD.row_events(g, gname, limit=8)
        if evs:
            parts.append("<div class='evhead'>事件时间线(跨来源已去重)</div><div class='evlist'>")
            for ev in evs:
                srcs = ev.get("sources") or []
                src_html = " · ".join(
                    "<a href='%s' target='_blank' rel='noopener'>%s</a>" % (
                        e(s.get("url") or "#"), e(s.get("src") or "?"))
                    for s in srcs if s.get("url")) or "—"
                et = ev.get("etype") or "other"
                detail_id = "evd-%s" % ev.get("key", "")[:12]
                more = ("" if len(srcs) <= 1 else
                        " <span class='evmore' data-evd='%s'>证据×%d ▾</span>" % (
                            detail_id, ev.get("count") or len(srcs)))
                parts.append(
                    "<div class='evrow'><span class='evk k-%s'>%s</span>%s"
                    "<span class='evt'>%s → %s</span>"
                    "<span class='evsrc'>%s%s</span>"
                    "<div class='evdetail' id='%s'>%s</div></div>" % (
                        e(et), e(MOD.EVENT_LABEL.get(et, et)),
                        e(ev.get("title") or ""),
                        e((ev.get("first_at") or "")[5:16]),
                        e((ev.get("last_at") or "")[5:16]),
                        src_html, more, detail_id,
                        "<br>".join(
                            "[%s %s] %s" % (e(s.get("src") or ""),
                                            e((s.get("at") or "")[5:16]),
                                            e((s.get("text") or "")[:160]))
                            for s in srcs)))
            parts.append("</div>")

        # ---- FR-08 同作者的其它作品 ----
        if r.get("x_handle"):
            sibs = [x for x in MOD.works_by_creator(rows, r["x_handle"])
                    if (x.get("name") or "") != gname]
            if sibs:
                parts.append("<div class='otherworks'>该作者(@%s)其它作品: %s</div>" % (
                    e(r["x_handle"]),
                    " ".join("<a href='#' data-jump='%s'>%s</a>" % (
                        e(s.get("name") or ""), e(s.get("name") or ""))
                        for s in sibs)))

        if hist:
            parts.append("<div class='updhead'>最近更新(最多 %d 条, 新在上):</div>" % keep)
            parts.append("<div class='updlist'>")
            for u in hist:
                uid = upd_uid(gname, u.get("t", ""), u.get("src", ""), u.get("text", ""))
                md = u.get("media") or {}
                media_html = ""
                imgs = md.get("images") or []
                vid = md.get("video") or {}
                blocks = []
                for im in imgs[:6]:
                    blocks.append("<a href='%s' target='_blank' rel='noopener'>"
                                  "<img class='um' src='%s' loading='lazy' alt=''>"
                                  "</a>" % (e(im), e(im)))
                if vid.get("mp4"):
                    poster = (" poster='%s'" % e(vid["poster"])) if vid.get("poster") else ""
                    blocks.append("<video class='uv' controls preload='none'%s src='%s'></video>"
                                  % (poster, e(vid["mp4"])))
                if blocks:
                    media_html = "<div class='umed'>%s</div>" % "".join(blocks)
                parts.append(
                    "<div class='upd' data-uid='%s'>"
                    "<span class='dot'></span>"
                    "<span class='ts'>%s</span>"
                    "<span class='src'>%s</span>"
                    "<span class='txt'>%s</span>"
                    "<span class='state'></span>"
                    "%s"
                    "</div>" % (uid, e(u.get("t", "")[5:]), e(u.get("src", "")),
                               linkify(e(u.get("text", ""))), media_html))
            parts.append("</div>")

        parts.append("<div class='kv'>"
                     "<span>预计发售 <b>%s</b></span>"
                     "<span>上次更新 <b>%s</b></span>"
                     "<span>更新次数 <b>%s</b></span>"
                     "<span>近30天 <b>%d</b> 次更新</span>"
                     "</div>" % (e(r.get("expected_release") or "—"),
                                 e(r.get("last_update") or "首次拉取"),
                                 e(str(r.get("update_count") or 0)),
                                 freq))
        parts.append("</div>")  # /cbody
        parts.append("</div>")  # /card
    parts.append("</div>")  # /#cards

    # 历史事件(最近 20 条, 纯审计用)
    hist2 = []
    if os.path.exists(EVENTS_PATH) and os.path.getsize(EVENTS_PATH) > 0:
        with open(EVENTS_PATH, encoding="utf-8-sig", newline="") as f:
            hist2 = list(csv.DictReader(f))[-20:]
    if hist2:
        parts.append("<h2>最近事件(审计)</h2><table><tr><th>时间</th><th>游戏</th><th>类型</th><th>内容</th></tr>")
        for hrow in reversed(hist2):
            parts.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                e(hrow.get("time", "")), e(hrow.get("game", "")),
                e(hrow.get("kind", "")), e(hrow.get("detail", ""))))
        parts.append("</table>")

    parts.append("<footer>数据源: X(twitter-cli 公开时间线, <b>不关注/不点赞/不互动</b>) · Steam Web API · DLsite 作品页<br>"
                 "文件: watchlist.csv(关注表,手改加行) · updates.json(每游戏最近 %d 条更新) · config.json(keep_updates) · "
                 "events.csv(审计日志) · report.html(本报告)</footer>" % keep)
    parts.append("<script>%s</script>" % READ_JS.replace(
        "__TOKEN__", MOD.load_or_create_token()))
    parts.append("</div></body></html>")
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("".join(parts))


# ---------- 主流程 ----------

def load_rows():
    if not os.path.exists(CSV_PATH):
        raise RuntimeError("找不到关注表: %s (先双击 添加.cmd 录入第一个游戏)" % CSV_PATH)
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in FIELDS:
            r.setdefault(k, "")
        if not (r.get("stars") or "").strip():
            r["stars"] = "1"  # 关注度默认 1 星
    return rows


def save_rows(rows):
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def _x_event_norm(etype, text, post_id):
    """FR-10 归一化键: demo/lang/listing 用类型本身(跨来源合并); 发售/延期取
    文中日期; 价格取文中金额; 都取不到就退回帖子 id(仅同帖合并)。"""
    if etype in ("demo", "lang", "listing", "version", "other", "x_post"):
        if etype == "version":
            m = re.search(r"\bv?\d+\.\d+(?:\.\d+)?\b", text or "")
            return m.group(0) if m else etype
        return etype
    if etype in ("release", "delay"):
        m = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", text or "")
        if m:
            return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
        m = re.search(r"(\d{1,2})月(\d{1,2})日", text or "")
        if m:
            return "%s-%02d-%02d" % (datetime.now().year, int(m.group(1)), int(m.group(2)))
        return etype
    if etype == "price":
        m = re.search(r"[¥￥$]\s?([\d,]+)", text or "")
        if m:
            return m.group(1).replace(",", "")
        return etype
    return post_id or etype


def pull_row(r, errors, g, alert_log):
    """拉取单行三个源, 返回变化列表; 就地更新行字段。基线(字段为空)只记录不计更新。
    g: graph.json 证据库(事实/事件/候选); alert_log: 收集应弹窗的新事件。"""
    changes = []
    events = []
    name = r.get("name") or "(未命名)"

    # --- X ---
    handle = (r.get("x_handle") or "").strip().lstrip("@")
    if handle:
        try:
            posts = fetch_x_posts(handle, n=20)
            if posts:
                newest = max(posts, key=lambda p: int(p["id"]))
                last_id = (r.get("x_last_id") or "").strip()
                if not last_id:
                    r["x_last_id"] = newest["id"]
                    events.append((now_str(), name, "Init", "X 基线建立: 最新帖 %s" % newest["id"]))
                else:
                    new_posts = [p for p in posts if int(p["id"]) > int(last_id)]
                    if new_posts:
                        new_posts.sort(key=lambda p: int(p["id"]))
                        r["x_last_id"] = str(max(int(p["id"]) for p in new_posts))
                        # 每条新帖一条更新(带图/视频); 超过 10 条只逐帖展示最近 10, 更早的打包
                        head = new_posts[-10:]
                        if len(new_posts) > 10:
                            changes.append(("X", "X @%s 更早还有 %d 条新帖未逐条展示"
                                            % (handle, len(new_posts) - 10)))
                        for p in head:
                            preview = p["text"].replace("\n", " ")[:150]
                            purl = "https://x.com/%s/status/%s" % (handle, p["id"])
                            md = None
                            try:
                                pj = M.fxtwitter_post(handle, p["id"])
                                md = M.post_media(pj, p["id"])
                            except Exception:
                                md = None  # 媒体抓失败不阻塞更新本身
                            changes.append(("X", "X @%s 新帖: %s %s" % (handle, preview, purl),
                                            md))
                            # FR-09/10: 重要更新识别 + 跨来源事件合并
                            etype = MOD.classify_event_text(p["text"], "x")
                            norm = _x_event_norm(etype, p["text"], p["id"])
                            ev, is_new = MOD.upsert_event(
                                g, name, etype,
                                "%s: %s" % (MOD.EVENT_LABEL.get(etype, etype),
                                            preview[:80]),
                                {"src": "X", "url": purl, "text": p["text"][:300]},
                                norm=norm)
                            if is_new and MOD.should_notify(g, name, etype, "x", handle):
                                alert_log.append((name, etype, ev))
                        for p in new_posts:
                            events.append((now_str(), name, "X",
                                           "新帖 https://x.com/%s/status/%s" % (handle, p["id"])))
            time.sleep(0.6)
        except Exception as ex:
            errors.append("X @%s: %s" % (handle, ex))
            MOD.record_fact(g, name, "x_feed", "抓取失败", "X", status="failed",
                            note=str(ex)[:160])

    # --- Steam ---
    apps = [a.strip() for a in (r.get("steam_appid") or "").split(",") if a.strip()]
    if apps:
        try:
            old_map = {}
            if (r.get("steam_last_release") or "").strip():
                try:
                    old_map = json.loads(r["steam_last_release"])
                except Exception:
                    old_map = {}
            new_map = {}
            first_name = ""
            for app in apps:
                info = fetch_steam(app)
                new_map[app] = info["date"]
                if not first_name and info["name"]:
                    first_name = info["name"]
                surl = "https://store.steampowered.com/app/%s" % app
                # FR-05 事实: 发售日/价格/类型/Demo/语言, 各带来源与抓取时间
                prev_f = MOD.latest_fact(g, name, "release_date", src="Steam")
                prev_norm = (prev_f or {}).get("v") or ""
                nd = parse_steam_date(info["date"]) or ""
                st, st_note = MOD.classify_release(
                    nd, info["coming"], prev_date=prev_norm,
                    author_claimed=bool((r.get("expected_release") or "").strip()))
                MOD.record_fact(g, name, "release_date", nd or info["date"] or "",
                                "Steam", url=surl, status=st, note=st_note)
                MOD.record_fact(g, name, "release_status", st, "Steam", url=surl,
                                status=st, note=MOD.RELEASE_LABEL.get(st, st))
                pt = price_text(info["price"])
                new_pv = pt or ("免费" if info["is_free"] else "未标价")
                prev_p = MOD.latest_fact(g, name, "price", src="Steam")
                MOD.record_fact(g, name, "price", new_pv, "Steam", url=surl,
                                status="free" if info["is_free"] else ("ok" if pt else "none"))
                MOD.record_fact(g, name, "genres", ", ".join(info["genres"]),
                                "Steam", url=surl)
                if info["languages"]:
                    MOD.record_fact(g, name, "language", ", ".join(info["languages"][:12]),
                                    "Steam", url=surl)
                # FR-12 Demo: appdetails.demos 字段(本体页入口+独立Demo页)
                dst = MOD.demo_state_from_steam(info["demos"])
                prev_demo = MOD.latest_fact(g, name, "demo", src="Steam")
                MOD.record_fact(g, name, "demo", dst, "Steam", url=surl,
                                status=dst, note="%s" % ",".join(info["demos"])
                                if info["demos"] else "")
                if dst != "none" and (not prev_demo or prev_demo.get("v") != dst):
                    ev, is_new = MOD.upsert_event(
                        g, name, "demo",
                        "Steam 出现 Demo(%s)" % (",".join(info["demos"]) or "入口"),
                        {"src": "Steam", "url": surl, "text": "demos=%s" % info["demos"]},
                        norm="demo")
                    if is_new and MOD.should_notify(g, name, "demo", "steam", handle):
                        alert_log.append((name, "demo", ev))
                # 价格变化事件(FR-09)
                if (prev_p and prev_p.get("v") and prev_p["v"] != new_pv
                        and prev_p["v"] not in ("免费", "未标价")
                        and new_pv not in ("免费", "未标价")):
                    ev, is_new = MOD.upsert_event(
                        g, name, "price",
                        "Steam 价格 %s -> %s" % (prev_p["v"], new_pv),
                        {"src": "Steam", "url": surl,
                         "text": "%s -> %s" % (prev_p["v"], new_pv)},
                        norm=new_pv)
                    if is_new and MOD.should_notify(g, name, "price", "steam", handle):
                        alert_log.append((name, "price", ev))
                    changes.append(("Steam", "Steam 价格变化: %s -> %s"
                                    % (prev_p["v"], new_pv)))
                time.sleep(0.5)
            if not (r.get("steam_last_release") or "").strip():
                r["steam_last_release"] = json.dumps(new_map, ensure_ascii=False)
                events.append((now_str(), name, "Init", "Steam 基线: %s" % json.dumps(new_map, ensure_ascii=False)))
            else:
                diffs = []
                for app in apps:
                    o, n = old_map.get(app), new_map.get(app)
                    if date_key(o) != date_key(n):
                        diffs.append("%s: %s -> %s" % (app, o or "(无)", n or "(无)"))
                        events.append((now_str(), name, "Steam", "%s 发售日 %s -> %s" % (app, o or "(无)", n or "(无)")))
                        # FR-06: 新日期晚于旧日期 = 延期; 否则发售日变更
                        od, nd2 = parse_steam_date(o), parse_steam_date(n)
                        etype = "delay" if (od and nd2 and nd2 > od) else "release"
                        ev, is_new = MOD.upsert_event(
                            g, name, etype,
                            "Steam 发售日 %s -> %s" % (o or "(无)", n or "(无)"),
                            {"src": "Steam",
                             "url": "https://store.steampowered.com/app/%s" % app,
                             "text": "%s -> %s" % (o or "(无)", n or "(无)")},
                            norm=nd2 or n or "date")
                        if is_new and MOD.should_notify(g, name, etype, "steam", handle):
                            alert_log.append((name, etype, ev))
                if diffs:
                    changes.append(("Steam", "Steam 发售日变化: " + "; ".join(diffs)))
                # 恒回写: 语言/格式差异(日文->英文)静默归一, 不计更新
                r["steam_last_release"] = json.dumps(new_map, ensure_ascii=False)
            # 名称兜底
            if not (r.get("name") or "").strip() and first_name:
                r["name"] = first_name
                name = first_name
        except Exception as ex:
            errors.append("Steam %s: %s" % (r.get("steam_appid"), ex))
            MOD.record_fact(g, name, "steam_feed", "抓取失败", "Steam",
                            status="failed", note=str(ex)[:160])

    # --- DLsite ---
    did = (r.get("dlsite_id") or "").strip()
    if did:
        try:
            info = fetch_dlsite(did)
            durl = info.get("url") or "https://www.dlsite.com/maniax/work/=/product_id/%s.html" % did
            dl_st = MOD.classify_release(info["date"] or "", False,
                                         author_claimed=bool((r.get("expected_release") or "").strip()))[0]
            MOD.record_fact(g, name, "release_date", info["date"] or "",
                            "DLsite", url=durl, status=dl_st)
            MOD.record_fact(g, name, "release_status", dl_st, "DLsite", url=durl,
                            status=dl_st, note=MOD.RELEASE_LABEL.get(dl_st, dl_st))
            if info.get("price"):
                MOD.record_fact(g, name, "price",
                                "%s [%s·%s]" % (info["price"]["formatted"],
                                                info["price"]["currency"],
                                                info["price"]["region"]),
                                "DLsite", url=durl, status="ok")
            if info.get("genres"):
                MOD.record_fact(g, name, "genres", ", ".join(info["genres"][:8]),
                                "DLsite", url=durl)
            if info.get("languages"):
                MOD.record_fact(g, name, "language", ", ".join(info["languages"]),
                                "DLsite", url=durl)
            # FR-12: 体験版(独立试玩文件)
            dst = MOD.demo_state_from_dlsite(info.get("trial"))
            prev_demo = MOD.latest_fact(g, name, "demo", src="DLsite")
            MOD.record_fact(g, name, "demo", dst, "DLsite", url=durl, status=dst,
                            note=info.get("trial_url") or "")
            if dst != "none" and (not prev_demo or prev_demo.get("v") in ("none", None, "")):
                ev, is_new = MOD.upsert_event(
                    g, name, "demo", "DLsite 出现体験版",
                    {"src": "DLsite", "url": durl, "text": info.get("trial_url") or "trial"},
                    norm="demo")
                if is_new and MOD.should_notify(g, name, "demo", "dlsite", handle):
                    alert_log.append((name, "demo", ev))
            old = (r.get("dlsite_last_release") or "").strip()
            if not old:
                r["dlsite_last_release"] = info["date"] or "(无販売日)"
                events.append((now_str(), name, "Init", "DLsite 基线販売日: %s" % (info["date"] or "(无)")))
            elif info["date"] and info["date"] != old:
                changes.append(("DLsite", "DLsite 販売日: %s -> %s" % (old, info["date"])))
                events.append((now_str(), name, "DLsite", "販売日 %s -> %s" % (old, info["date"])))
                etype = "delay" if info["date"] > old else "release"
                ev, is_new = MOD.upsert_event(
                    g, name, etype,
                    "DLsite 販売日 %s -> %s" % (old, info["date"]),
                    {"src": "DLsite", "url": durl, "text": "%s -> %s" % (old, info["date"])},
                    norm=info["date"])
                if is_new and MOD.should_notify(g, name, etype, "dlsite", handle):
                    alert_log.append((name, etype, ev))
                r["dlsite_last_release"] = info["date"]
            if not (r.get("name") or "").strip() and info.get("title"):
                r["name"] = info["title"]
                name = info["title"]
            time.sleep(0.8)
        except Exception as ex:
            errors.append("DLsite %s: %s" % (did, ex))
            MOD.record_fact(g, name, "dlsite_feed", "抓取失败", "DLsite",
                            status="failed", note=str(ex)[:160])

    # --- 预计发售自动填(只填空, 不覆盖手填) ---
    if not (r.get("expected_release") or "").strip():
        cand = None
        did2 = (r.get("dlsite_id") or "").strip()
        # Steam coming soon 的具体日期优先
        for app in apps:
            try:
                sm = {}
                if (r.get("steam_last_release") or "").strip():
                    sm = json.loads(r["steam_last_release"])
                ds = parse_steam_date(sm.get(app, ""))
                if ds:
                    cand = ds
                    break
            except Exception:
                pass
        if not cand and did2:
            dl = (r.get("dlsite_last_release") or "").strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}$", dl) and dl >= datetime.now().strftime("%Y-%m-%d"):
                cand = dl
        if cand:
            r["expected_release"] = cand
            events.append((now_str(), name, "Auto", "预计发售填为 %s" % cand))

    # --- 封面补全(只补空, 每次拉取最多尝试一轮; 失败下次再试) ---
    if not (r.get("cover") or "").strip():
        pj = None
        if handle and (r.get("x_last_id") or "").strip():
            try:
                pj = M.fxtwitter_post(handle, r["x_last_id"])
            except Exception:
                pj = None
        try:
            if M.ensure_cover(r, pj):
                events.append((now_str(), name, "Auto", "封面补全: %s" % r["cover"]))
        except Exception:
            pass

    if changes:
        r["update_count"] = str(int(r.get("update_count") or 0) + 1)
        r["last_update"] = now_str()
    return changes, events


def ensure_server():
    """确保 server.py 在 127.0.0.1:8790 运行(星星/取消关注的写回通道)。
    detached 拉起: 不随本进程退出, 独立存活。
    存活探测用真 HTTP(本环境 connect_ex 有假阴性, 禁用端口原语)。"""
    def busy():
        try:
            req = urllib.request.Request("http://127.0.0.1:8790/api/ping")
            with urllib.request.urlopen(req, timeout=1.5) as r:
                return json.loads(r.read()).get("ok") is True
        except Exception:
            return False
    if busy():
        print("本地服务已在运行 (127.0.0.1:8790)")
        return True
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    for attempt in range(2):  # 重试1次: 上一实例刚死时端口有释放竞态
        try:
            with open(os.devnull, "rb") as dn:
                subprocess.Popen([sys.executable, "-u", os.path.join(BASE, "server.py")],
                                 cwd=BASE, stdin=dn, stdout=dn, stderr=dn,
                                 creationflags=flags)
            time.sleep(1.5)
            if busy():
                print("本地服务已启动 (http://127.0.0.1:8790)")
                return True
        except Exception as ex:
            print("[warn] 本地服务启动异常: %r" % ex)
            break
        time.sleep(1.0)
    print("[warn] 本地服务启动失败 (星星/取消关注暂不可用, 下次拉取重试)")
    return False


def main():
    # 单实例锁: 防止双击两次/上一次卡住时重复拉取造成 CSV 写竞争
    lock_p = os.path.join(BASE, ".pull.lock")
    if os.path.exists(lock_p):
        try:
            age = time.time() - os.path.getmtime(lock_p)
        except OSError:
            age = 0
        if age < 15 * 60:
            print("已有一次拉取在运行(锁 %d 分钟前创建), 本次退出。" % int(age // 60))
            return 0
        print("发现陈旧锁(%.0f 分钟), 覆盖继续。" % (age / 60))
    with open(lock_p, "w") as f:
        f.write(str(os.getpid()))
    try:
        return _run()
    finally:
        try:
            os.remove(lock_p)
        except OSError:
            pass


def _run():
    t0 = time.time()
    errors = []
    all_events = []
    changes_map = {}
    cfg = load_config()
    keep = cfg["keep_updates"]
    updates = load_updates()
    run_time = now_str()
    g = MOD.load_graph()
    alert_log = []   # (作品, 事件类型, event) — FR-11 过滤后应弹窗的

    rows = load_rows()
    print("关注表 %d 个游戏, 开始拉取...(每游戏保留最近 %d 次更新)" % (len(rows), keep))
    for r in rows:
        print(" - %s" % (r.get("name") or r.get("x_handle") or r.get("steam_appid") or "?"))
        try:
            chg, evs = pull_row(r, errors, g, alert_log)
        except Exception as ex:
            errors.append("行级异常 %s: %s" % (r.get("name"), ex))
            chg, evs = [], []
        if chg:
            changes_map[id(r)] = chg
            # 追加进该游戏的更新历史, 裁剪到最近 keep 条(旧的丢弃)
            gname = r.get("name") or "(未命名)"
            hist = updates.get(gname) or []
            for ch in chg:
                src, text = ch[0], ch[1]
                md = ch[2] if len(ch) > 2 else None
                entry = {"t": run_time, "src": src, "text": text}
                if md:
                    if md.get("images") or md.get("video"):
                        entry["media"] = md
                hist.append(entry)
            updates[gname] = hist[-keep:]
        all_events.extend(evs)

    save_rows(rows)
    save_updates(updates)
    append_events(all_events)
    try:
        MOD.save_graph(g)
    except Exception as ex:
        errors.append("graph.json 保存失败: %s" % ex)
    elapsed = "%.1fs" % (time.time() - t0)
    build_report(rows, changes_map, errors, {"time": run_time, "elapsed": elapsed},
                 updates, keep, g)

    n_upd = len(changes_map)
    print()
    print("完成: 本轮 %d 个游戏有更新, %d 个重要事件待提醒, %d 个源失败, 耗时 %s"
          % (n_upd, len(alert_log), len(errors), elapsed))
    print("报告: %s" % REPORT_PATH)

    # 自动带起本地服务(星星评分/取消关注要靠它写表), 已在跑则跳过
    ensure_server()

    # FR-11: 只有通过提醒偏好的事件才弹窗; 普通 X 帖只进报告(低噪声)
    if alert_log:
        # 同类合并一条通知, 附来源与作品
        by_type = {}
        for gname, etype, ev in alert_log:
            by_type.setdefault(etype, []).append((gname, ev))
        lines = []
        for etype, items in by_type.items():
            label = MOD.EVENT_LABEL.get(etype, etype)
            srcs = sorted({s.get("src", "") for _, ev in items
                           for s in ev.get("sources", [])})
            names = sorted({n for n, _ in items})
            lines.append("%s×%d: %s (%s)" % (label, len(items),
                                             "/".join(names[:3]),
                                             "+".join(x for x in srcs if x)))
        notify("黄油关注提示 - 重要更新",
               "%d 个事件。%s" % (len(alert_log), "; ".join(lines[:4])),
               icon="Information")
    elif n_upd:
        notify("黄油关注提示",
               "%d 个游戏有新动态(普通帖, 见报告)。" % n_upd,
               icon="Information")
    elif errors:
        notify("黄油关注提示 - 拉取有错误",
               "%d 个源失败(可能 X cookie 过期), 详见报告。" % len(errors),
               icon="Warning")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        import traceback
        traceback.print_exc()
        try:
            notify("黄油关注提示 - 启动失败", str(exc)[:120], icon="Error")
        except Exception:
            pass
        sys.exit(1)
