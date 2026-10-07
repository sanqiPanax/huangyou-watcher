# -*- coding: utf-8 -*-
"""FR-17~20 联网段自测: 真实 HTTP 打本地服务 — 401/CORS/预筛/导入job全链路。"""
import io
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import model as MOD

API = "http://127.0.0.1:8790"
TOKEN = MOD.load_or_create_token()
checks = []


def ck(n, c):
    checks.append((n, bool(c)))
    print(("PASS " if c else "FAIL ") + n)


def req(method, path, body=None, token=None, origin=None, raw=False):
    h = {}
    if token:
        h["X-Hyw-Token"] = token
    if origin:
        h["Origin"] = origin
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data:
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(API + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


# 1. ping 无需 token
s, _, b = req("GET", "/api/ping")
ck("ping 免认证存活", s == 200 and json.loads(b).get("ok"))

# 2. 无 token 401 且不泄漏数据
s, _, b = req("GET", "/api/llm_config")
j = json.loads(b)
ck("llm_config 无token 401", s == 401)
ck("401 不回显配置", "config" not in j)
s, _, b = req("POST", "/api/stars", {"game": "x", "stars": 3})
ck("写接口无token 401", s == 401)

# 3. 带 token 通过
s, _, b = req("GET", "/api/llm_config", token=TOKEN)
ck("llm_config 带token 200", s == 200 and "config" in json.loads(b))
s, _, b = req("GET", "/api/import_status?id=deadbeefdeadbeef", token=TOKEN)
ck("不存在job 404", s == 404)

# 4. CORS: 白名单回显, 外站不回显
s, hd, _ = req("GET", "/api/ping", origin="chrome-extension://abcdef")
ck("CORS 扩展来源回显",
   hd.get("Access-Control-Allow-Origin") == "chrome-extension://abcdef")
s, hd, _ = req("GET", "/api/ping", origin="https://evil.example")
ck("CORS 外站不回显",
   "Access-Control-Allow-Origin" not in hd)
s, hd, _ = req("GET", "/api/ping", origin="null")
ck("CORS 报告页(null)回显", hd.get("Access-Control-Allow-Origin") == "null")

# 预检
s, hd, _ = req("OPTIONS", "/api/import", origin="chrome-extension://abcdef")
ck("OPTIONS 预检 204", s == 204)
ck("预检允许token头",
   "X-Hyw-Token" in (hd.get("Access-Control-Allow-Headers") or ""))

# 5. FR-18 预筛
s, _, b = req("POST", "/api/import_preview", {
    "urls": ["https://example.com/x",
             "https://store.steampowered.com/app/5141500",
             "https://example.com/x"]}, token=TOKEN)
res = json.loads(b).get("results") or []
ck("预筛 返回3条", len(res) == 3)
ck("预筛 陌生站点unsupported+原因",
   res[0]["status"] == "unsupported" and res[0]["reason"])
ck("预筛 已存在Steam(exists)", res[1]["status"] == "exists"
   and "已存在" in res[1]["reason"])
ck("预筛 批内重复duplicate", res[2]["status"] == "duplicate")
s, _, _ = req("POST", "/api/import_preview", {"urls": ["x"]})
# 无 token -> 401
ck("预筛 无token 401", s == 401)

# 6. FR-19 提交 + 轮询逐页结果(全用 unsupported/exists 页面, 不碰外网)
s, _, b = req("POST", "/api/import", {"pages": [
    {"url": "https://example.com/a", "title": "例页"},
    {"url": "https://store.steampowered.com/app/5141500", "title": "已存在页"},
    {"url": "https://www.dlsite.com/maniax/circle/=/maker_id/RG1", "title": "社团页"},
]}, token=TOKEN)
j = json.loads(b)
ck("导入受理返回job_id", s == 200 and j.get("job_id"))
job_id = j.get("job_id", "")
ck("受理态明确=accepted", j.get("state") == "accepted" and j.get("accepted") == 3)

done = None
for _ in range(60):
    time.sleep(1)
    s, _, b = req("GET", "/api/import_status?id=" + job_id, token=TOKEN)
    if s != 200:
        break
    done = json.loads(b)
    if done.get("done"):
        break
ck("job 最终done", bool(done and done.get("done")))
pages = (done or {}).get("pages") or []
byst = {p["url"]: p for p in pages}
ck("逐页 unsupported带原因",
   byst.get("https://example.com/a", {}).get("status") == "unsupported"
   and byst.get("https://example.com/a", {}).get("reason"))
ck("逐页 exists带原因",
   byst.get("https://store.steampowered.com/app/5141500", {}).get("status") == "exists"
   and "已存在" in byst.get("https://store.steampowered.com/app/5141500", {}).get("reason", ""))
ck("逐页 社团页不支持",
   byst.get("https://www.dlsite.com/maniax/circle/=/maker_id/RG1", {}).get("status")
   == "unsupported")
ck("无页面漏处理", all(p.get("status") not in ("queued", "processing") for p in pages))

# 7. 锁拒绝
open(os.path.join(BASE, ".pull.lock"), "w").write("99999")
try:
    s, _, _ = req("POST", "/api/import", {"pages": [{"url": "https://example.com/z"}]},
                  token=TOKEN)
    ck("拉取锁 409", s == 409)
finally:
    try:
        os.remove(os.path.join(BASE, ".pull.lock"))
    except OSError:
        pass

# 8. 扩展完整流程模拟: ping -> 预筛 -> 提交 -> 轮询 (与 popup.js 同序)
s, _, _ = req("GET", "/api/ping", origin="chrome-extension://testid")
ck("扩展流程 ping", s == 200)
s, _, b = req("POST", "/api/import_preview",
              {"urls": ["https://example.com/only"]}, token=TOKEN,
              origin="chrome-extension://testid")
ck("扩展流程 预筛", s == 200 and json.loads(b)["results"][0]["status"] == "unsupported")

ok = sum(1 for _, p in checks if p)
print("%d/%d" % (ok, len(checks)))
sys.exit(0 if ok == len(checks) else 1)
