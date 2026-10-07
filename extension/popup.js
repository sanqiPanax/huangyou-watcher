/* 黄油关注 · 导入选中标签页 (REQUIREMENTS FR-17~FR-20)
 * FR-17 只在打开弹窗(=用户点击扩展按钮)后读取当前窗口已选中的标签页
 * FR-18 预览标题/域名/网址/数量, 可取消单页, 自动排除重复与不支持
 * FR-19 一次提交, 轮询逐页结果: 已加入/已存在/待确认/失败(含原因)
 * FR-20 本地服务不可用 -> 一键复制筛选后的网址列表
 */
const API = 'http://127.0.0.1:8790';
let TOKEN = '';
let PAGES = [];          // {url,title,domain,selected,checked,status,reason}
let jobTimer = null;

const $ = id => document.getElementById(id);

/* ---------- 本地兜底分类(FR-18: 服务不可用时也能过滤) ---------- */
const PATS = [
  ['x_post', /^https?:\/\/(?:www\.)?(?:x|twitter)\.com\/[A-Za-z0-9_]{1,15}\/status\/\d+/],
  ['x_profile', /^https?:\/\/(?:www\.)?(?:x|twitter)\.com\/[A-Za-z0-9_]{1,15}(?![\w\/-])/],
  ['steam', /^https?:\/\/store\.steampowered\.com\/app\/\d+/],
  ['dlsite', /^https?:\/\/www\.dlsite\.com\/[a-z]+\/work=|work\/=\/product_id\/[A-Za-z]{2}\d+/i],
];
const TYPE_ZH = {x_post: 'X帖子', x_profile: 'X主页', steam: 'Steam页', dlsite: 'DLsite页'};

function localClassify(url) {
  for (const [t, re] of PATS) if (re.test(url)) return {ok: true, reason: TYPE_ZH[t]};
  let host = '';
  try { host = new URL(url).hostname; } catch (e) { host = ''; }
  if (host.includes('dlsite.com'))
    return {ok: false, reason: 'DLsite 非作品页(社团页等)暂不支持'};
  if (host.includes('steampowered.com'))
    return {ok: false, reason: 'Steam 链接需为 /app/<数字> 作品页'};
  if (host.endsWith('x.com') || host.endsWith('twitter.com'))
    return {ok: false, reason: 'X 链接需为帖子或主页'};
  return {ok: false, reason: '暂不支持的站点(' + host + ')'};
}

function domainOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ''); }
  catch (e) { return ''; }
}

/* ---------- FR-17 读取当前窗口已选中的标签页 ---------- */
function readSelectedTabs() {
  $('list').innerHTML = '<div style="padding:14px;color:#5b6474;">读取中...</div>';
  chrome.tabs.query({currentWindow: true, highlighted: true}, tabs => {
    // highlighted=Ctrl/Shift 选中; 单击的当前页也带 highlighted, 一并列出由用户勾选
    PAGES = [];
    const seen = new Set();
    for (const t of tabs || []) {
      const url = t.url || '';
      if (!/^https?:\/\//i.test(url)) continue;         // chrome:// 等直接过滤
      if (seen.has(url)) continue;                       // FR-18 去重
      seen.add(url);
      const c = localClassify(url);
      PAGES.push({url, title: t.title || url, domain: domainOf(url),
                  supported: c.ok, reason: c.reason,
                  checked: c.ok, removed: false});
    }
    if (!PAGES.length) {
      $('list').innerHTML = '<div style="padding:14px;color:#5b6474;">' +
        '当前窗口没有选中的网页标签页。<br>请在标签栏用 Ctrl/Shift 点选若干网页后重试。</div>';
      $('counts').style.display = 'none';
      $('go').disabled = true;
      return;
    }
    renderList();
    serverPreview();   // 服务可达时用服务端权威预筛覆盖本地判断
  });
}

/* ---------- FR-18 预览列表 ---------- */
function renderList() {
  const box = $('list');
  box.innerHTML = '';
  PAGES.forEach((p, i) => {
    const row = document.createElement('div');
    row.className = 'row';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = p.checked && !p.removed;
    cb.disabled = !p.supported || p.removed;
    cb.addEventListener('change', () => { p.checked = cb.checked; renderCounts(); });
    const info = document.createElement('div');
    info.className = 'info';
    const t = document.createElement('div');
    t.className = 't'; t.textContent = p.title;
    const u = document.createElement('div');
    u.className = 'u'; u.textContent = p.url;
    info.appendChild(t); info.appendChild(u);
    const bd = document.createElement('span');
    bd.className = 'badge ' + (p.removed ? 'b-ex' : p.supported ? 'b-ok' : 'b-no');
    bd.textContent = p.removed ? '已移除' : (p.reason || '');
    const rm = document.createElement('button');
    rm.className = 'rm'; rm.title = '移除此页';
    rm.textContent = '×';
    rm.addEventListener('click', () => { p.removed = true; p.checked = false; renderList(); });
    row.appendChild(cb); row.appendChild(info); row.appendChild(bd); row.appendChild(rm);
    box.appendChild(row);
  });
  renderCounts();
}

function renderCounts() {
  const total = PAGES.length;
  const sel = PAGES.filter(p => p.checked && !p.removed).length;
  const ok = PAGES.filter(p => p.supported && !p.removed).length;
  const no = PAGES.filter(p => !p.supported && !p.removed).length;
  const dup = total - ok - no;
  $('counts').style.display = 'flex';
  $('counts').innerHTML =
    '选中 <b>' + total + '</b> 页 · 可导入 <b>' + ok + '</b> · ' +
    '不支持 <b>' + no + '</b> · 已勾选 <b>' + sel + '</b>';
  $('go').disabled = sel === 0;
}

/* ---------- 服务端权威预筛 (FR-18) ---------- */
function headers() {
  const h = {'Content-Type': 'application/json'};
  if (TOKEN) h['X-Hyw-Token'] = TOKEN;
  return h;
}

function apiPost(path, body) {
  return fetch(API + path, {method: 'POST', headers: headers(),
                            body: JSON.stringify(body)}).then(r =>
    r.json().then(j => { if (!r.ok) { const e = new Error(j.error || ('HTTP ' + r.status)); e.status = r.status; throw e; } return j; }));
}
function apiGet(path) {
  return fetch(API + path, {headers: headers()}).then(r =>
    r.json().then(j => { if (!r.ok) { const e = new Error(j.error || ('HTTP ' + r.status)); e.status = r.status; throw e; } return j; }));
}

function setConn(ok, txt) {
  $('conn').className = 'dot ' + (ok ? 'ok' : 'bad');
  $('conn-txt').textContent = txt;
}
function msg(txt, cls) {
  const m = $('msg'); m.textContent = txt; m.className = 'msg ' + (cls || '');
}

function serverPreview() {
  setConn(false, '检测本地服务...');
  apiGet('/api/ping').then(() => {
    return apiPost('/api/import_preview', {urls: PAGES.map(p => p.url)});
  }).then(j => {
    (j.results || []).forEach(r => {
      const p = PAGES.find(x => x.url === r.url);
      if (!p) return;
      p.supported = (r.status === 'supported' || r.status === 'exists');
      p.status = r.status;              // supported | exists | unsupported | duplicate
      p.reason = r.status === 'exists' ? '已存在' :
                 r.status === 'duplicate' ? '重复' : r.reason;
      if (r.status === 'exists') p.checked = false;    // 自动排除(FR-18)
      if (r.status === 'unsupported') p.checked = false;
      if (r.status === 'duplicate') { p.checked = false; p.removed = true; }
    });
    setConn(true, '本地服务已连接 (127.0.0.1:8790)');
    renderList();
  }).catch(e => {
    if (e.status === 401) {
      setConn(false, 'token 无效或未填');
      $('tk-box').open = true;
      msg('本地服务要求认证: 请填 token(服务目录 server_token.txt)', 'err');
    } else {
      setConn(false, '本地服务未启动');
      msg('连不上本地服务: 先双击服务目录的 拉取.cmd(或 python server.py)。' +
          '勾选仍可用, 可先"复制勾选网址"备用(FR-20)。', 'warn');
    }
    renderList();
  });
}

/* ---------- FR-19 提交 + 轮询逐页结果 ---------- */
function submit() {
  const chosen = PAGES.filter(p => p.checked && !p.removed);
  if (!chosen.length) return;
  $('go').disabled = true;
  msg('提交中...', '');
  apiPost('/api/import', {pages: chosen.map(p => ({url: p.url, title: p.title}))})
    .then(j => {
      // 验收14: accepted 只是受理, 必须轮询逐页真实结果
      msg('已受理 ' + j.accepted + ' 页 (任务 ' + j.job_id + '), 逐页处理中...', 'warn');
      pollJob(j.job_id);
    })
    .catch(e => {
      $('go').disabled = false;
      if (e.status === 401) { $('tk-box').open = true; msg('token 无效, 请填写后重试', 'err'); }
      else if (e.status === 409) msg('拉取进行中, 稍后再导入', 'err');
      else msg('提交失败: ' + e.message, 'err');
    });
}

function pollJob(id) {
  clearInterval(jobTimer);
  let fails = 0;
  jobTimer = setInterval(() => {
    apiGet('/api/import_status?id=' + id).then(job => {
      fails = 0;
      renderResults(job);
      if (job.done) {
        clearInterval(jobTimer);
        $('go').disabled = false;
        const ps = job.pages || [];
        const n = k => ps.filter(p => p.status === k).length;
        const pend = ps.reduce((s, p) => s + (p.pending || []).length, 0);
        msg('完成: 已加入 ' + n('added') + ' · 已存在 ' + n('exists') + ' · ' +
            '待确认 ' + pend + ' · 不支持 ' + n('unsupported') + ' · 失败 ' + n('failed'),
            n('failed') ? 'err' : 'ok');
        if (pend) msg(msg.textContent + '(待确认候选在报告页 ⚑ 面板处理)', 'warn');
      }
    }).catch(() => {
      if (++fails >= 5) { clearInterval(jobTimer); $('go').disabled = false;
        msg('任务状态查询失败(服务可能已退出), 请在报告页核对结果', 'err'); }
    });
  }, 1200);
}

const ZH = {added: '已加入', exists: '已存在', unsupported: '不支持',
            failed: '失败', processing: '处理中', queued: '排队中'};
const CLS = {added: 'b-ok', exists: 'b-ex', unsupported: 'b-no',
             failed: 'b-no', processing: 'b-dup', queued: 'b-dup'};

function renderResults(job) {
  const box = $('res');
  box.style.display = 'block';
  box.innerHTML = '<div class="resnum"><span>逐页结果</span>' +
    '<span>共 <b>' + (job.pages || []).length + '</b> 页</span></div>';
  (job.pages || []).forEach(p => {
    const row = document.createElement('div');
    row.className = 'rr';
    const st = document.createElement('span');
    st.className = 'badge ' + (CLS[p.status] || 'b-ex');
    st.textContent = ZH[p.status] || p.status;
    const t = document.createElement('span');
    t.className = 'rt';
    t.textContent = (p.title || p.url) + (p.reason ? ' — ' + p.reason : '') +
      ((p.pending || []).length ? ' · 待确认候选 ' + p.pending.length + ' 个' : '');
    t.title = p.url + (p.reason ? '\n' + p.reason : '');
    row.appendChild(st); row.appendChild(t);
    box.appendChild(row);
  });
}

/* ---------- FR-20 剪贴板兜底 ---------- */
function copyUrls() {
  const chosen = PAGES.filter(p => p.checked && !p.removed);
  if (!chosen.length) { msg('没有勾选任何页面', 'warn'); return; }
  const text = chosen.map(p => p.url).join('\n');
  navigator.clipboard.writeText(text).then(() => {
    msg('已复制 ' + chosen.length + ' 个网址, 可粘到报告页"批量添加"框', 'ok');
  }).catch(() => {
    // clipboardWrite 权限兜底: 退回 execCommand
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); msg('已复制 ' + chosen.length + ' 个网址', 'ok'); }
    catch (e) { msg('复制失败, 请手动选择', 'err'); }
    ta.remove();
  });
}

/* ---------- token (本地接口认证) ---------- */
function loadToken() {
  chrome.storage.local.get(['hyw_token'], d => {
    TOKEN = d.hyw_token || '';
    $('token').value = TOKEN;
    if (TOKEN) serverPreview();   // 有 token 才做服务端预筛
    else { $('tk-box').open = true;
           msg('首次使用: 请填 token(服务目录 server_token.txt)', 'warn'); }
  });
}

$('tk-save').addEventListener('click', () => {
  TOKEN = ($('token').value || '').trim();
  chrome.storage.local.set({hyw_token: TOKEN}, () => {
    msg(TOKEN ? 'token 已保存' : 'token 已清除', 'ok');
    serverPreview();
  });
});
$('tk-test').addEventListener('click', () => {
  TOKEN = ($('token').value || '').trim();
  serverPreview();
});
$('refresh').addEventListener('click', readSelectedTabs);
$('copy').addEventListener('click', copyUrls);
$('go').addEventListener('click', submit);

/* 启动: 先读本地 token, 再读选中标签页(弹窗打开 = 用户已点击扩展按钮, FR-17) */
loadToken();
readSelectedTabs();
