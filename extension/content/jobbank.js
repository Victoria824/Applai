'use strict';
/* jobbank.js — Job Bank 半自动
 *
 * 在 jobbank.gc.ca 职位详情页右下角浮一个 Applai 面板：
 *  - 如果该职位在 Applai 职位库里，显示标题/分数
 *  - 「标记已投递」：在 Applai 里记为 submitted（用户已在 Job Bank 手动投完）
 *  - 「在 Applai 打开」：跳到 dashboard
 */
(function () {
  if (!/^https:\/\/(www\.)?jobbank\.gc\.ca\/jobsearch\/jobposting\//.test(location.href)) return;

  const PANEL_ID = 'applai-jb-panel';

  function el(tag, style, html) {
    const d = document.createElement(tag);
    d.setAttribute('style', style);
    if (html != null) d.innerHTML = html;
    return d;
  }

  async function getSettings() {
    try {
      const { aap_settings: s } = await chrome.storage.local.get(['aap_settings']);
      return s || {};
    } catch (e) { return {}; }
  }

  async function api(path, method, body) {
    const s = await getSettings();
    const base = String(s.backendUrl || 'https://applai-backend.fly.dev').replace(/\/$/, '');
    const headers = { 'Content-Type': 'application/json' };
    if (s.apiToken) headers['Authorization'] = 'Bearer ' + s.apiToken;
    const r = await fetch(base + path, {
      method: method || 'GET', headers,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  function pageTitle() {
    const h = document.querySelector('h1');
    return (h ? h.textContent : document.title).trim().slice(0, 80);
  }

  async function markApplied(statusEl, btn) {
    btn.disabled = true;
    statusEl.textContent = '正在记录…';
    try {
      const url = location.href.split('#')[0];
      let job = null;
      try {
        job = await api('/api/v1/jobs/lookup?url=' + encodeURIComponent(url));
      } catch (e) { /* 404 → 下面新建 */ }
      if (!job) {
        job = await api('/api/v1/jobs', 'POST', {
          user_id: 'x', url, title: pageTitle(), company: '', location: '',
        });
      }
      const jid = job.id;
      await api('/api/v1/applications', 'POST', {
        user_id: 'x', job_id: jid, status: 'submitted', detail: 'Job Bank 手动投递（插件标记）',
      });
      statusEl.textContent = '✅ 已在 Applai 记录为已投递';
      btn.style.display = 'none';
    } catch (e) {
      statusEl.textContent = '❌ 记录失败：' + (e.message || e) + '（检查插件 Token）';
      btn.disabled = false;
    }
  }

  async function init() {
    if (document.getElementById(PANEL_ID)) return;
    const s = await getSettings();
    if (!s.apiToken) return; // 没连账号就不打扰

    const panel = el('div',
      'position:fixed;right:16px;bottom:16px;z-index:2147483647;background:#fff;' +
      'border:1px solid #e5e7eb;border-radius:14px;box-shadow:0 8px 30px rgba(0,0,0,.18);' +
      'padding:14px 16px;width:280px;font-family:-apple-system,"PingFang SC",sans-serif;font-size:13px;color:#1a1a1a;',
      '');
    const title = el('div', 'font-weight:700;margin-bottom:2px;', '💼 Applai');
    const sub = el('div', 'color:#8c8c8c;font-size:12px;margin-bottom:10px;',
      '在 Job Bank 投完后，点一下同步到 Applai');
    const statusEl = el('div', 'font-size:12px;color:#595959;margin-bottom:8px;', '');
    const btn = el('button',
      'width:100%;padding:9px;border:none;border-radius:999px;background:#1a1a1a;color:#fff;' +
      'font-size:13px;font-weight:600;cursor:pointer;',
      '标记已投递 ✓');
    const openBtn = el('button',
      'width:100%;padding:8px;margin-top:6px;border:1px solid #e5e7eb;border-radius:999px;' +
      'background:#fff;font-size:12px;cursor:pointer;color:#595959;',
      '在 Applai 打开 ↗');
    btn.onclick = () => markApplied(statusEl, btn);
    openBtn.onclick = () => {
      getSettings().then((st) => {
        const base = String(st.backendUrl || 'https://applai-backend.fly.dev').replace(/\/$/, '');
        window.open(base + '/dashboard', '_blank');
      });
    };
    const close = el('button',
      'position:absolute;top:6px;right:10px;border:none;background:none;cursor:pointer;color:#bbb;font-size:16px;', '×');
    close.onclick = () => panel.remove();
    panel.style.position = 'fixed';
    panel.appendChild(close);
    panel.appendChild(title);
    panel.appendChild(sub);
    panel.appendChild(statusEl);
    panel.appendChild(btn);
    panel.appendChild(openBtn);
    panel.id = PANEL_ID;
    document.body.appendChild(panel);

    // 如果已在库里，显示分数
    try {
      const job = await api('/api/v1/jobs/lookup?url=' + encodeURIComponent(location.href.split('#')[0]));
      if (job && job.score) {
        sub.textContent = `Applai 匹配 ${Math.round(job.score)} 分 · 投完点一下同步`;
      }
    } catch (e) { /* 不在库里，保持默认文案 */ }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
