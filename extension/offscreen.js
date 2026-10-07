/* offscreen 文档: MV3 service worker 无 DOM, 剪贴板写入放在这里。
 * background.js 通过 runtime.sendMessage 送文本过来。
 * 权限已申请 clipboardWrite, 且 offscreen 是用户点击右键后才创建(非驻留)。 */

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== "copy") return;
  const text = String(msg.text || "");
  if (!text) { sendResponse({ok: false, error: "empty"}); return; }

  const done = (ok, error) => sendResponse({ok: !!ok, error: error || ""});

  // 优先 navigator.clipboard; file:///非聚焦场景回退 execCommand
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text)
      .then(() => done(true))
      .catch(() => fallback());
  } else {
    fallback();
  }

  function fallback() {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      done(ok, ok ? "" : "execCommand failed");
    } catch (e) {
      done(false, String(e));
    }
  }
  return true;   // 异步 sendResponse
});
