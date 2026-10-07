# -*- coding: utf-8 -*-
"""拖拽导入自测: 从 pull.READ_JS 抽出 urlsFromDrop, 用 node 跑真实 JS 断言。
覆盖浏览器拖拽的三种数据格式(text/uri-list / text/plain / text/html)。"""
import io
import json
import os
import re
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import pull

checks = []


def ck(name, cond):
    checks.append((name, bool(cond)))


# ---- 从 READ_JS 抽出被测函数(保证测的是真正进报告的那段代码) ----
m = re.search(r"(function urlsFromDrop\(dt\)\{.*?\n  \})", pull.READ_JS, re.S)
ck("从 READ_JS 抽出 urlsFromDrop", bool(m))
if not m:
    for n, c in checks:
        print(("PASS " if c else "FAIL ") + n)
    print("0/%d" % len(checks))
    sys.exit(1)

fn_src = m.group(1)
# READ_JS 是非 raw 字符串, Python 已把 \\r\\n 还原成 \r\n; 这里直接用即可

TEST_JS = fn_src + r"""

var CASES = [
  ["uri-list 3个+注释行", {"text/uri-list": "# c\r\nhttps://a.com/1\r\nhttps://b.com/2\r\nhttps://c.com/3"},
   ["https://a.com/1", "https://b.com/2", "https://c.com/3"]],
  ["uri-list 尾斜杠保留", {"text/uri-list": "https://dlsite.com/x/"},
   ["https://dlsite.com/x/"]],
  ["plain 空格分隔", {"text/plain": "看 https://e.com/1 和 https://f.com/2 好玩"},
   ["https://e.com/1", "https://f.com/2"]],
  ["plain 逗号分隔", {"text/plain": "https://g.com/1,https://h.com/2"},
   ["https://g.com/1", "https://h.com/2"]],
  ["plain 尾部标点", {"text/plain": "(https://i.com/1.)"},
   ["https://i.com/1"]],
  ["html 抽 href", {"text/html": "<a href=\"https://j.com/1\">t</a> and <a href='https://k.com/2'>u</a>"},
   ["https://j.com/1", "https://k.com/2"]],
  ["三种格式混合去重", {"text/uri-list": "https://m.com/1", "text/plain": "https://m.com/1 https://n.com/2",
                 "text/html": "<a href=\"https://m.com/1\">x</a>"},
   ["https://m.com/1", "https://n.com/2"]],
  ["空数据", {}, []],
  ["只有标题无URL", {"text/plain": "只是一个标题 没有链接"}, []],
  ["非http协议过滤", {"text/plain": "ftp://x.com/a chrome://settings javascript:alert(1)"}, []],
  ["http保留https优先", {"text/plain": "http://o.com/1 https://p.com/2"}, ["http://o.com/1", "https://p.com/2"]],
  ["重复URL去重", {"text/plain": "https://q.com/1 https://q.com/1 https://q.com/1"}, ["https://q.com/1"]],
];

function fakeDT(data){
  return {getData: function(k){ return data[k] || ""; }};
}

var fails = [];
CASES.forEach(function(c){
  var got = urlsFromDrop(fakeDT(c[1]));
  var want = c[2];
  var ok = got.length === want.length && got.every(function(v,i){ return v === want[i]; });
  if(!ok) fails.push(c[0] + " => got " + JSON.stringify(got) + " want " + JSON.stringify(want));
});
console.log(JSON.stringify({total: CASES.length, failed: fails}));
"""

js_path = os.path.join(BASE, "_selftest_drop_run.js")
with open(js_path, "w", encoding="utf-8") as f:
    f.write(TEST_JS)

try:
    r = subprocess.run(["node", js_path], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    ck("node 执行成功(rc=0)", r.returncode == 0)
    if r.returncode != 0:
        print("node stderr:", (r.stderr or "")[:800])
        res = {"total": 0, "failed": ["node 挂了"]}
    else:
        res = json.loads(r.stdout.strip().splitlines()[-1])
finally:
    try:
        os.remove(js_path)
    except OSError:
        pass

ck("全部用例数 %d" % res.get("total", 0), res.get("total", 0) >= 12)
ck("0 用例失败", not res.get("failed"))
for f in res.get("failed") or []:
    print("  FAIL detail:", f)

# ---- 报告产物: 拖拽逻辑真的进了 report.html ----
rep = os.path.join(BASE, "report.html")
if os.path.exists(rep):
    h = open(rep, encoding="utf-8").read()
    ck("report 含 urlsFromDrop", "urlsFromDrop" in h)
    ck("report 含 drop 监听", "addEventListener('drop'" in h)
    ck("report 含拖放提示文案", "拖进这个框" in h)
    ck("report 含 dropping 样式", "textarea.dropping" in h)
    ck("report textarea 与面板都接线",
       h.count("wireDrop(") >= 2)
else:
    ck("report.html 存在(先跑 _rebuild_report)", False)

ok = 0
for name, passed in checks:
    print(("PASS " if passed else "FAIL ") + name)
    if passed:
        ok += 1
print("%d/%d" % (ok, len(checks)))
sys.exit(0 if ok == len(checks) else 1)
