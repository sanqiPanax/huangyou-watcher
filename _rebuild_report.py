# -*- coding: utf-8 -*-
"""用已有数据重建 report.html(不联网): 把本地接口 token 嵌进报告页。"""
import io
import os
import sys
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import pull
import model as MOD

rows = pull.load_rows()
updates = pull.load_updates()
cfg = pull.load_config()
g = MOD.load_graph()
# 沿用上一次报告的拉取时间(文件修改时间), 不伪造新的拉取
mtime = os.path.getmtime(pull.REPORT_PATH) if os.path.exists(pull.REPORT_PATH) else 0
t = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S") if mtime else pull.now_str()
pull.build_report(rows, {}, [], {"time": t, "elapsed": "-"}, updates,
                  cfg["keep_updates"], g)
out = open(pull.REPORT_PATH, encoding="utf-8").read()
tok = MOD.load_or_create_token()
print("report rebuilt: %d bytes" % len(out))
print("token embedded:", tok in out)
print("no placeholder:", "__TOKEN__" not in out)
print("div balance:", out.count("<div"), out.count("</div>"))
sys.exit(0 if (tok in out and "__TOKEN__" not in out
               and out.count("<div") == out.count("</div>")) else 1)
