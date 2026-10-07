# -*- coding: utf-8 -*-
"""huangyou-watcher AI 分类: 只在添加时给作者打一次类型标签(拉取不分类)。
模型后端优先级: llm.json(页面配置的 OpenAI 兼容 API, 如本地 OpenCode go) > DeepSeek。
输出: 游戏 / 视频 / 插画 / 3D / 卖肉 / 其他 (失败返回 "", 不阻塞主流程)。"""
import json
import os
import ssl
import urllib.request

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

BASE = os.path.dirname(os.path.abspath(__file__))
LLM_PATH = os.path.join(BASE, "llm.json")  # 页面"模型 API"窗口写入, 含 key, 不进 Git

CATS = ["游戏", "视频", "插画", "3D", "卖肉", "其他"]

_SYSTEM = (
    "你是内容分类器。根据作者账号名和帖文/简介内容,判断该创作者的主要类型。"
    "只输出一个JSON,不要任何其他文字。"
    '字段: cat(英文枚举: gamedev/video/art/3d/nsfw/other), '
    'cn(中文,必须是这几个之一: 游戏/视频/插画/3D/卖肉/其他), conf(0-1)。'
    "游戏开发/做游戏的=gamedev; 做视频/剪辑/动画视频=video; "
    "二维插画/画师/立绘/2D原画=art; 3D建模/3D渲染/绑骨/动作/3D场景/VRoid=3d; "
    "以色情内容为卖点的写实或福利图=nsfw; 其余=other。"
    "注意: 3D 优先于 卖肉——如果作者同时做3D和福利内容, 主业是3D建模就归3d。"
)


def _get_key():
    k = os.environ.get("DEEPSEEK_API_KEY")
    if k:
        return k
    # 本机凭据文件(可选): 环境变量优先; 文件不存在则跳过, 不影响 llm.json 通道
    p = os.path.expanduser(r"~\.dsh\.credentials.yaml")
    try:
        import yaml
        d = yaml.safe_load(open(p, encoding="utf-8"))

        def walk(o, path=""):
            if isinstance(o, dict):
                for kk, v in o.items():
                    np = path + "/" + str(kk)
                    if "deepseek" in str(kk).lower() and isinstance(v, str) and v.startswith("sk-"):
                        return v
                    r = walk(v, np)
                    if r:
                        return r
            elif isinstance(o, str) and o.startswith("sk-") and "deepseek" in path.lower():
                return o
            return None
        return walk(d)
    except Exception:
        return None


def _load_llm():
    """读页面配置的 OpenAI 兼容 API; 没配返回 None。"""
    try:
        with open(LLM_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("base_url"):
            return cfg
    except Exception:
        pass
    return None


def _openai_chat(cfg, messages, timeout):
    """调 llm.json 配置的 OpenAI 兼容端点; 支持自定义 header(如 x-opencode-session)。"""
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
    body = json.dumps({"model": model, "temperature": 0, "messages": messages}).encode("utf-8")
    last = None
    for u in urls:
        headers = {"Content-Type": "application/json",
                   "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                 "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
        if key:
            headers["Authorization"] = "Bearer " + key
        extra = cfg.get("headers")
        if isinstance(extra, dict):
            for hk, hv in extra.items():
                headers[str(hk)] = str(hv)
        try:
            req = urllib.request.Request(u, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                d = json.loads(r.read())
            return str(d["choices"][0]["message"]["content"])
        except Exception as ex:
            last = ex
    raise RuntimeError("llm.json 端点调用失败: %r" % last)


def _deepseek_chat(messages, timeout):
    key = _get_key()
    if not key:
        raise RuntimeError("DeepSeek key 缺失")
    body = json.dumps({"model": "deepseek-chat", "temperature": 0,
                       "max_tokens": 4096, "messages": messages}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        d = json.loads(r.read())
    return str(d["choices"][0]["message"]["content"])


def _chat(messages, timeout=60):
    """统一 chat 出口: llm.json 端点优先; 失败回退 DeepSeek(README 承诺的行为)。"""
    cfg = _load_llm()
    if cfg:
        try:
            return _openai_chat(cfg, messages, timeout)
        except Exception:
            pass  # 配置端点挂了 -> 落到 DeepSeek 回退
    return _deepseek_chat(messages, timeout)


def _parse_cat(content):
    """从模型输出解析分类。"""
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`").lstrip("json").strip()
    j = json.loads(content)
    cn = str(j.get("cn") or "").strip()
    if cn in CATS:
        return cn
    cat = str(j.get("cat") or "").strip()
    m = {"gamedev": "游戏", "video": "视频", "art": "插画",
         "3d": "3D", "nsfw": "卖肉", "other": "其他"}
    return m.get(cat, "")


def classify(handle, text, name=""):
    """返回中文分类('' 表示失败)。text=帖文/线索, name=当前表里的名字。"""
    user = "handle: @%s\nname: %s\npost: %s" % (handle or "?", name or "?", (text or "")[:600])
    messages = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user}]
    for _ in range(2):
        try:
            return _parse_cat(_chat(messages))
        except Exception:
            continue
    return ""
