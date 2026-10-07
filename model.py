# -*- coding: utf-8 -*-
"""huangyou-watcher 身份与证据层 (REQUIREMENTS P0+P1, FR-01..FR-12)

职责:
- FR-01 输入类型识别 + 解析失败留存(原链接 + 原因)
- FR-02 X 内容外链/作品名/作者名提取, 标注证据来源; 转发/回复链接不算作者作品
- FR-03 候选作品匹配(置信度) + 确认/拒绝
- FR-04 作者/作品/页面区分: handle 只代表作者, 不再等同于一部游戏(去重规则在 add_game)
- FR-05 动态事实(日期/价格/Demo/类型)记录来源 + 抓取时间 + 历史
- FR-06 发售状态分类(未知/作者预告/商店待定/已确认/已延期/已发售) + 历史
- FR-09/10 事件分类与跨来源去重(同一事件合并多条证据, 只提醒一次)
- FR-11 提醒偏好(按作品/事件类型/来源), 默认普通 X 帖只进报告
- FR-12 Demo 状态: 本体页入口 / 独立 Demo 页 / 体験版 / 作者口头

存储: graph.json(个人数据, 已被 .gitignore 排除)。
watchlist.csv 仍是拉取主表; 本模块只做"身份与证据", 不抢它的字段。
"""
import hashlib
import json
import os
import re
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
GRAPH_PATH = os.path.join(BASE, "graph.json")
TOKEN_PATH = os.path.join(BASE, "server_token.txt")   # 本地接口配对 token(不进 Git)


def load_or_create_token():
    """本地接口认证 token(FR-17~20 前置收紧)。
    server.py 与 pull.py(嵌进报告页)共用同一文件; 缺失即生成 32 字节 hex。"""
    try:
        t = open(TOKEN_PATH, encoding="utf-8").read().strip()
        if re.fullmatch(r"[0-9a-f]{32,128}", t):
            return t
    except OSError:
        pass
    import secrets
    t = secrets.token_hex(16)
    tmp = TOKEN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(t)
    os.replace(tmp, TOKEN_PATH)
    return t

# ---------- 事件类型 ----------

EVENT_TYPES = ["demo", "release", "delay", "listing", "price", "version", "lang", "x_post", "other"]
EVENT_LABEL = {
    "demo": "Demo", "release": "发售", "delay": "延期", "listing": "上架",
    "price": "定价/降价", "version": "版本更新", "lang": "语言/汉化",
    "x_post": "普通帖", "other": "其他",
}
# 提醒默认值: 高价值事件弹窗, 普通 X 帖只进报告(§7 低噪声)
DEFAULT_ALERTS = {
    "demo": True, "release": True, "delay": True, "listing": True,
    "price": True, "version": True, "lang": True, "x_post": False, "other": False,
    "src_x": True, "src_steam": True, "src_dlsite": True, "mute": True,
}
# 同 key 事件在此窗口内合并(跨来源去重), 超窗视为新一轮事件重新提醒
EVENT_MERGE_WINDOW_DAYS = 14

# ---------- 发售状态 ----------

RELEASE_STATES = ["unknown", "author_post", "store_tbc", "confirmed", "delayed", "released"]
RELEASE_LABEL = {
    "unknown": "未知", "author_post": "作者预告", "store_tbc": "商店待定",
    "confirmed": "已确认", "delayed": "已延期", "released": "已发售",
}


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today():
    return datetime.now().strftime("%Y-%m-%d")


# ---------- 存储 ----------

def load_graph():
    """读 graph.json; 缺失/损坏返回空骨架(损坏时把原文件备份, 不静默丢数据)。"""
    if not os.path.exists(GRAPH_PATH):
        return _blank()
    try:
        with open(GRAPH_PATH, encoding="utf-8") as f:
            g = json.load(f)
        if not isinstance(g, dict):
            raise ValueError("graph root not dict")
    except Exception:
        try:
            bak = GRAPH_PATH + ".corrupt-%s" % datetime.now().strftime("%Y%m%d%H%M%S")
            os.replace(GRAPH_PATH, bak)
        except OSError:
            pass
        return _blank()
    for k, v in _blank().items():
        g.setdefault(k, v)
    return g


def _blank():
    return {"version": 1, "candidates": [], "input_errors": [], "facts": {},
            "events": [], "alerts": {"rows": {}}, "creators": {}}


def save_graph(g):
    """原子写: 先写 .tmp 再 replace, 拉取与本地服务并发时不写坏文件。"""
    tmp = GRAPH_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(g, f, ensure_ascii=False, indent=1)
    os.replace(tmp, GRAPH_PATH)


# ---------- FR-01 输入类型识别 ----------

URL_PATS = [
    ("x_post", re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}/status/\d+")),
    ("x_profile", re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}(?![\w/-])")),
    ("steam", re.compile(r"https?://store\.steampowered\.com/app/\d+")),
    ("dlsite", re.compile(r"https?://www\.dlsite\.com/[a-z]+/work/=/product_id/[A-Za-z]{2}\d+")),
]
TYPE_LABEL = {"x_post": "X帖子", "x_profile": "X主页", "steam": "Steam页", "dlsite": "DLsite页"}


def classify_input(text):
    """识别输入里的链接类型。返回 {types:[...], links:[{url,type}], unknown:[url...]}。
    FR-01: 解析不出来的原因由调用方记录(见 record_input_error)。"""
    if not text:
        return {"types": [], "links": [], "unknown": []}
    types, links, unknown = [], [], []
    seen = set()
    for u in re.findall(r"https?://[^\s,，;；\"'<>()]+", text):
        u = u.rstrip(".,，")
        if u in seen:
            continue
        seen.add(u)
        hit = None
        for t, pat in URL_PATS:
            if pat.search(u):
                hit = t
                break
        if hit:
            if hit not in types:
                types.append(hit)
            links.append({"url": u, "type": hit})
        else:
            unknown.append(u)
    # 裸 handle 输入(如 @someone 或纯 handle)不算链接, 交给调用方
    return {"types": types, "links": links, "unknown": unknown}


def classify_url(url):
    """单 URL 预筛(FR-18 导入预览) -> {supported, type, reason, domain}。
    不支持的链接给出明确原因与"保留后续支持入口", 不伪装成可导入。"""
    u = (url or "").strip()
    out = {"url": u, "supported": False, "type": "invalid",
           "reason": "", "domain": ""}
    if not re.match(r"https?://", u, re.I):
        out["reason"] = "不是 http(s) 链接"
        return out
    mdom = re.match(r"https?://([^/:?#]+)", u, re.I)
    out["domain"] = (mdom.group(1) if mdom else "").lower()
    for t, pat in URL_PATS:
        if pat.search(u):
            out.update({"supported": True, "type": t, "reason": TYPE_LABEL.get(t, t)})
            return out
    d = out["domain"]
    if "dlsite.com" in d:
        out["type"] = "dlsite_unsupported"
        out["reason"] = "DLsite 非作品页(社团页/商品页等)暂不支持, 已保留后续支持入口"
    elif "x.com" in d or "twitter.com" in d:
        out["type"] = "x_invalid"
        out["reason"] = "X 链接需为帖子或主页(带 /status/ 或裸 handle)"
    elif "steampowered.com" in d:
        out["type"] = "steam_invalid"
        out["reason"] = "Steam 链接需为 /app/<数字> 作品页"
    else:
        out["type"] = "unsupported"
        out["reason"] = "暂不支持的站点(%s), 不会导入" % (d or "未知域名")
    return out


def record_input_error(g, inp, reason):
    """FR-01: 解析失败保留原链接与错误原因; 同一输入只记一条(滚动覆盖旧原因)。"""
    inp = (inp or "").strip()
    if not inp:
        return False
    for it in g["input_errors"]:
        if it.get("input") == inp:
            if it.get("reason") != reason:
                it["reason"] = reason
                it["at"] = now_str()
                return True
            return False
    g["input_errors"].append({"input": inp, "reason": reason, "at": now_str()})
    return True


# ---------- FR-02 X 内容证据提取 ----------

def platform_of(url):
    for t, pat in URL_PATS:
        if pat.search(url or ""):
            return t
    return "other"


def extract_x_evidence(post_json=None, user_info=None, body="", src="post"):
    """从 X 帖子/主页资料提取证据。返回:
    {links:[{url,platform,from,retweet}], names:[...], handles:[...]}
    FR-02: 转发(reposted_by)/回复(replying_to)里的链接标 retweet=True,
    后续不得凭它自动认作作者作品(进低置信度候选)。"""
    links, names, handles = [], [], []
    retweet = False
    text = body or ""
    if post_json:
        tweet = post_json.get("tweet") or {}
        if tweet.get("reposted_by") or tweet.get("retweeted_by"):
            retweet = True
        if tweet.get("replying_to"):
            retweet = True  # 回复里的链接同样不代表作者作品
        raw = tweet.get("raw_text")
        if isinstance(raw, dict):
            text = (raw.get("text") or "") + "\n" + text
        elif isinstance(raw, str):
            text = raw + "\n" + text
        if not text:
            tx = tweet.get("text")
            text = (tx if isinstance(tx, str) else (tx or {}).get("text") or "") + "\n" + text
        au = tweet.get("author") or post_json.get("author") or {}
        if au.get("screen_name"):
            handles.append(au["screen_name"])
        if au.get("name"):
            names.append(au["name"])
    if user_info:
        if user_info.get("screen_name"):
            handles.append(user_info["screen_name"])
        if user_info.get("name"):
            names.append(user_info["name"])
        text = (user_info.get("description") or "") + "\n" + text

    seen = set()
    for u in re.findall(r"https?://[^\s,，;；\"'<>()]+", text):
        u = u.rstrip(".,，")
        if u in seen:
            continue
        seen.add(u)
        links.append({"url": u, "platform": platform_of(u), "from": src, "retweet": retweet})
    # 作品名候选: 引号/书名号里的片段
    for m in re.finditer(r"[『「《""]([^『」《""]{2,50})[』」》""]", text):
        names.append(m.group(1).strip())
    # 去重保序
    def uniq(seq):
        out, s = [], set()
        for x in seq:
            if x and x not in s:
                s.add(x)
                out.append(x)
        return out

    def uniq_links(lst):
        out, s = [], set()
        for x in lst:
            k = (x.get("url"), x.get("platform"))
            if x.get("url") and k not in s:
                s.add(k)
                out.append(x)
        return out
    return {"links": uniq_links(links), "names": uniq(names), "handles": uniq(handles)}


# ---------- FR-03 候选 ----------

def _cid(key):
    return "c_" + hashlib.md5(key.encode("utf-8")).hexdigest()[:12]


def add_candidate(g, url, platform, title="", evidence=None, confidence=0.5,
                  reason="", row="", handle="", kind="page"):
    """加入候选(待确认)。同 URL 已 pending 不重复; 已 rejected 不复活; 已 confirmed 跳过。"""
    key = (url or "").strip()
    if not key:
        return None, False
    cid = _cid(key)
    for c in g["candidates"]:
        if c.get("id") == cid:
            if c.get("status") == "rejected":
                return c, False           # 用户拒绝过, 不复活(FR-03)
            if c.get("status") == "confirmed":
                return c, False
            c["evidence"] = evidence or c.get("evidence") or []
            c["confidence"] = max(confidence, float(c.get("confidence") or 0))
            c["reason"] = reason or c.get("reason") or ""
            c["at"] = now_str()
            return c, True
    cand = {"id": cid, "kind": kind, "url": key, "platform": platform, "title": title,
            "evidence": evidence or [], "confidence": float(confidence), "reason": reason,
            "target_row": row, "handle": handle, "status": "pending",
            "created": now_str(), "resolved": ""}
    g["candidates"].append(cand)
    return cand, True


def pending_candidates(g):
    return [c for c in g["candidates"] if c.get("status") == "pending"]


def resolve_candidate(g, cid, action, target=""):
    """action: confirm(挂到 target 行) / reject。返回 (cand|None, msg)。"""
    cand = next((c for c in g["candidates"] if c.get("id") == cid), None)
    if not cand:
        return None, "候选不存在: %s" % cid
    if action == "confirm":
        cand["status"] = "confirmed"
        cand["target_row"] = target or cand.get("target_row") or ""
        cand["resolved"] = now_str()
        return cand, "已确认 -> %s" % (cand["target_row"] or "(新作品)")
    if action == "reject":
        cand["status"] = "rejected"
        cand["resolved"] = now_str()
        return cand, "已拒绝(不再提示)"
    return None, "未知动作: %s" % action


# ---------- FR-05 事实 ----------

FACT_FIELDS = ["release_date", "price", "genres", "demo", "release_status", "language"]
FACT_CAP = 20  # 每字段保留最近 N 条(最新在末尾)


def record_fact(g, row, field, value, src, url="", status="", note=""):
    """记录一条事实(按 field+src 分别跟踪, 不同来源互不顶替)。
    value 归一为 str; 与该来源上一条同值同状态则只刷新时间, 不膨胀历史。
    返回是否"该来源的值发生了变化"。FR-05: 每条带来源与抓取时间。"""
    if not row or not field:
        return False
    row = str(row)
    facts = g.setdefault("facts", {})
    lst = facts.setdefault(row, {}).setdefault(field, [])
    val = "" if value is None else str(value)
    last = None
    for it in reversed(lst):
        if it.get("src") == src:
            last = it
            break
    if last and last.get("v") == val and last.get("status") == status:
        last["at"] = now_str()
        if url and url not in (last.get("url") or ""):
            last["url"] = "%s %s" % (last.get("url"), url).strip()
        if note and note != last.get("note"):
            last["note"] = note
        return False
    lst.append({"v": val, "src": src, "url": url, "status": status,
                "note": note, "at": now_str()})
    if len(lst) > FACT_CAP:
        del lst[:len(lst) - FACT_CAP]
    return True


def latest_fact(g, row, field, src=""):
    """取某字段最新事实; src 给定时只看该来源。"""
    lst = (g.get("facts", {}).get(row) or {}).get(field) or []
    if src:
        for it in reversed(lst):
            if it.get("src") == src:
                return it
        return None
    return lst[-1] if lst else None


def prev_fact(g, row, field, src="", exclude_value=None):
    """取该来源上一条不同的事实(用于变更检测); exclude_value 跳过的值。"""
    lst = (g.get("facts", {}).get(row) or {}).get(field) or []
    for it in reversed(lst):
        if src and it.get("src") != src:
            continue
        if exclude_value is not None and it.get("v") == exclude_value:
            continue
        return it
    return None


def fact_history(g, row, field):
    return list((g.get("facts", {}).get(row) or {}).get(field) or [])


# ---------- FR-06 发售状态 ----------

def classify_release(store_date, coming_soon, prev_date="", author_claimed=False, released_hint=False):
    """按商店原始状态分类。返回 (state, note)。
    store_date: 归一化 YYYY-MM-DD 或空; coming_soon: 商店是否"即将推出"。
    delayed: 有历史日期且新日期晚于旧日期(由调用方把 prev_date 传进来)。"""
    if store_date:
        if prev_date and prev_date < store_date and prev_date >= "1990-01-01":
            return "delayed", "发售日 %s -> %s" % (prev_date, store_date)
        if store_date <= today():
            return "released", store_date
        return "confirmed", store_date
    if coming_soon:
        return "store_tbc", "商店显示即将推出"
    if author_claimed:
        return "author_post", "作者口头预告"
    if released_hint:
        return "released", "作者称已发售(商店未确认)"
    return "unknown", ""


# ---------- FR-09/10 事件 ----------

_ET_PATTERNS = [
    ("demo", r"demo|体験版|試遊版|試玩版|デモ|trial version", re.I),
    ("delay", r"延期|delayed|postponed|后延|遅延", re.I),
    ("lang", r"漢化|汉化|简体|繁体|中国語|翻訳|翻译|localization|localis|language support|语言支持", re.I),
    ("version", r"\bv?\d+\.\d+(\.\d+)?\b|バージョン|version|アップデート|更新版", re.I),
    ("price", r"価格|价格|定价|降价|discount|off%|セール|sale\b|¥\s?\d|\$\s?\d", re.I),
    ("release", r"発売|发售|販売日|release\s|launch|now available|配信開始|上架", re.I),
]


def classify_event_text(text, src=""):
    """从更新文本推断事件类型; 识别不出归普通帖。返回 EVENT_TYPES 之一。"""
    t = text or ""
    for et, pat, flags in _ET_PATTERNS:
        if re.search(pat, t, flags):
            return et
    if src in ("Steam", "DLsite"):
        return "listing"
    return "x_post"


def _event_key(row, etype, norm):
    raw = "%s|%s|%s" % (row, etype, norm)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def _parse_ts(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s or "", fmt)
        except ValueError:
            continue
    return None


def upsert_event(g, row, etype, title, source, norm=""):
    """FR-10 跨来源去重: 同 (作品, 类型, 归一化值) 在 EVENT_MERGE_WINDOW_DAYS 内
    只留一条事件, 证据合并进 sources; 超窗开新一轮(可再次提醒)。
    返回 (event, is_new)。is_new=True 才应该触发提醒。"""
    if etype not in EVENT_TYPES:
        etype = "other"
    key = _event_key(row, etype, norm or title or "")
    now = datetime.now()
    for ev in g["events"]:
        if ev.get("key") != key:
            continue
        # 超窗: 同 key 距上次观察超过合并窗口 -> 新一轮事件
        last = _parse_ts(ev.get("last_at"))
        if last and (now - last).days > EVENT_MERGE_WINDOW_DAYS:
            continue
        ev["last_at"] = now_str()
        ev["count"] = int(ev.get("count") or 1) + 1
        if title and title not in ev.get("title", ""):
            ev["title"] = title or ev.get("title")
        srcs = ev.setdefault("sources", [])
        dup = False
        for s in srcs:
            if s.get("src") == source.get("src") and s.get("url") == source.get("url"):
                s["at"] = now_str()
                dup = True
                break
        if not dup:
            srcs.append({"src": source.get("src", ""), "url": source.get("url", ""),
                         "text": (source.get("text") or "")[:300],
                         "at": now_str()})
            if len(srcs) > 8:
                del srcs[:len(srcs) - 8]
        return ev, False
    ev = {"key": key, "round": now.strftime("%Y%m%d"), "row": row, "etype": etype,
          "title": title or EVENT_LABEL.get(etype, etype),
          "sources": [{"src": source.get("src", ""), "url": source.get("url", ""),
                       "text": (source.get("text") or "")[:300], "at": now_str()}],
          "first_at": now_str(), "last_at": now_str(), "count": 1}
    g["events"].append(ev)
    if len(g["events"]) > 2000:
        del g["events"][:len(g["events"]) - 2000]
    return ev, True


def row_events(g, row, limit=30):
    evs = [e for e in g["events"] if e.get("row") == row]
    evs.sort(key=lambda e: e.get("last_at") or "", reverse=True)
    return evs[:limit]


# ---------- FR-11 提醒偏好 ----------

def alert_prefs(g, row=None, handle=""):
    """合并优先级: 默认 < 全局(global) < 作者(handles) < 作品(rows)。
    row 传作品名, handle 传作者 x_handle(FR-11: 可按作者控制)。"""
    prefs = dict(DEFAULT_ALERTS)
    al = g.get("alerts") or {}
    for scope in ("global", "handles", "rows"):
        src = al.get(scope) or {}
        key = None
        if scope == "global":
            key = "_global_"
        elif scope == "handles":
            key = (handle or "").lower().lstrip("@")
        else:
            key = row
        if key and key in src:
            prefs.update({k: bool(v) for k, v in src[key].items() if k in DEFAULT_ALERTS})
    return prefs


def set_alert(g, row, key, value, scope="row", handle=""):
    """key 必须是 DEFAULT_ALERTS 里的键。
    scope: row=按作品 / author=按作者(handle) / global=全局。"""
    if key not in DEFAULT_ALERTS:
        return False, "未知提醒项: %s" % key
    alerts = g.setdefault("alerts", {"rows": {}})
    if scope == "global":
        alerts.setdefault("global", {}).setdefault("_global_", {})[key] = bool(value)
    elif scope == "author":
        h = (handle or "").lower().lstrip("@")
        if not h:
            return False, "该行没有作者 handle"
        alerts.setdefault("handles", {}).setdefault(h, {})[key] = bool(value)
    else:
        if not row:
            return False, "缺作品名"
        alerts.setdefault("rows", {}).setdefault(row, {})[key] = bool(value)
    return True, "ok"


def should_notify(g, row, etype, src="", handle=""):
    """FR-11: mute(该作品单独静音) + 事件类型开关 + 来源开关都通过才弹窗;
    普通帖默认只进报告(低噪声)。"""
    prefs = alert_prefs(g, row, handle)
    if not prefs.get("mute", True):
        return False
    if not prefs.get(etype, False):
        return False
    s = (src or "").lower()
    if s == "x" and not prefs.get("src_x", True):
        return False
    if s == "steam" and not prefs.get("src_steam", True):
        return False
    if s == "dlsite" and not prefs.get("src_dlsite", True):
        return False
    return True


# ---------- FR-12 Demo 状态 ----------

DEMO_STATES = ["none", "entry", "page", "trial", "claimed"]
DEMO_LABEL = {"none": "无Demo", "entry": "本体页入口", "page": "独立Demo页",
              "trial": "体験版", "claimed": "作者口头"}


def demo_state_from_steam(demos_field, html_has_entry=False):
    """Steam appdetails.demos 字段 -> 状态。
    demos 非空 = 本体页有 Demo 入口且存在独立 Demo 页; 仅 HTML 有入口则 entry。"""
    if demos_field:
        return "page"
    if html_has_entry:
        return "entry"
    return "none"


def demo_state_from_dlsite(has_trial_block):
    return "trial" if has_trial_block else "none"


# ---------- FR-08/04 作者 -> 其他作品 ----------

def works_by_creator(rows, handle):
    """同一作者的全部作品行(FR-04: handle 只是作者标识)。"""
    h = (handle or "").lower().lstrip("@")
    if not h:
        return []
    return [r for r in rows if (r.get("x_handle") or "").lower().lstrip("@") == h]
