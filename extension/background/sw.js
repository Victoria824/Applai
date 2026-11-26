'use strict';
/* sw.js — 后台：队列存储 + ntfy 推送 + 每日 10 点自动投递执行器 */

const DEFAULTS = {
  topic: '', autoSubmit: false,
  backendUrl: 'https://applai-backend.fly.dev',
  scheduleEnabled: false, scheduleTime: '10:00', dailyCount: 10,
};

// One-time migration: installs that saved the old local-backend default before
// the Fly.io deployment automatically switch to the production URL.
const OLD_LOCAL_BACKEND = 'http://127.0.0.1:8000';

async function getSettings() {
  const { aap_settings: s = {} } = await chrome.storage.local.get(['aap_settings']);
  const merged = { ...DEFAULTS, ...s };
  const saved = String(merged.backendUrl || '').replace(/\/$/, '');
  if (saved === OLD_LOCAL_BACKEND) {
    merged.backendUrl = DEFAULTS.backendUrl;
    await chrome.storage.local.set({ aap_settings: { ...s, backendUrl: merged.backendUrl } });
  }
  return merged;
}

async function getUserId() {
  let { aap_user_id: uid } = await chrome.storage.local.get(['aap_user_id']);
  if (!uid) {
    uid = 'u_' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
    await chrome.storage.local.set({ aap_user_id: uid });
  }
  return uid;
}

function apiUrl(path) {
  return getSettings().then((s) => s.backendUrl.replace(/\/$/, '') + path);
}

async function authHeaders() {
  const s = await getSettings();
  const t = (s.apiToken || '').trim();
  return t ? { 'Authorization': 'Bearer ' + t } : {};
}

async function apiGet(path) {
  const url = await apiUrl(path);
  const r = await fetch(url, { headers: await authHeaders() });
  if (!r.ok) throw new Error(`GET ${path} -> ${r.status}`);
  return r.json();
}

async function apiPost(path, body) {
  const url = await apiUrl(path);
  const r = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...(await authHeaders()) },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`POST ${path} -> ${r.status}`);
  return r.json();
}

/* ---------- ntfy 手机推送 ---------- */
async function pushViaNtfy(title, message) {
  const s = await getSettings();
  const topic = (s.topic || '').trim();
  if (!topic) {
    try {
      await chrome.notifications.create({
        type: 'basic', iconUrl: 'icons/icon48.png',
        title: 'Applai：未配置 ntfy 主题', message: '请在插件设置中填写 ntfy 主题以启用手机推送',
      });
    } catch (e) { /* ignore */ }
    return { ok: false, error: 'ntfy topic not configured' };
  }
  try {
    const resp = await fetch(`https://ntfy.sh/${encodeURIComponent(topic)}`, {
      method: 'POST',
      headers: { Title: title, Priority: 'high', Tags: 'warning,briefcase' },
      body: message,
    });
    return { ok: resp.ok, status: resp.status };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}

async function desktopNotify(title, message) {
  try {
    await chrome.notifications.create({
      type: 'basic', iconUrl: 'icons/icon48.png', title, message: String(message).slice(0, 200),
    });
  } catch (e) { /* ignore */ }
}

/* ---------- 定时：每天 10 点 ---------- */
function nextRunMs(timeStr) {
  const [h, m] = timeStr.split(':').map(Number);
  const now = new Date();
  const next = new Date(now);
  next.setHours(h || 8, m || 0, 0, 0);
  if (next <= now) next.setDate(next.getDate() + 1);
  return next.getTime();
}

async function scheduleDaily() {
  await chrome.alarms.clear('daily-apply');
  const s = await getSettings();
  if (!s.scheduleEnabled) return;
  await chrome.alarms.create('daily-apply', {
    when: nextRunMs(s.scheduleTime),
    periodInMinutes: 24 * 60,
  });
}

/* 定时配置以网页端为准：启动/每小时从后端同步一次，本地做兜底 */
async function syncScheduleFromBackend() {
  try {
    const data = await apiGet('/api/v1/settings');
    const s = await getSettings();
    const mapped = {
      scheduleEnabled: !!data.schedule_enabled,
      scheduleTime: data.schedule_time || s.scheduleTime,
      dailyCount: data.daily_count || s.dailyCount,
    };
    await chrome.storage.local.set({ aap_settings: { ...s, ...mapped } });
    await scheduleDaily();
  } catch (e) { /* 后端不可达时沿用本地配置 */ }
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'daily-apply') runDailyQueue('schedule');
});

/* ---------- 执行器 ---------- */
function sendToTab(tabId, msg, timeoutMs) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve({ ok: false, error: 'timeout' }), timeoutMs || 60000);
    chrome.tabs.sendMessage(tabId, msg, (resp) => {
      clearTimeout(timer);
      if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
      else resolve(resp || { ok: false, error: 'no response' });
    });
  });
}

function waitForTabLoad(tabId, timeoutMs) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => { chrome.tabs.onUpdated.removeListener(listener); resolve(false); }, timeoutMs || 30000);
    function listener(id, info) {
      if (id === tabId && info.status === 'complete') {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve(true);
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const rand = (a, b) => a + Math.random() * (b - a);

// 快照不存的个人标识字段
const SNAPSHOT_PII = new Set(['firstName', 'lastName', 'email', 'phone', 'address', 'location',
  'linkedin', 'github', 'website', 'dob', 'ssn', 'sin', 'passport', 'name']);
const SNAPSHOT_SALARY_KEYS = new Set(['salaryMin', 'salaryMax', 'salaryCurrency', 'salaryPeriod']);

async function sha256hex(str) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(str));
  return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, '0')).join('');
}

function displayResumeName(profile, fallback) {
  // 投递时简历必须用专业文件名（招聘方可见）：First_Last_Resume.pdf
  const clean = (v) => {
    let s = String(v || '').replace(/[^\p{L}\p{N}\s-]/gu, '').trim();
    if (s.length > 1 && s === s.toUpperCase() && /\p{L}/u.test(s)) s = s.toLowerCase().replace(/(^|\s)(\p{L})/gu, (m, a, b) => a + b.toUpperCase());
    return s.replace(/\s+/g, '_');
  };
  const p = profile || {};
  const name = [clean(p.firstName), clean(p.lastName)].filter(Boolean).join('_');
  const ext = ((String(fallback || '').match(/\.[A-Za-z0-9]+$/) || ['.pdf'])[0]).toLowerCase();
  if (name) return name + '_Resume' + ext;
  if (fallback && !/^tailored-\d+\./i.test(fallback)) return fallback;
  return 'Resume' + ext;
}

const INTENT_ZH = { sponsorship: '签证担保', resume: '简历文件', coverLetter: '求职信',
  workAuth: '工作许可', salary: '期望薪资', location: '所在地', linkedin: 'LinkedIn',
  github: 'GitHub', website: '个人网站', phone: '电话', email: '邮箱', startDate: '可入职日期',
  yearsExperience: '工作年限', education: '学历', gender: '性别', veteran: '退伍军人身份',
  disability: '残障状况', citizenship: '公民身份' };
const intentLbl = (i) => INTENT_ZH[i] ? `${INTENT_ZH[i]}(${i})` : i;

async function getResumeForJob(jobId, profile) {
  // 问后端该用哪版简历（画像开了改写 → 改写版 PDF；否则母版）
  try {
    const r = await apiGet(`/api/v1/resume/for-job?job_id=${jobId}`);
    if (r.tailored && r.pdf_hash) {
      const blob = await apiGet(`/api/v1/resume-blob/${r.pdf_hash}`);
      return { name: r.filename, dataUrl: blob.data_url, versionId: r.version_id,
               tailored: true, hash: r.pdf_hash };
    }
    // 母版：后端 for-job 直接回了 data_url（画像里简历的正文），优先用它——
    // 插件侧 profile.resumeFile 可能来自旧缓存，不一定有正文。
    if (r.data_url) {
      const rf = (profile && profile.resumeFile) || {};
      return { name: r.filename || rf.name || "", dataUrl: r.data_url, versionId: 0, tailored: false };
    }
  } catch (e) { /* 降级用母版 */ }
  const rf = (profile && profile.resumeFile) || {};
  return { name: rf.name || "", dataUrl: rf.dataUrl || "", versionId: 0, tailored: false };
}

async function resolveJobCountries(job) {
  // Issue 3：职位国家解析。队列自带的直接用；没有时问后端（先确定性直认，
  // 认不出再调 LLM，结果存回职位）；同一地点串只问一次（本地缓存）。
  // 任何失败都返回 []——填表侧再就地解析地点串，解析失败绝不能中断跑批。
  try {
    if (job.countries && job.countries.length) return job.countries;
    const loc = job.location || '';
    const key = 'countries:' + loc;
    const cached = (await chrome.storage.local.get([key]))[key];
    if (Array.isArray(cached)) return cached;
    const r = await apiPost('/api/v1/location/countries', { job_id: job.id, location: loc });
    const list = (r && Array.isArray(r.countries)) ? r.countries : [];
    await chrome.storage.local.set({ [key]: list });
    return list;
  } catch (e) { return []; }
}

async function fetchBackendProfile() {
  // 控制台是画像的数据源；本地 aap_profile 只是缓存，新装/重装插件后可能从未写过
  try {
    const r = await apiGet('/api/v1/profile');
    const p = (r && r.profile) || null;
    if (p && (p.email || p.firstName || p.resumeFile)) {
      await chrome.storage.local.set({ aap_profile: p });
      return p;
    }
  } catch (e) { /* 后端不可达时退回本地缓存 */ }
  return null;
}

function _urlToken(u) {
  const m = String(u || '').match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b\d{4,}\b/i);
  return m ? m[0].toLowerCase() : '';
}
function _sameOrigin(a, b) {
  try { return new URL(a).origin === new URL(b).origin; } catch (e) { return false; }
}
async function findManualJob(pageUrl) {
  // 用户打开的页面是不是一个「待人工 / 已填待提交」的职位？
  try {
    const d = await apiGet('/api/v1/applications?limit=100');
    const list = (d && d.applications) || [];
    const tok = _urlToken(pageUrl);
    for (const a of list) {
      if (!a || !a.url) continue;
      if (a.status !== 'needs_manual' && a.status !== 'filled') continue;
      if (a.url === pageUrl) return a;
      if (tok && _sameOrigin(a.url, pageUrl) && a.url.toLowerCase().includes(tok)) return a;
    }
  } catch (e) { /* 查不到就不打扰 */ }
  return null;
}

async function reportProgress(patch) {
  // 进度上报给后端，控制台轮询展示；上报失败绝不能影响投递本身
  try { await apiPost('/api/v1/run-progress', patch); } catch (e) { console.warn('[Applai] run-progress 上报失败', e); }
}

/* ---------- 跑批控制：暂停 / 停止 ----------
 * 控制台把 state=paused/stopped 写进 run-progress 行；执行器在每步之前读回这行，
 * 所以停止是真停——不会再开新职位页、不会再调简历接口、不会再烧 token。
 * 暂停 = 当前职位处理完后原地等待（带心跳），点继续后从下一份接着跑；
 * 停止 = 本轮结束，剩下的职位留在队列里。
 * 读控制态失败时默认继续（fail-open），免得后端抖动误杀一轮。 */
async function getRunControlState() {
  try {
    const d = await apiGet('/api/v1/run-progress');
    const s = (d && d.progress && d.progress.state) || '';
    return (s === 'paused' || s === 'stopped' || s === 'done') ? s : 'running';
  } catch (e) { return 'running'; }
}
async function waitWhilePaused(counts) {
  // 返回 'resumed' 或 'stopped'；等待期间每 5 秒报一次 paused 心跳
  for (;;) {
    await reportProgress({ state: 'paused', ...counts, current_job_id: 0, current_title: '已暂停' });
    await sleep(5000);
    const s = await getRunControlState();
    if (s === 'stopped' || s === 'done') return 'stopped';
    if (s === 'running') return 'resumed';
  }
}
// 在继续下一步之前检查一次控制态；返回 true=继续跑，false=本轮结束
async function runControlGate(counts) {
  const s = await getRunControlState();
  if (s === 'stopped' || s === 'done') return false;
  if (s === 'paused') return (await waitWhilePaused(counts)) === 'resumed';
  return true;
}
// 发进度之前先看一眼控制态，避免把用户刚点的暂停/停止又写回 running
async function reportRunState(patch) {
  const s = await getRunControlState();
  if (s === 'paused' || s === 'stopped') {
    await reportProgress({ ...patch, state: s });
    return s;
  }
  await reportProgress(patch);
  return 'running';
}
// 20–60 秒的限速等待切成 5 秒一段，每段都看一眼控制态，点停止不用等满一分钟
async function sleepWithGate(ms, counts) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    await sleep(Math.min(5000, end - Date.now()));
    if (!(await runControlGate(counts))) return false;
  }
  return true;
}

/* ---------- 跑批等待心跳 ----------
 * MV3 Service Worker 在 ~30 秒无活动后可能被 Chrome 回收；一旦回收，本轮投递就无声
 * 无息地死掉——内存里的循环状态全丢，也没有恢复路径（2026-11-25 真实跑批只跑了
 * 1/10 就停了，进度停在 1/10，原因就卡在这里）。所以跑批里的每一次长等待
 * （等标签页加载、等插件回填/提交）都要带心跳，每 5 秒向后端报一次进度，
 * 让 SW 一直有活动。心跳走 control-aware 的上报，不会把用户点的暂停/停止写回 running。
 * 限速等待已经由 sleepWithGate 每 5 秒读一次控制态（自带活动），抓取等待每 5 秒
 * 轮询一次任务状态，也都不需要再包一层。 */
async function withHeartbeat(promise, counts) {
  const beat = setInterval(() => { reportRunState({ state: 'running', ...counts }).catch(() => {}); }, 5000);
  try { return await promise; }
  finally { clearInterval(beat); }
}

async function recordSnapshot(userId, job, fillResult, profile, resumeInfo) {
  try {
    const r = fillResult || {};
    const filled = (r.filled || []).filter((f) => !SNAPSHOT_PII.has(f.intent));
    const salaryParts = (r.filled || [])
      .filter((f) => SNAPSHOT_SALARY_KEYS.has(f.intent))
      .map((f) => `${f.label || f.intent}: ${f.value || ''}`);
    const prof = profile || {};
    const resumeFile = prof.resumeFile || {};
    let resumeHash = '', resumeDataUrl = '';
    if (resumeInfo && resumeInfo.dataUrl) {
      resumeHash = resumeInfo.hash || await sha256hex(resumeInfo.dataUrl);
      resumeDataUrl = resumeInfo.dataUrl;
    } else if (resumeFile.dataUrl) {
      resumeHash = await sha256hex(resumeFile.dataUrl);
      resumeDataUrl = resumeFile.dataUrl;
    }
    await apiPost('/api/v1/applications/snapshot', {
      user_id: userId,
      job_id: job.id,
      job_url: job.url || '',
      site: '',
      filled_fields: filled.map((f) => ({ intent: f.intent, label: f.label, value: f.value })),
      salary_info: salaryParts.join('; '),
      cover_letter: prof.coverLetter || '',
      resume_name: (resumeInfo && resumeInfo.name) || resumeFile.name || '',
      resume_hash: (resumeInfo && resumeInfo.hash) || resumeHash,
      resume_data_url: (resumeInfo && resumeInfo.dataUrl) || resumeDataUrl,
      resume_version_id: (resumeInfo && resumeInfo.versionId) || 0,
      resume_tailored: !!((resumeInfo && resumeInfo.tailored)),
    });
  } catch (e) { /* 快照失败不影响主流程 */ }
}

async function recordApp(userId, jobId, status, detail) {
  try {
    await apiPost('/api/v1/applications', { user_id: userId, job_id: jobId, status, detail: detail || '' });
  } catch (e) { /* 后端不可达时只记本地 */ }
  const { aap_queue: q = [] } = await chrome.storage.local.get(['aap_queue']);
  const item = q.find((x) => x.jobId === jobId);
  if (item) { item.status = status; item.detail = detail; await chrome.storage.local.set({ aap_queue: q }); }
}

let _running = false;
async function runDailyQueue(trigger) {
  if (_running) return; // 已有一轮在跑，避免手动+定时重叠重复投递
  _running = true;
  try { await _runDailyQueueImpl(trigger); } finally { _running = false; }
}

async function _runDailyQueueImpl(trigger) {
  const s = await getSettings();
  const userId = await getUserId();
  const runKey = `aap_lastrun_${new Date().toISOString().slice(0, 10)}`;
  const { [runKey]: already } = await chrome.storage.local.get([runKey]);
  if (trigger === 'schedule' && already) return; // 每天只跑一次
  await chrome.storage.local.set({ [runKey]: Date.now() });
  await reportProgress({ state: 'running', total: 0, processed: 0, submitted: 0, manual: 0, failed: 0, current_job_id: 0, current_title: '正在抓取新职位…' });

  let queue;
  try {
    // 先按用户保存的来源自动抓取新职位（异步任务，轮询等完成，最多等 6 分钟）
    try {
      const t = await apiPost('/api/v1/jobs/discover/auto', {});
      if (t && t.task_id) {
        for (let i = 0; i < 72; i++) {
          await sleep(5000);
          if (!(await runControlGate({ total: 0, processed: 0, submitted: 0, manual: 0, failed: 0 }))) {
            await reportProgress({ state: 'stopped', total: 0, processed: 0, submitted: 0, manual: 0, failed: 0, current_job_id: 0, current_title: '已停止' });
            await pushViaNtfy('Applai：投递已停止', '在抓取新职位阶段就停下了，队列里的职位都没动');
            await desktopNotify('Applai：投递已停止', '在抓取阶段停下，队列里的职位都没动');
            return;
          }
          try {
            const st = await apiGet(`/api/v1/jobs/discover/status/${t.task_id}`);
            if (st.status === 'done' || st.status === 'error') break;
          } catch (e) { break; }
          // 抓取可能持续数分钟：每 30 秒报一次心跳，免得控制台把进度判为中断
          if (i % 6 === 5) await reportRunState({ state: 'running', total: 0, processed: 0, submitted: 0, manual: 0, failed: 0, current_job_id: 0, current_title: '正在抓取新职位…' });
        }
      }
    } catch (e) { /* best effort */ }
    const data = await apiGet(`/api/v1/queue?top_n=${s.dailyCount}`);
    queue = data.jobs || [];
  } catch (e) {
    await pushViaNtfy('Applai：今日投递失败', `无法连接后端（${s.backendUrl}），请检查后端是否运行`);
    await reportProgress({ state: 'done', total: 0, processed: 0, submitted: 0, manual: 0, failed: 0, current_job_id: 0, current_title: '无法连接后端' });
    return;
  }
  if (!queue.length) {
    await desktopNotify('Applai：今日无可投职位', '匹配队列为空，先去后端添加职位源');
    await reportProgress({ state: 'done', total: 0, processed: 0, submitted: 0, manual: 0, failed: 0, current_job_id: 0, current_title: '队列为空' });
    return;
  }

  // 本地也镜像一份队列
  await chrome.storage.local.set({
    aap_queue: queue.map((j) => ({ jobId: j.id, url: j.url, title: j.title, company: j.company, status: 'queued', score: j.score })),
  });

  // 画像在循环外解析一次：后端优先（附简历 dataUrl），本地缓存兜底，绝不为 undefined
  let profile = (await fetchBackendProfile())
    || (await chrome.storage.local.get(['aap_profile'])).aap_profile || {};

  let submitted = 0, manual = 0, failed = 0, processed = 0, stopped = false;
  await reportRunState({ state: 'running', total: queue.length, processed: 0, submitted: 0, manual: 0, failed: 0, current_job_id: 0, current_title: '' });
  for (let qi = 0; qi < queue.length; qi++) {
    const job = queue[qi];
    // 心跳上报用的计数快照：一次等待内 submitted/manual/failed 不会变
    const counts = { total: queue.length, processed: qi, submitted, manual, failed, current_job_id: job.id, current_title: job.title || '' };
    if (!(await runControlGate({ total: queue.length, processed: qi, submitted, manual, failed }))) { stopped = true; break; }
    await reportProgress({ state: 'running', total: queue.length, processed: qi, submitted, manual, failed, current_job_id: job.id, current_title: job.title || '' });
    let tab = null;
    try {
      tab = await chrome.tabs.create({ url: job.url, active: false });
      const loaded = await withHeartbeat(waitForTabLoad(tab.id), counts);
      if (!loaded) throw new Error('page load timeout');
      await sleep(rand(1500, 3500)); // 等动态表单渲染

      // 取该岗位该用的简历（画像开了改写 → 改写版；否则母版）
      const resumeInfo = await getResumeForJob(job.id, profile);
      if (resumeInfo.dataUrl) {
        resumeInfo.name = displayResumeName(profile, resumeInfo.name);
        profile.resumeFile = { name: resumeInfo.name, dataUrl: resumeInfo.dataUrl };
      }
      // 职位国家（Issue 3）：队列自带的优先；没有时问后端（先确定性直认，
      // 认不出再调 LLM，结果存回职位）。拿不到也不要紧——填表侧再就地解析。
      const jobCountries = await resolveJobCountries(job);
      let fill = await withHeartbeat(sendToTab(tab.id, { type: 'AUTOAPPLY_FILL', profile: profile || {}, options: { jobLocation: job.location || '', jobCountries } }, 120000), counts);
      if (!fill.ok) throw new Error('fill failed: ' + (fill.error || ''));
      // 后台标签页渲染慢：首扫没找到表单时多等几次再扫，表单出现即填上。
      // 只在 noForm（整页无字段）时重试，正常填过的页面不受影响。
      for (let attempt = 0; attempt < 2 && fill.result && fill.result.noForm; attempt++) {
        await sleep(3000);
        const retry = await withHeartbeat(sendToTab(tab.id, { type: 'AUTOAPPLY_FILL', profile: profile || {}, options: { jobLocation: job.location || '', jobCountries } }, 120000), counts);
        if (retry.ok && retry.result) fill = retry;
        else break;
      }
      const r = fill.result;
      // 投递留痕：填了什么先存下来（PII 已过滤）
      await recordSnapshot(userId, job, r, profile, resumeInfo);

      if (r.captcha && r.captcha.present) {
        await recordApp(userId, job.id, 'needs_manual', `验证码（${r.captcha.type}）`);
        manual++;
        await pushViaNtfy('Applai：需要人工处理验证码', `${job.title} @ ${job.company}\n${job.url}`);
      } else if (r.noForm) {
        await recordApp(userId, job.id, 'needs_manual',
          '未找到申请表：职位页上没有可填字段（表单可能藏在 Application 标签下，插件已尝试自动打开仍未找到）');
        manual++;
      } else if (r.missing.length || r.needsReview.length) {
        await recordApp(userId, job.id, 'needs_manual',
          `缺失字段: ${r.missing.map((m) => intentLbl(m.intent)).join(',') || '无'}; 待确认: ${r.needsReview.map((m) => intentLbl(m.intent)).join(',') || '无'}`);
        manual++;
      } else if (s.autoSubmit) {
        const sub = await withHeartbeat(sendToTab(tab.id, { type: 'AUTOAPPLY_SUBMIT', timeoutMs: 15000 }, 60000), counts);
        if (sub.ok && sub.result.submitted) {
          await recordApp(userId, job.id, 'submitted', `signal=${sub.result.signal}`);
          submitted++;
        } else {
          await recordApp(userId, job.id, 'needs_manual', '提交未确认，请人工检查');
          manual++;
        }
      } else {
        // 未开自动提交：填好等用户确认
        await recordApp(userId, job.id, 'filled', '已自动填写，待人工提交');
        manual++;
      }
    } catch (e) {
      failed++;
      await recordApp(userId, job.id, 'failed', String((e && e.message) || e).slice(0, 200));
    } finally {
      if (tab) { try { await chrome.tabs.remove(tab.id); } catch (e) { /* ignore */ } }
    }
    await reportRunState({ state: 'running', total: queue.length, processed: qi + 1, submitted, manual, failed, current_job_id: 0, current_title: '' });
    processed = qi + 1;
    // 限速：每份投递间隔 20–60 秒随机，避免触发反爬（等待中也可暂停/停止）
    if (!(await sleepWithGate(rand(20000, 60000), { total: queue.length, processed, submitted, manual, failed }))) { stopped = true; break; }
  }

  if (stopped) {
    await reportProgress({ state: 'stopped', total: queue.length, processed, submitted, manual, failed, current_job_id: 0, current_title: '' });
    const stopSummary = `投递已停止：已处理 ${processed} 个，成功 ${submitted}，待人工 ${manual}，失败 ${failed}；剩下的留在队列里`;
    await pushViaNtfy('Applai：投递已停止', stopSummary);
    await desktopNotify('Applai：投递已停止', stopSummary);
    return;
  }
  await reportProgress({ state: 'done', total: queue.length, processed: queue.length, submitted, manual, failed, current_job_id: 0, current_title: '' });
  const summary = `今日投递完成：成功 ${submitted}，待人工 ${manual}，失败 ${failed}`;
  await pushViaNtfy('Applai：' + summary, `共处理 ${queue.length} 个职位`);
  await desktopNotify('Applai', summary);
}

/* ---------- 消息路由 ---------- */
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg.type === 'AUTOAPPLY_NOTIFY') {
      const r = await pushViaNtfy(msg.title || 'Applai', msg.message || '');
      await desktopNotify(msg.title || 'Applai', msg.message || '');
      sendResponse({ ok: true, ntfy: r });
    } else if (msg.type === 'AUTOAPPLY_RUN_NOW') {
      // 立即确认「已启动」：整队跑完要几分钟，等完成才回复页面必然超时误报
      const already = _running;
      runDailyQueue('manual');
      sendResponse({ ok: true, started: !already, already });
    } else if (msg.type === 'AUTOAPPLY_CHECK_MANUAL') {
      // 跑批中不插手（跑批自己会填）；用户手动打开待人工职位页时才回传画像重填
      if (_running) { sendResponse({ ok: true, manual: null }); }
      else {
        const a = await findManualJob(msg.url || '');
        if (!a) { sendResponse({ ok: true, manual: null }); }
        else {
          let profile = (await fetchBackendProfile())
            || (await chrome.storage.local.get(['aap_profile'])).aap_profile || {};
          const resumeInfo = await getResumeForJob(a.job_id, profile);
          if (resumeInfo.dataUrl) {
            resumeInfo.name = displayResumeName(profile, resumeInfo.name);
            profile = { ...profile, resumeFile: { name: resumeInfo.name, dataUrl: resumeInfo.dataUrl } };
          }
          sendResponse({ ok: true, manual: { jobId: a.job_id, detail: a.detail || '', location: a.location || '' }, profile });
        }
      }
    } else if (msg.type === 'AUTOAPPLY_RESCHEDULE') {
      await scheduleDaily();
      sendResponse({ ok: true });
    } else if (msg.type === 'AUTOAPPLY_GET_USER') {
      sendResponse({ ok: true, user_id: await getUserId() });
    } else if (msg.type === 'AUTOAPPLY_SAVE_TOKEN') {
      // Dashboard「一键连接插件」：保存 API Token（简单校验格式）
      const t = String(msg.token || '').trim();
      if (/^aap_[0-9a-f]{32,}$/.test(t)) {
        const { aap_settings: s = {} } = await chrome.storage.local.get(['aap_settings']);
        await chrome.storage.local.set({ aap_settings: { ...s, apiToken: t } });
        sendResponse({ ok: true });
      } else {
        sendResponse({ ok: false, error: 'bad token format' });
      }
    } else {
      sendResponse({ ok: false, error: 'unknown message' });
    }
  })();
  return true;
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get(['aap_settings'], (cur) => {
    if (!cur.aap_settings) chrome.storage.local.set({ aap_settings: { ...DEFAULTS } });
  });
  syncScheduleFromBackend();
  // 每小时同步一次网页端的定时配置
  chrome.alarms.create('sync-settings', { periodInMinutes: 60 });
});

chrome.runtime.onStartup.addListener(() => syncScheduleFromBackend());

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'sync-settings') syncScheduleFromBackend();
});
