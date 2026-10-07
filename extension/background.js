/* huangyou-watcher 扩展后台(事件驱动, 无常驻轮询)
 *
 * FR-17 边界: 只在用户点击右键菜单项后才读取标签页; 启动时不读任何标签、
 * 不碰浏览历史(history 权限压根不申请)。
 *
 * 右键行为(关键细节):
 *   - 右键点在"多选组"里(highlighted=true) -> 复制整个选中组的网址
 *   - 右键点在某个未选中的后台标签上(highlighted=false) -> 只复制这一个
 *   若不区分这两种情况, 单个后台标签会误复制成"当前活动标签"的网址。
 */

const MENU_ID = "hyw-copy-urls";

chrome.runtime.onInstalled.addListener(() => {
  // removeAll 先清, 避免重复加载扩展时菜单叠加
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_ID,
      title: "仅复制网址(逗号分隔)",
      contexts: ["tab"],
    });
  });
});

chrome.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId !== MENU_ID) return;
  copySelectedTabUrls(info.tabId);
});

function copySelectedTabUrls(tabId) {
  const deliver = (tabs) => {
    const seen = new Set();
    const urls = [];
    for (const t of tabs || []) {
      const u = (t && t.url) || "";
      if (!/^https?:\/\//i.test(u)) continue;   // chrome:// 等不复制
      if (seen.has(u)) continue;
      seen.add(u);
      urls.push(u);
    }
    if (urls.length) copyText(urls.join(","));
  };

  if (typeof tabId !== "number") {
    chrome.tabs.query({ currentWindow: true, highlighted: true }, deliver);
    return;
  }
  chrome.tabs.get(tabId, (tab) => {
    if (chrome.runtime.lastError || !tab) {
      chrome.tabs.query({ currentWindow: true, highlighted: true }, deliver);
      return;
    }
    if (!tab.highlighted) {
      // 右键的是单个未选中标签: 就复制这一个(不要误拿活动标签)
      deliver([tab]);
      return;
    }
    // 右键落在多选组内: 复制整组
    chrome.tabs.query({ windowId: tab.windowId, highlighted: true }, deliver);
  });
}

/* MV3 service worker 没有 DOM / navigator.clipboard,
 * 官方解法是 offscreen 文档(仅 CLIPBOARD 用途)。 */
async function copyText(text) {
  try {
    await chrome.offscreen.createDocument({
      url: "offscreen.html",
      reasons: ["CLIPBOARD"],
      justification: "把选中标签页的网址写入剪贴板",
    });
  } catch (e) {
    // 已存在一个 offscreen 文档(上次点击未关) -> 继续发消息即可
  }
  for (let i = 0; i < 20; i++) {
    try {
      const r = await chrome.runtime.sendMessage({ type: "copy", text });
      if (r && r.ok) break;
    } catch (e) {
      // offscreen 脚本可能还没注册好监听, 稍候重试
    }
    await new Promise((res) => setTimeout(res, 50));
  }
  // 稍后关闭, 让连续右键也有机会复用
  setTimeout(() => {
    try {
      chrome.offscreen.closeDocument();
    } catch (e) {}
  }, 1500);
}
