/* 真机验证扩展加载: 拉起浏览器(临时 profile) -> CDP 读每个扩展上下文的
 * getManifest().name -> 优雅关闭(刷新 Preferences) -> 回读注册记录。
 * 判据用扩展自己的 manifest 名字, 不靠猜扩展 ID。
 * 用法: node _ext_load_check.js <browser.exe> <extension_dir> <port> <prof_dir>
 */
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const BROWSER = process.argv[2];
const EXT_DIR = path.resolve(process.argv[3] || "");
const PORT = parseInt(process.argv[4] || "9341", 10);
const PROF = path.resolve(process.argv[5] || "");
const EXPECT = "黄油";   // 扩展 name = "黄油关注 · 导入选中标签页"

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function targets() {
  const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
  return await r.json();
}

function evaluate(wsUrl, expr) {
  return new Promise((resolve) => {
    let ws;
    try { ws = new WebSocket(wsUrl); } catch (e) { return resolve(null); }
    const t = setTimeout(() => { try { ws.close(); } catch (e) {} resolve(null); }, 4000);
    ws.onopen = () => ws.send(JSON.stringify({
      id: 1, method: "Runtime.evaluate",
      params: { expression: expr, returnByValue: true }
    }));
    ws.onmessage = (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.id !== 1) return;
      clearTimeout(t);
      try { ws.close(); } catch (e) {}
      const v = m.result && m.result.result && m.result.result.value;
      resolve(typeof v === "string" ? v : null);
    };
    ws.onerror = () => { clearTimeout(t); resolve(null); };
  });
}

async function main() {
  // 旧 profile 删不掉(被占用/权限)也要继续 —— Chrome 会复用它
  try {
    fs.rmSync(PROF, { recursive: true, force: true });
  } catch (e) { /* 忽略 */ }
  try {
    fs.mkdirSync(PROF, { recursive: true });
  } catch (e) { /* 已存在 */ }

  const args = ["--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--user-data-dir=" + PROF, "--load-extension=" + EXT_DIR,
    "--remote-debugging-port=" + PORT, "about:blank"];
  const child = spawn(BROWSER, args, { stdio: "ignore" });

  const result = { sw: [], names: [], targetCount: 0, prefsMatch: false, err: "" };
  try {
    let ts = [];
    // 轮询到命中或超时: 扩展 SW 注册比内置扩展晚, 拿到首批就停会漏掉它
    for (let i = 0; i < 60; i++) {
      try { ts = await targets(); } catch (e) { ts = []; }
      result.targetCount = ts.length;
      const extTs = ts.filter(t =>
        (t.type === "service_worker" || t.type === "background_page") &&
        (t.url || "").startsWith("chrome-extension://") &&
        t.webSocketDebuggerUrl);
      for (const t of extTs) {
        if (!result.sw.includes(t.url)) {
          result.sw.push(t.url);
          const n = await evaluate(t.webSocketDebuggerUrl,
            "chrome.runtime && chrome.runtime.getManifest ? chrome.runtime.getManifest().name : ''");
          if (n) { result.names.push(n); }
        }
      }
      result.hit = result.names.some(n => n && n.includes(EXPECT));
      if (result.hit) break;
      await sleep(500);
    }
  } catch (e) {
    result.err = String(e);
  }

  // 优雅关闭 -> Preferences 落盘
  try {
    const v = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json();
    await new Promise((resolve) => {
      let ws; try { ws = new WebSocket(v.webSocketDebuggerUrl); } catch (e) { return resolve(); }
      const t = setTimeout(() => { try { ws.close(); } catch (e) {} resolve(); }, 4000);
      ws.onopen = () => ws.send(JSON.stringify({ id: 1, method: "Browser.close" }));
      ws.onmessage = () => { clearTimeout(t); resolve(); };
      ws.onerror = () => { clearTimeout(t); resolve(); };
    });
  } catch (e) { /* 忽略 */ }
  await sleep(1500);
  try { child.kill(); } catch (e) {}

  // 回读 Preferences 注册记录(需要浏览器写完)
  for (let i = 0; i < 20; i++) {
    const pf = path.join(PROF, "Default", "Preferences");
    if (fs.existsSync(pf)) {
      try {
        const raw = fs.readFileSync(pf, "utf8");
        if (raw.includes("huangyou") || raw.includes("huangyou-watcher")) {
          result.prefsMatch = true; break;
        }
        const j = JSON.parse(raw);
        const st = (j.extensions && j.extensions.settings) || {};
        const ids = Object.keys(st);
        result.registeredIds = ids;
        if (ids.length) { result.prefsMatch = ids.length > 0; }
      } catch (e) {}
    }
    await sleep(500);
  }
  console.log(JSON.stringify(result, null, 1));
  try {
    fs.mkdirSync(path.dirname(process.env.OUTJSON || "."), { recursive: true });
    if (process.env.OUTJSON) fs.writeFileSync(process.env.OUTJSON, JSON.stringify(result, null, 1));
  } catch (e) { /* 落盘失败不影响 stdout */ }
}

main().catch(e => console.log(JSON.stringify({ fatal: String(e) })));
