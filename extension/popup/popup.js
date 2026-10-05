'use strict';
/* popup.js — 插件弹窗逻辑：画像管理、页面检测、填写触发、队列、ntfy 设置 */

const $ = (id) => document.getElementById(id);

function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add('hidden'), 2200);
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

function sendToTab(tabId, msg) {
  return new Promise((resolve) => {
    chrome.tabs.sendMessage(tabId, msg, (resp) => {
      if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
      else resolve(resp || { ok: false, error: 'no response' });
    });
  });
}

const store = {
  get: (keys) => new Promise((r) => chrome.storage.local.get(keys, r)),
  set: (obj) => new Promise((r) => chrome.storage.local.set(obj, r)),
};

/* ---------- 页面检测 ---------- */
async function detectPage() {
  const tab = await getActiveTab();
  if (!tab || !tab.url || !/^https?:/.test(tab.url)) {
    $('page-url').textContent = '非网页（无法注入）';
    $('page-ats').textContent = '—';
    $('page-captcha').textContent = '—';
    $('btn-fill').disabled = true;
    return;
  }
  $('page-url').textContent = tab.url;
  $('page-url').title = tab.url;
  $('btn-fill').disabled = false;

  const det = await sendToTab(tab.id, { type: 'AUTOAPPLY_DETECT' });
  if (det.ok) {
    const { ats, confidence, via } = det.result;
    $('page-ats').innerHTML = `<span class="status-ats">${ats}</span> <span class="hint">置信度 ${confidence}% · ${via}</span>`;
  } else {
    $('page-ats').textContent = '检测失败（刷新页面后重试）';
  }

  const cap = await sendToTab(tab.id, { type: 'AUTOAPPLY_CAPTCHA' });
  if (cap.ok) {
    $('page-captcha').innerHTML = cap.result.present
      ? `<span class="err" style="color:#d97706;font-weight:600">⚠️ 检测到 ${cap.result.type}</span>`
      : '<span style="color:#059669">未检测到</span>';
  }
}

/* ---------- 自动填写 ---------- */
function renderReport(r) {
  const el = $('fill-report');
  el.classList.remove('hidden');
  const f = r.filled.map((x) => `<div class="ok">✓ ${x.label} ← ${x.intent}</div>`).join('');
  const m = r.missing.map((x) => `<div class="err">✗ ${x.label || x.intent}：${x.reason}</div>`).join('');
  const rv = r.needsReview.map((x) => `<div class="warn">? ${x.label || x.intent}：${x.reason}</div>`).join('');
  const cap = r.captcha && r.captcha.present
    ? `<div class="warn">⚠️ 检测到验证码（${r.captcha.type}），已停止自动流程</div>` : '';
  el.innerHTML = `<div><b>填写报告</b>（${r.ats}）</div>${f}${m}${rv}${cap}`
    + (r.autoSubmitted ? `<div class="ok">已自动提交 ✓</div>` : '');
}

async function doFill() {
  const tab = await getActiveTab();
  const { aap_profile: profile } = await store.get(['aap_profile']);
  if (!profile || !profile.email) { toast('请先完成第一步：求职画像设置'); return; }
  const { aap_settings: settings } = await store.get(['aap_settings']);
  $('btn-fill').disabled = true;
  $('btn-fill').textContent = '填写中…';
  try {
    const resp = await sendToTab(tab.id, {
      type: 'AUTOAPPLY_FILL',
      profile,
      options: { autoSubmit: !!(settings && settings.autoSubmit) },
    });
    if (!resp.ok) { toast('填写失败：' + (resp.error || resp.result?.error || '未知')); return; }
    const r = resp.result;
    renderReport(r);
    if (r.captcha && r.captcha.present) {
      await enqueueJob(tab, 'needs_manual', r);
      chrome.runtime.sendMessage({
        type: 'AUTOAPPLY_NOTIFY',
        title: 'AutoApply：需要人工处理验证码',
        message: `${r.jobTitle || '该职位'}\n${tab.url}`,
      });
      toast('检测到验证码，已加入待人工队列并推送提醒');
    } else if (r.autoSubmitted) {
      await enqueueJob(tab, 'submitted', r);
    } else {
      await enqueueJob(tab, 'filled', r);
    }
    renderQueue();
  } finally {
    $('btn-fill').disabled = false;
    $('btn-fill').textContent = '自动填写表单';
  }
}

/* ---------- 队列 ---------- */
async function enqueueJob(tab, status, report) {
  const { aap_queue: queue = [] } = await store.get(['aap_queue']);
  const det = await sendToTab(tab.id, { type: 'AUTOAPPLY_DETECT' }).catch(() => null);
  queue.unshift({
    id: 'job_' + Date.now(),
    url: tab.url,
    title: tab.title || '',
    ats: (det && det.ok && det.result.ats) || (report && report.ats) || 'unknown',
    status,
    createdAt: new Date().toISOString(),
    missing: (report && report.missing) || [],
  });
  await store.set({ aap_queue: queue.slice(0, 200) });
}

const STATUS_LABEL = { queued: '已加入', filled: '已填写', needs_manual: '待人工', submitted: '已提交', failed: '失败' };

async function renderQueue() {
  const { aap_queue: queue = [] } = await store.get(['aap_queue']);
  const el = $('queue-list');
  if (!queue.length) { el.innerHTML = '<p class="hint">暂无任务</p>'; return; }
  el.innerHTML = queue.map((j) => `
    <div class="qitem">
      <div class="t">${escapeHtml(j.title || '未命名职位')}<span class="badge ${j.status}">${STATUS_LABEL[j.status] || j.status}</span></div>
      <div class="meta">${escapeHtml(j.ats)} · ${new Date(j.createdAt).toLocaleString('zh-CN')}</div>
      <a class="open" href="#" data-url="${escapeHtml(j.url)}">打开职位页 →</a>
    </div>`).join('');
  el.querySelectorAll('.open').forEach((a) => {
    a.addEventListener('click', (e) => { e.preventDefault(); chrome.tabs.create({ url: a.dataset.url }); });
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

$('btn-queue-clear').addEventListener('click', async () => {
  const { aap_queue: queue = [] } = await store.get(['aap_queue']);
  await store.set({ aap_queue: queue.filter((j) => j.status === 'needs_manual' || j.status === 'queued') });
  renderQueue();
  toast('已清空已完成任务');
});

/* ---------- 画像：结构化表单 ---------- */
const LIST_KEYS = ['targetTitles', 'industries', 'preferredLocations'];

function splitList(s) {
  return String(s || '').split(/[,，、\n]/).map((x) => x.trim()).filter(Boolean);
}

/* 旧版扁平画像 → 新版结构迁移 */
function normalizeProfile(p) {
  p = p || {};
  const out = { ...p };
  for (const k of LIST_KEYS) {
    if (typeof out[k] === 'string') out[k] = splitList(out[k]);
    if (!Array.isArray(out[k])) out[k] = [];
  }
  if (out.yearsExperience !== undefined && out.yearsExperience !== '') {
    out.yearsExperience = Number(out.yearsExperience) || 0;
  }
  if (out.salaryMin !== undefined && out.salaryMin !== '') out.salaryMin = Number(out.salaryMin) || 0;
  if (out.salaryMax !== undefined && out.salaryMax !== '') out.salaryMax = Number(out.salaryMax) || 0;
  if (!out.salaryCurrency) out.salaryCurrency = 'CAD';
  if (!out.remotePreference) out.remotePreference = 'any';
  out.needsSponsorship = !!out.needsSponsorship;
  return out;
}

function salaryText(p) {
  if (!p.salaryMin && !p.salaryMax) return '';
  const cur = p.salaryCurrency || 'CAD';
  if (p.salaryMin && p.salaryMax) return `${p.salaryMin}–${p.salaryMax} ${cur}`;
  if (p.salaryMin) return `${p.salaryMin}+ ${cur}`;
  return `up to ${p.salaryMax} ${cur}`;
}

function renderProfileView(p) {
  p = normalizeProfile(p);
  if (!p.email) { $('profile-view').textContent = '未配置，点击"编辑"完成第一步设置'; return; }
  const lines = [
    `${p.firstName || ''} ${p.lastName || ''} · ${p.email}`.trim(),
    [p.location, p.phone].filter(Boolean).join(' · '),
    p.targetTitles.length ? '目标：' + p.targetTitles.join(' / ') : '',
    [p.preferredLocations.join(', '), salaryText(p), p.yearsExperience ? p.yearsExperience + '年经验' : ''].filter(Boolean).join(' · '),
    p.resumeFile ? `简历：${p.resumeFile.name}（已上传本机）` : '简历：未上传',
  ].filter(Boolean);
  $('profile-view').textContent = lines.join('\n');
}

function fillProfileForm(p) {
  p = normalizeProfile(p);
  document.querySelectorAll('#profile-form [data-k]').forEach((el) => {
    const k = el.dataset.k;
    if (el.type === 'checkbox') { el.checked = !!p[k]; return; }
    if (el.type === 'number') { el.value = p[k] || ''; return; }
    if (LIST_KEYS.includes(k)) { el.value = (p[k] || []).join(', '); return; }
    el.value = p[k] || '';
  });
  renderResumeStatus(p.resumeFile);
}

function renderResumeStatus(rf) {
  $('resume-status').textContent = rf
    ? `已上传：${rf.name}（${Math.round(rf.size / 1024)} KB，只保存在本机）`
    : '未上传（文件只保存在本机浏览器中）';
}

let pendingResumeFile = null;

$('resume-file').addEventListener('change', (e) => {
  const f = e.target.files[0];
  if (!f) return;
  if (f.size > 4 * 1024 * 1024) { toast('文件超过 4MB，请压缩后上传'); e.target.value = ''; return; }
  const reader = new FileReader();
  reader.onload = () => {
    pendingResumeFile = { name: f.name, size: f.size, dataUrl: reader.result };
    renderResumeStatus(pendingResumeFile);
    toast('简历已就绪，保存画像后生效');
  };
  reader.readAsDataURL(f);
});

function collectProfileForm() {
  const p = {};
  document.querySelectorAll('#profile-form [data-k]').forEach((el) => {
    const k = el.dataset.k;
    if (el.type === 'checkbox') { p[k] = el.checked; return; }
    if (LIST_KEYS.includes(k)) { p[k] = splitList(el.value); return; }
    p[k] = el.value.trim();
  });
  return normalizeProfile(p);
}

function openProfileEditor(profile) {
  pendingResumeFile = null;
  $('resume-file').value = '';
  fillProfileForm(profile);
  $('profile-json').value = JSON.stringify(normalizeProfile(profile), null, 2);
  $('json-advanced').classList.add('hidden');
  $('profile-view').classList.add('hidden');
  $('profile-edit').classList.remove('hidden');
}

function closeProfileEditor() {
  $('profile-edit').classList.add('hidden');
  $('profile-view').classList.remove('hidden');
}

$('btn-profile-toggle').addEventListener('click', async () => {
  const { aap_profile: profile } = await store.get(['aap_profile']);
  openProfileEditor(profile);
});
$('btn-profile-cancel').addEventListener('click', closeProfileEditor);

$('profile-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const p = collectProfileForm();
  if (!p.email) { toast('请填写邮箱'); return; }
  const { aap_profile: old } = await store.get(['aap_profile']);
  if (pendingResumeFile) {
    p.resumeFile = pendingResumeFile;
  } else if (old && old.resumeFile) {
    p.resumeFile = old.resumeFile; // 保留之前上传的
  }
  await store.set({ aap_profile: p });
  renderProfileView(p);
  closeProfileEditor();
  toast('画像已保存 ✓');
});

$('btn-json-toggle').addEventListener('click', () => {
  $('json-advanced').classList.toggle('hidden');
});
$('btn-json-apply').addEventListener('click', async () => {
  try {
    const p = normalizeProfile(JSON.parse($('profile-json').value));
    if (!p.email) { toast('画像缺少 email 字段'); return; }
    await store.set({ aap_profile: p });
    fillProfileForm(p);
    toast('已从 JSON 导入');
  } catch (e) { toast('JSON 格式错误：' + e.message); }
});

/* ---------- 设置 / ntfy ---------- */
$('btn-test-push').addEventListener('click', async () => {
  const topic = $('set-topic').value.trim();
  if (!topic) { toast('请先填写 ntfy 主题'); return; }
  await store.set({ aap_settings: { topic, autoSubmit: $('set-autosubmit').checked } });
  chrome.runtime.sendMessage({
    type: 'AUTOAPPLY_NOTIFY',
    title: 'AutoApply 测试推送',
    message: '如果手机收到这条消息，说明验证码提醒通道正常 ✓',
  });
  toast('测试推送已发送');
});
$('set-topic').addEventListener('change', async () => {
  const { aap_settings: s = {} } = await store.get(['aap_settings']);
  await store.set({ aap_settings: { ...s, topic: $('set-topic').value.trim() } });
});
$('set-autosubmit').addEventListener('change', async () => {
  const { aap_settings: s = {} } = await store.get(['aap_settings']);
  await store.set({ aap_settings: { ...s, autoSubmit: $('set-autosubmit').checked } });
  if ($('set-autosubmit').checked) toast('已开启自动提交（仅无验证码且无缺失字段时）');
});

/* ---------- 事件 ---------- */
$('btn-fill').addEventListener('click', doFill);
$('btn-enqueue').addEventListener('click', async () => {
  const tab = await getActiveTab();
  await enqueueJob(tab, 'queued', null);
  renderQueue();
  toast('已加入队列');
});

/* ---------- 初始化 ---------- */
(async function init() {
  const { aap_profile: profile, aap_settings: settings = {} } = await store.get(['aap_profile', 'aap_settings']);
  renderProfileView(profile);
  $('set-topic').value = settings.topic || '';
  $('set-autosubmit').checked = !!settings.autoSubmit;
  renderQueue();
  detectPage();
})();
