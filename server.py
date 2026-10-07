# -*- coding: utf-8 -*-
"""huangyou-watcher 本地服务(仅 127.0.0.1:8790)
让 report.html 里的星星评分 / 取消关注能实时写回 watchlist.csv。
由 pull.py 拉取结束时自动拉起(detached), 也可手动: python server.py
端口被占(已有实例)即退出。写表前检查 .pull.lock, 拉取进行中拒绝修改。

安全(REQUIREMENTS §7 本地接口接入门槛, FR-17~20 前置):
- CORS: 只回显白名单来源(扩展/报告页/回环), 不再 Access-Control-Allow-Origin: *
- 认证: 除 /api/ping 外全部要求 X-Hyw-Token(报告页由 pull.py 内嵌, 扩展粘贴一次)
- /api/llm_config 含 api_key, 同样必须带 token 才返回
"""
import csv
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import io
import urllib.request
import ssl
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE, "watchlist.csv")
UNF_PATH = os.path.join(BASE, "unfollowed.csv")
EVENTS_PATH = os.path.join(BASE, "events.csv")
LOCK_PATH = os.path.join(BASE, ".pull.lock")
LLM_PATH = os.path.join(BASE, "llm.json")          # 模型 API 配置(含 key, 不进 Git)
ADD_LOG = os.path.join(BASE, "add_log.txt")         # 批量添加的后台日志
JOBS_DIR = os.path.join(BASE, "jobs")               # 导入任务逐页结果(FR-19)
PORT = 8790
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

sys.path.insert(0, BASE)
import model as MOD

TOKEN = MOD.load_or_create_token()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def origin_allowed(origin):
    """CORS 白名单: 扩展(chrome/moz-extension)、报告页(file:// -> Origin: null)、回环。
    不在名单 -> 不回显 ACAO, 浏览器侧直接拦截。"""
    if not origin:
        return True   # 同源/直连(curl, 本地脚本)不走 CORS
    o = origin.strip().lower()
    if o in ("null", "file://"):
        return True
    if o.startswith("chrome-extension://") or o.startswith("moz-extension://"):
        return True
    return o in ("http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT)


def token_ok(handler):
    """校验 X-Hyw-Token(头或 ?token=)。用 compare_digest 防时序。"""
    got = handler.headers.get("X-Hyw-Token") or ""
    if not got:
        m = re.search(r"[?&]token=([0-9a-fA-F]+)", handler.path)
        got = m.group(1) if m else ""
    return bool(got) and hmac.compare_digest(got, TOKEN)


def port_busy():
    """已有实例探测: 打真 HTTP(connect_ex 在本沙箱假阴性, 禁用)。"""
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/ping" % PORT)
        with urllib.request.urlopen(req, timeout=1.0) as r:
            return json.loads(r.read()).get("ok") is True
    except Exception:
        return False


def read_rows():
    if not os.path.exists(CSV_PATH):
        return [], []
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        fields = rd.fieldnames or []
        return list(rd), fields


def write_rows(rows, fields):
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def pull_locked():
    """拉取进行中(锁新鲜)则拒绝写。"""
    try:
        import time
        return (time.time() - os.path.getmtime(LOCK_PATH)) < 15 * 60
    except OSError:
        return False


def log_event(game, kind, detail):
    exists = os.path.exists(EVENTS_PATH) and os.path.getsize(EVENTS_PATH) > 0
    with open(EVENTS_PATH, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["time", "game", "kind", "detail"])
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), game, kind, detail])


def _precheck(url, rows):
    """FR-18: 单 URL 预筛 — supported/exists/unsupported/duplicate, 全部本地判断。
    exists 复用 add_game 的查重口径(平台 id 强、handle 仅纯主页)。"""
    cls = MOD.classify_url(url)
    out = {"url": url, "domain": cls["domain"], "status": "", "reason": ""}
    if not cls["supported"]:
        out["status"] = "unsupported"
        out["reason"] = cls["reason"]
        return out
    steam_ids = set(re.findall(r"store\.steampowered\.com/app/(\d+)", url))
    dl_ids = set(re.findall(r"dlsite\.com/[a-z]+/work/=/product_id/([A-Za-z]{2}\d+)", url))
    mh = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?![\w/-])", url)
    handle = mh.group(1) if mh else ""
    for r in rows:
        r_apps = {a.strip() for a in (r.get("steam_appid") or "").split(",") if a.strip()}
        if steam_ids and r_apps.intersection(steam_ids):
            out.update({"status": "exists",
                        "reason": "已存在(Steam): %s" % (r.get("name") or "")})
            return out
        if dl_ids and (r.get("dlsite_id") or "").strip() in dl_ids:
            out.update({"status": "exists",
                        "reason": "已存在(DLsite): %s" % (r.get("name") or "")})
            return out
    if handle and not (steam_ids or dl_ids):
        same = [r for r in rows
                if (r.get("x_handle") or "").lower() == handle.lower()]
        if same:
            out.update({"status": "exists",
                        "reason": "已关注该作者 @%s: %s" % (handle, same[0].get("name"))})
            return out
    out.update({"status": "supported", "reason": cls["reason"]})
    return out


def _job_write(job):
    os.makedirs(JOBS_DIR, exist_ok=True)
    p = os.path.join(JOBS_DIR, "%s.json" % job["id"])
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _prune_jobs(days=7):
    """job 文件最多留 7 天, 防目录膨胀。"""
    import time
    try:
        now = time.time()
        for fn in os.listdir(JOBS_DIR):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(JOBS_DIR, fn)
            if now - os.path.getmtime(p) > days * 86400:
                os.remove(p)
    except OSError:
        pass


class H(BaseHTTPRequestHandler):
    def _cors(self):
        """FR-17~20 收紧: 只回显白名单来源, 不再 '*'。"""
        origin = self.headers.get("Origin")
        if origin and origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Hyw-Token")

    def log_message(self, *a):  # 静默访问日志
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _need_auth(self):
        """除 /api/ping 外全部要求 token; 401 不回显任何数据。"""
        if token_ok(self):
            return False
        self._json({"error": "unauthorized",
                    "hint": "带 X-Hyw-Token 头; token 见 server_token.txt 或报告页页脚"}, 401)
        return True

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/ping"):
            self._json({"ok": True, "port": PORT})
            return
        if self._need_auth():
            return
        if self.path.startswith("/api/llm_config"):
            cfg = {}
            if os.path.exists(LLM_PATH):
                try:
                    with open(LLM_PATH, encoding="utf-8") as f:
                        cfg = json.load(f)
                except Exception:
                    cfg = {}
            self._json({"ok": True, "config": cfg})
        elif self.path.startswith("/api/add_log"):
            tail = ""
            try:
                with open(ADD_LOG, "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 4000))
                    tail = f.read().decode("utf-8", "replace")
            except OSError:
                tail = "(还没有批量添加日志)"
            self._json({"ok": True, "log": tail})
        elif self.path.startswith("/api/import_status"):
            m = re.search(r"[?&]id=([0-9a-f]{8,32})", self.path)
            if not m:
                self._json({"error": "id 必填"}, 400)
                return
            p = os.path.join(JOBS_DIR, "%s.json" % m.group(1))
            try:
                with open(p, encoding="utf-8") as f:
                    self._json(json.load(f))
            except OSError:
                self._json({"error": "任务不存在"}, 404)
        else:
            self._json({"error": "not found"}, 404)

    @staticmethod
    def _llm_chat(cfg, prompt, timeout=40):
        """对 OpenAI 兼容端点发一次最小 chat 请求。base_url 灵活归一。"""
        base = str(cfg.get("base_url") or "").rstrip("/")
        key = str(cfg.get("api_key") or "")
        model = str(cfg.get("model") or "")
        if not base:
            raise RuntimeError("base_url 为空")
        if base.endswith("/chat/completions"):
            urls = [base]
        elif base.endswith("/v1"):
            urls = [base + "/chat/completions"]
        else:
            urls = [base + "/v1/chat/completions", base + "/chat/completions"]
        body = json.dumps({"model": model, "temperature": 0,
                           "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
        extra = cfg.get("headers")
        last = None
        for u in urls:
            headers = {"Content-Type": "application/json",
                       "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                     "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
            if key:
                headers["Authorization"] = "Bearer " + key
            if isinstance(extra, dict):
                for hk, hv in extra.items():
                    headers[str(hk)] = str(hv)
            try:
                req = urllib.request.Request(u, data=body, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                    d = json.loads(r.read())
                content = d["choices"][0]["message"]["content"]
                return str(content), d.get("model", model)
            except Exception as ex:
                last = ex
                continue
        raise RuntimeError("模型调用失败: %r" % last)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._json({"error": "bad json"}, 400)
            return
        if self._need_auth():
            return

        # ---- FR-18 导入预筛: 逐页给 supported/unsupported/exists/duplicate ----
        if self.path.startswith("/api/import_preview"):
            urls = data.get("urls")
            if not isinstance(urls, list) or not urls:
                self._json({"error": "urls 必须是非空数组"}, 400)
                return
            if len(urls) > 200:
                self._json({"error": "一次最多 200 个"}, 400)
                return
            rows, _fields = read_rows()
            seen = set()
            results = []
            for u in urls:
                u = str(u or "").strip()
                if not u:
                    continue
                if u in seen:
                    results.append({"url": u, "status": "duplicate",
                                    "reason": "本批次内重复", "domain": ""})
                    continue
                seen.add(u)
                r = _precheck(u, rows)
                results.append(r)
            self._json({"ok": True, "results": results})
            return

        # ---- FR-19 提交导入: 建 job + 后台逐页处理, 返回 job_id 供轮询 ----
        if self.path.startswith("/api/import"):
            pages = data.get("pages")
            if not isinstance(pages, list) or not pages:
                self._json({"error": "pages 必须是非空数组 [{url,title?}]"}, 400)
                return
            if len(pages) > 100:
                self._json({"error": "一次最多 100 页"}, 400)
                return
            if pull_locked():
                self._json({"error": "拉取进行中, 稍后再导入"}, 409)
                return
            import secrets
            job_id = secrets.token_hex(8)
            clean = []
            seen = set()
            for p in pages:
                if not isinstance(p, dict):
                    continue
                u = str(p.get("url") or "").strip()
                if not u or u in seen:
                    continue
                seen.add(u)
                clean.append({"url": u, "title": str(p.get("title") or "")[:200],
                              "status": "queued", "reason": "", "pending": []})
            if not clean:
                self._json({"error": "没有可导入的 URL"}, 400)
                return
            job = {"id": job_id, "created": MOD.now_str(), "done": False,
                   "pages": clean}
            _job_write(job)
            _prune_jobs()
            try:
                flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                with open(ADD_LOG, "ab") as lf, open(os.devnull, "rb") as dn:
                    subprocess.Popen([sys.executable, "-u",
                                      os.path.join(BASE, "add_game.py"),
                                      "--job", job_id],
                                     cwd=BASE, stdin=dn, stdout=lf, stderr=lf,
                                     creationflags=flags)
                log_event("(import)", "Import", "提交导入任务 %s, %d 页"
                          % (job_id, len(clean)))
                # 明确区分"已受理": 真实结果必须轮询 import_status(验收14)
                self._json({"ok": True, "job_id": job_id, "accepted": len(clean),
                            "state": "accepted"})
            except Exception as ex:
                try:
                    os.remove(os.path.join(JOBS_DIR, "%s.json" % job_id))
                except OSError:
                    pass
                self._json({"error": "启动后台任务失败: %r" % ex}, 500)
            return

        if self.path.startswith("/api/llm_config"):
            # 保存 {base_url, api_key, model, headers?}; 空 base_url=清除配置(回退 DeepSeek)
            cfg = {"base_url": str(data.get("base_url") or "").strip(),
                   "api_key": str(data.get("api_key") or "").strip(),
                   "model": str(data.get("model") or "").strip()}
            if isinstance(data.get("headers"), dict) and data["headers"]:
                cfg["headers"] = {str(k): str(v) for k, v in data["headers"].items()}
            if not cfg["base_url"]:
                if os.path.exists(LLM_PATH):
                    os.remove(LLM_PATH)
                self._json({"ok": True, "cleared": True})
                return
            with open(LLM_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=1)
            log_event("(llm)", "Config", "模型API已保存: %s (%s)" % (cfg["base_url"], cfg["model"]))
            self._json({"ok": True})
            return

        if self.path.startswith("/api/llm_test"):
            cfg = {"base_url": str(data.get("base_url") or "").strip(),
                   "api_key": str(data.get("api_key") or "").strip(),
                   "model": str(data.get("model") or "").strip()}
            if isinstance(data.get("headers"), dict) and data["headers"]:
                cfg["headers"] = {str(k): str(v) for k, v in data["headers"].items()}
            if not cfg["base_url"]:
                self._json({"error": "先填 base_url"}, 400)
                return
            try:
                reply, model = self._llm_chat(cfg, "回复两个字: 在线")
                self._json({"ok": True, "reply": reply[:200], "model": model})
            except Exception as ex:
                self._json({"error": str(ex)}, 502)
            return

        if self.path.startswith("/api/batch_add"):
            text = str(data.get("text") or "")
            if not text.strip():
                self._json({"error": "输入为空"}, 400)
                return
            if pull_locked():
                self._json({"error": "拉取进行中, 稍后再添加"}, 409)
                return
            sys.path.insert(0, BASE)
            from add_game import extract_links
            links = extract_links(text)
            if not links:
                self._json({"error": "没识别出 X/Steam/DLsite 链接"}, 400)
                return
            if len(links) > 100:
                self._json({"error": "一次最多 100 个链接"}, 400)
                return
            # detached 后台跑 add_game(每条即时落盘), 立即返回不阻塞
            try:
                flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                with open(ADD_LOG, "ab") as lf, open(os.devnull, "rb") as dn:
                    subprocess.Popen([sys.executable, "-u",
                                      os.path.join(BASE, "add_game.py")] + links,
                                     cwd=BASE, stdin=dn, stdout=lf, stderr=lf,
                                     creationflags=flags)
                log_event("(batch)", "Add", "批量添加提交 %d 个链接" % len(links))
                self._json({"ok": True, "count": len(links)})
            except Exception as ex:
                self._json({"error": "启动后台添加失败: %r" % ex}, 500)
            return

        if self.path.startswith("/api/stars"):
            game = str(data.get("game") or "")
            try:
                stars = int(data.get("stars"))
            except (TypeError, ValueError):
                self._json({"error": "stars must be 1-5"}, 400)
                return
            if not (1 <= stars <= 5):
                self._json({"error": "stars must be 1-5"}, 400)
                return
            if pull_locked():
                self._json({"error": "拉取进行中, 暂不可改"}, 409)
                return
            rows, fields = read_rows()
            hit = None
            for r in rows:
                if (r.get("name") or "") == game:
                    r["stars"] = str(stars)
                    hit = r
                    break
            if hit is None:
                self._json({"error": "game not found: %s" % game}, 404)
                return
            if "stars" not in fields:
                fields = fields + ["stars"]
            write_rows(rows, fields)
            log_event(game, "Stars", "关注度 -> %d 星" % stars)
            self._json({"ok": True, "stars": stars})
            return

        if self.path.startswith("/api/unfollow"):
            game = str(data.get("game") or "")
            if pull_locked():
                self._json({"error": "拉取进行中, 暂不可改"}, 409)
                return
            rows, fields = read_rows()
            hit = next((r for r in rows if (r.get("name") or "") == game), None)
            if hit is None:
                self._json({"error": "game not found: %s" % game}, 404)
                return
            # 移入 unfollowed.csv 留痕(可反悔), 再从关注表删除
            hist_fields = fields + (["unfollowed_at"] if "unfollowed_at" not in fields else [])
            new_row = dict(hit)
            new_row["unfollowed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 表头校验: 文件不存在/为空/表头不符 -> 重建, 绝不往坏表头里盲追加
            need_rebuild = True
            old_rows = []
            if os.path.exists(UNF_PATH) and os.path.getsize(UNF_PATH) > 0:
                with open(UNF_PATH, encoding="utf-8-sig", newline="") as f:
                    rd = csv.DictReader(f)
                    if rd.fieldnames == hist_fields:
                        need_rebuild = False
                        old_rows = list(rd)
                    else:
                        # 旧表头不同: 按位置抢救已有数据行
                        f.seek(0)
                        raw = list(csv.reader(f))[1:]
                        old_rows = [dict(zip(rd.fieldnames or [], r)) for r in raw]
                        print("[server] unfollowed.csv 表头不符, 重建并抢救 %d 行" % len(old_rows))
            with open(UNF_PATH, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=hist_fields)
                w.writeheader()
                for r in old_rows:
                    w.writerow({k: r.get(k, "") for k in hist_fields})
                w.writerow({k: new_row.get(k, "") for k in hist_fields})
            rows.remove(hit)
            write_rows(rows, fields)
            log_event(game, "Unfollow", "取消关注(已存 unfollowed.csv)")
            self._json({"ok": True})
            return

        # ---- FR-03 候选确认/拒绝(写 graph.json; 确认到已有作品时把平台页挂上去) ----
        if self.path.startswith("/api/candidate"):
            cid = str(data.get("id") or "")
            action = str(data.get("action") or "")
            target = str(data.get("target") or "")
            if not cid or action not in ("confirm", "reject"):
                self._json({"error": "id/action 必填, action=confirm|reject"}, 400)
                return
            if pull_locked():
                self._json({"error": "拉取进行中, 稍后再操作"}, 409)
                return
            g = MOD.load_graph()
            cand, msg = MOD.resolve_candidate(g, cid, action, target)
            if cand is None:
                self._json({"error": msg}, 404)
                return
            appended = ""
            if action == "confirm" and target and target != "(新作品)":
                rows, fields = read_rows()
                hit = next((r for r in rows if (r.get("name") or "") == target), None)
                if hit is None:
                    MOD.resolve_candidate(g, cid, "reject")  # 回滚状态
                    MOD.save_graph(g)
                    self._json({"error": "目标作品不存在: %s" % target}, 404)
                    return
                url = cand.get("url") or ""
                m_st = re.search(r"store\.steampowered\.com/app/(\d+)", url)
                m_dl = re.search(r"dlsite\.com/[a-z]+/work/=/product_id/([A-Za-z]{2}\d+)", url)
                if m_st:
                    apps = [a for a in (hit.get("steam_appid") or "").split(",") if a.strip()]
                    if m_st.group(1) not in apps:
                        apps.append(m_st.group(1))
                    hit["steam_appid"] = ",".join(apps)
                    appended = "Steam %s" % m_st.group(1)
                elif m_dl:
                    hit["dlsite_id"] = m_dl.group(1)
                    appended = "DLsite %s" % m_dl.group(1)
                if appended:
                    write_rows(rows, fields)
                    MOD.record_fact(g, target, "page", appended, "confirm", url=url)
                    log_event(target, "Candidate", "确认候选 %s -> %s" % (url, target))
            elif action == "confirm" and target == "(新作品)":
                # 交给后台 add_game 正常入库(拿封面/分类), 候选先标已确认
                log_event(cand.get("title") or "(候选)", "Candidate",
                          "确认为新作品, 后台添加 %s" % cand.get("url"))
                url = cand.get("url") or ""
                if url:
                    try:
                        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS|NEW_PROCESS_GROUP
                        with open(ADD_LOG, "ab") as lf, open(os.devnull, "rb") as dn:
                            subprocess.Popen([sys.executable, "-u",
                                              os.path.join(BASE, "add_game.py"), url],
                                             cwd=BASE, stdin=dn, stdout=lf, stderr=lf,
                                             creationflags=flags)
                    except Exception as ex:
                        print("[server] 后台添加启动失败: %r" % ex)
            MOD.save_graph(g)
            self._json({"ok": True, "msg": msg, "appended": appended})
            return

        # ---- FR-11 提醒设置(全局事件类型/来源开关) ----
        if self.path.startswith("/api/alerts"):
            items = data.get("items")
            if not isinstance(items, list) or not items:
                self._json({"error": "items 必须是非空数组 [{key,value}]"}, 400)
                return
            g = MOD.load_graph()
            okn, bad = 0, []
            for it in items:
                if not isinstance(it, dict):
                    continue
                good, msg = MOD.set_alert(g, "", str(it.get("key") or ""),
                                          bool(it.get("value")), scope="global")
                if good:
                    okn += 1
                else:
                    bad.append(msg)
            MOD.save_graph(g)
            log_event("(alerts)", "Config", "提醒设置更新 %d 项 %s" % (okn, bad or ""))
            self._json({"ok": True, "saved": okn, "bad": bad})
            return

        # ---- FR-11 单作品静音/恢复 ----
        if self.path.startswith("/api/mute"):
            game = str(data.get("game") or "")
            if not game:
                self._json({"error": "game 必填"}, 400)
                return
            mute = bool(data.get("mute"))
            g = MOD.load_graph()
            good, msg = MOD.set_alert(g, game, "mute", not mute, scope="row")
            if not good:
                self._json({"error": msg}, 400)
                return
            MOD.save_graph(g)
            log_event(game, "Alert", "静音" if mute else "恢复提醒")
            self._json({"ok": True, "muted": mute})
            return

        if self.path.startswith("/api/category"):
            game = str(data.get("game") or "")
            cat = str(data.get("category") or "")
            if pull_locked():
                self._json({"error": "拉取进行中, 暂不可改"}, 409)
                return
            rows, fields = read_rows()
            hit = next((r for r in rows if (r.get("name") or "") == game), None)
            if hit is None:
                self._json({"error": "game not found"}, 404)
                return
            hit["category"] = cat
            if "category" not in fields:
                fields = fields + ["category"]
            write_rows(rows, fields)
            self._json({"ok": True})
            return

        self._json({"error": "not found"}, 404)


def main():
    if port_busy():
        print("server already running on %d" % PORT)
        return 0
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print("huangyou-watcher server on http://127.0.0.1:%d" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
