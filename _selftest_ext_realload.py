# -*- coding: utf-8 -*-
"""扩展真机加载验证: 用 CDP 直读扩展自报的 getManifest().name 做判据。
比"看有没有 background target"可靠 —— 那样会和浏览器内置扩展混淆。

判据: names 里出现扩展清单声明的名字 = 真机加载成功。
已知边界: Chrome 154 的 --load-extension 命令行加载被限制(不报错也不注册),
         但通过 chrome://extensions UI 加载不受影响 —— 本脚本只验扩展本身合法。
"""
import io
import json
import os
import shutil
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = os.path.dirname(os.path.abspath(__file__))
EXT = os.path.join(BASE, "extension")
CHECK = os.path.join(BASE, "_ext_load_check.js")
OUT_DIR = os.path.join(BASE, "_selftest_out")

checks = []


def ck(n, c):
    checks.append((n, bool(c)))
    print(("PASS " if c else "FAIL ") + n)


mf = json.load(open(os.path.join(EXT, "manifest.json"), encoding="utf-8"))
name = mf["name"]
ck("清单名字可读", bool(name) and "黄油" in name)

# 选一个支持 --load-extension 的 Chromium(Edge 实测可用)
candidates = [
    (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "edge"),
    (r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "edge"),
    (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "chrome"),
    (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", "chrome"),
]
browser = next((p for p, _ in candidates if os.path.exists(p)), None)
ck("找到 Chromium 浏览器", bool(browser))
if not browser:
    for n, c in checks:
        print(("PASS " if c else "FAIL ") + n)
    print("0/%d" % len(checks))
    sys.exit(1)
print("   browser:", browser)

os.makedirs(OUT_DIR, exist_ok=True)
out_json = os.path.join(OUT_DIR, "ext_load_result.json")
# 每次唯一 profile: 避开残留进程占用同名目录(EPERM)导致浏览器起不来
import time as _t
prof = os.path.join(OUT_DIR, "ext_load_prof_%d" % int(_t.time()))
if os.path.exists(out_json):
    os.remove(out_json)

env = dict(os.environ, OUTJSON=out_json)
# 端口也唯一: 残留实例会占着旧端口, 让 /json/list 连到别人身上
port = 9400 + int(_t.time()) % 400
r = subprocess.run(["node", CHECK, browser, EXT, str(port), prof],
                   cwd=BASE, capture_output=True, text=True, env=env,
                   encoding="utf-8", errors="replace", timeout=180)
ck("验证脚本执行成功(rc=0)", r.returncode == 0)
if r.returncode != 0:
    print("   stderr:", (r.stderr or "")[:600])

res = {}
stdout_raw = (r.stdout or "").strip()
if stdout_raw:
    print("   stdout head:", stdout_raw[:200].replace("\n", " | "))
    try:
        res = json.loads(stdout_raw)
    except Exception as ex:
        # 也许有非 JSON 前后缀, 截取首个 { 到末个 }
        s, e = stdout_raw.find("{"), stdout_raw.rfind("}")
        if s >= 0 and e > s:
            try:
                res = json.loads(stdout_raw[s:e + 1])
            except Exception:
                print("   JSON 解析失败:", repr(ex))
if not res and os.path.exists(out_json):
    try:
        res = json.load(open(out_json, encoding="utf-8"))
    except Exception:
        pass

ck("CDP 拿到扩展 target", res.get("targetCount", 0) > 0)
ck("扩展自报名字 == 清单名字(真机加载成功)",
   name in (res.get("names") or []))
print("   names seen:", res.get("names"))

ok = sum(1 for _, p in checks if p)
print("%d/%d" % (ok, len(checks)))

# 清理
for p in (prof,):
    try:
        shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass

sys.exit(0 if ok == len(checks) else 1)
