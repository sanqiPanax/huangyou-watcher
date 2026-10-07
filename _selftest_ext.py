# -*- coding: utf-8 -*-
"""FR-17~20 自测(离线段): URL预筛 / token / 本地服务认证与CORS / 导入job文件协议 /
扩展清单与弹窗静态检查 / 报告页 token 内嵌。"""
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import model as MOD
import server as SRV

checks = []


def ck(name, cond):
    checks.append((name, bool(cond)))


# ===== FR-18 单 URL 预筛 =====
c = MOD.classify_url("https://x.com/foo/status/123")
ck("预筛 X帖子 supported", c["supported"] and c["type"] == "x_post")
ck("预筛 带域名", c["domain"] == "x.com")
c = MOD.classify_url("https://store.steampowered.com/app/123")
ck("预筛 Steam supported", c["supported"] and c["type"] == "steam")
c = MOD.classify_url("https://www.dlsite.com/maniax/work/=/product_id/RJ01234567.html")
ck("预筛 DLsite作品页 supported", c["supported"] and c["type"] == "dlsite")
c = MOD.classify_url("https://www.dlsite.com/maniax/circle/=/maker_id/RG123")
ck("预筛 DLsite社团页不支持+原因", not c["supported"]
   and "暂不支持" in c["reason"] and "保留后续支持入口" in c["reason"])
c = MOD.classify_url("https://example.com/x")
ck("预筛 陌生站点不支持", not c["supported"] and "example.com" in c["reason"])
c = MOD.classify_url("https://store.steampowered.com/app/abc")
ck("预筛 Steam非数字app不支持", not c["supported"] and "作品页" in c["reason"])
c = MOD.classify_url("chrome://extensions")
ck("预筛 非http拒绝", not c["supported"] and c["type"] == "invalid")

# ===== FR-17~20 前置: token =====
t1 = MOD.load_or_create_token()
t2 = MOD.load_or_create_token()
ck("token 生成且稳定", bool(re.fullmatch(r"[0-9a-f]{32}", t1)) and t1 == t2)
ck("server 读同一 token", SRV.TOKEN == t1)

# ===== 本地服务: 来源白名单 + token 校验 =====
ck("CORS 白名单 chrome扩展", SRV.origin_allowed("chrome-extension://abcdef"))
ck("CORS 白名单 file页面(null)", SRV.origin_allowed("null"))
ck("CORS 白名单回环", SRV.origin_allowed("http://127.0.0.1:8790"))
ck("CORS 拒绝任意网站", not SRV.origin_allowed("https://evil.example"))
ck("CORS 拒绝其他端口", not SRV.origin_allowed("http://127.0.0.1:9999"))


class _FakeH:
    def __init__(self, path, headers):
        self.path = path
        self.headers = headers


ck("token_ok 头携带通过", SRV.token_ok(_FakeH("/api/x", {"X-Hyw-Token": t1})))
ck("token_ok query携带通过",
   SRV.token_ok(_FakeH("/api/x?token=%s" % t1, {})))
ck("token_ok 空token拒绝", not SRV.token_ok(_FakeH("/api/x", {})))
ck("token_ok 错token拒绝", not SRV.token_ok(_FakeH("/api/x", {"X-Hyw-Token": "0" * 32})))

# ===== FR-18 服务端预筛(带已关注判断) =====
rows = [{"name": "W", "x_handle": "devx", "steam_appid": "111", "dlsite_id": ""}]
r = SRV._precheck("https://store.steampowered.com/app/111", rows)
ck("预筛 已存在Steam", r["status"] == "exists" and "已存在" in r["reason"])
r = SRV._precheck("https://store.steampowered.com/app/222", rows)
ck("预筛 新Steam可导入", r["status"] == "supported")
r = SRV._precheck("https://x.com/devx", rows)
ck("预筛 已关注作者", r["status"] == "exists" and "已关注" in r["reason"])
r = SRV._precheck("https://x.com/other", rows)
ck("预筛 未关注作者可导入", r["status"] == "supported")
r = SRV._precheck("https://store.steampowered.com/app/111", [])
ck("预筛 空表可导入", r["status"] == "supported")

# ===== FR-19 job 文件协议(add_game.run_job 的输入输出) =====
import add_game
jobs_dir = os.path.join(BASE, "jobs")
os.makedirs(jobs_dir, exist_ok=True)
job_id = "ab" * 8
job = {"id": job_id, "created": MOD.now_str(), "done": False, "pages": [
    {"url": "https://example.com/x", "title": "例", "status": "queued",
     "reason": "", "pending": []},
    {"url": "not-a-url", "title": "坏", "status": "queued", "reason": "", "pending": []},
]}
add_game.job_save(job)
got = add_game.job_load(job_id)
ck("job 保存/读取回环", got["id"] == job_id and len(got["pages"]) == 2)
bad_ok = False
try:
    add_game.job_load("../evil")
except (ValueError, OSError):
    bad_ok = True
ck("job 坏id拒绝", bad_ok)

# 离线跑 run_job: 两个页面都会被 classify_url 判 unsupported, 不碰网络
rc = add_game.run_job(job_id)
done = add_game.job_load(job_id)
ck("run_job 退出码0", rc == 0)
ck("run_job 全部处理完", done["done"] is True)
ck("run_job unsupported+原因",
   all(p["status"] == "unsupported" and p["reason"] for p in done["pages"]))

# ===== 服务端 job 读写辅助 =====
SRV._job_write(done)
p = os.path.join(jobs_dir, "%s.json" % job_id)
ck("server job 写读回环", json.load(open(p, encoding="utf-8"))["id"] == job_id)
SRV._prune_jobs(days=-1)   # 立即过期 -> 清掉
ck("job 清理生效", not os.path.exists(p))

# ===== 扩展: 清单与弹窗静态检查 =====
ext = os.path.join(BASE, "extension")
mf = json.load(open(os.path.join(ext, "manifest.json"), encoding="utf-8"))
ck("扩展 MV3", mf["manifest_version"] == 3)
ck("扩展 权限含tabs(FR-17)", "tabs" in mf["permissions"])
ck("扩展 剪贴板权限(FR-20)", "clipboardWrite" in mf["permissions"])
ck("扩展 仅回环host权限", mf["host_permissions"] == ["http://127.0.0.1:8790/*"])
ck("扩展 无后台常驻(点击才读)", "background" not in mf)
ck("扩展 popup入口", mf["action"]["default_popup"] == "popup.html")

js = open(os.path.join(ext, "popup.js"), encoding="utf-8").read()
ck("FR-17 只查当前窗口选中标签",
   "currentWindow: true" in js and "highlighted: true" in js)
ck("FR-17 无history权限", "history" not in mf.get("permissions", []))
ck("FR-18 预览字段(标题/域名/网址)",
   "p.title" in js and "p.url" in js and "domainOf" in js)
ck("FR-18 可取消单页", "p.removed = true" in js)
ck("FR-18 自动排除重复", "seen.add(url)" in js)
ck("FR-18 不支持页禁勾选", "cb.disabled = !p.supported" in js)
ck("FR-19 提交接口", "'/api/import'" in js or '"/api/import"' in js)
ck("FR-19 轮询逐页结果", "import_status" in js and "pollJob" in js)
ck("FR-19 不把受理当完成", "已受理" in js and "job.done" in js)
ck("FR-19 逐页中文状态齐全",
   all(z in js for z in ("已加入", "已存在", "失败", "不支持")))
ck("FR-20 剪贴板兜底", "clipboard.writeText" in js and "execCommand" in js)
ck("扩展带token头", "X-Hyw-Token" in js)

html = open(os.path.join(ext, "popup.html"), encoding="utf-8").read()
ck("popup 说明只读选中", "已选中" in html and "不扫描浏览历史" in html)
ck("popup 引入js", 'src="popup.js"' in html)

# ===== 报告页: token 内嵌, 不留占位符 =====
import pull
real_report = pull.REPORT_PATH
out_dir = os.path.join(BASE, "_selftest_out")
os.makedirs(out_dir, exist_ok=True)
pull.REPORT_PATH = os.path.join(out_dir, "report_tok.html")
rows2 = [{"name": "W", "x_handle": "devx", "steam_appid": "", "dlsite_id": "",
          "stars": "1", "category": "", "cover": "", "update_count": "0"}]
pull.build_report(rows2, {}, [], {"time": "t", "elapsed": "0s"}, {}, 5)
rep = open(pull.REPORT_PATH, encoding="utf-8").read()
pull.REPORT_PATH = real_report
ck("报告 嵌入真实token", t1 in rep)
ck("报告 无占位符残留", "__TOKEN__" not in rep)
ck("报告 请求带token头", "X-Hyw-Token" in rep)
ck("报告 认证失败提示可读", "本地服务未启动" in rep)

ok = 0
for name, passed in checks:
    print(("PASS " if passed else "FAIL ") + name)
    if passed:
        ok += 1
print("%d/%d" % (ok, len(checks)))
sys.exit(0 if ok == len(checks) else 1)
