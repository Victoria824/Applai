'use strict';
/* sw.js — 后台：队列存储 + ntfy 推送 + 每日 8 点自动投递执行器 */

const DEFAULTS = {
  topic: '', autoSubmit: false,
  backendUrl: 'https://applai-backend.fly.dev',
  scheduleEnabled: false, scheduleTime: '08:00', dailyCount: 10,
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

/* ---------- 定时：每天 8 点 ---------- */
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

async function recordApp(userId, jobId, status, detail) {
  try {
    await apiPost('/api/v1/applications', { user_id: userId, job_id: jobId, status, detail: detail || '' });
  } catch (e) { /* 后端不可达时只记本地 */ }
  const { aap_queue: q = [] } = await chrome.storage.local.get(['aap_queue']);
  const item = q.find((x) => x.jobId === jobId);
  if (item) { item.status = status; item.detail = detail; await chrome.storage.local.set({ aap_queue: q }); }
}

async function runDailyQueue(trigger) {
  const s = await getSettings();
  const userId = await getUserId();
  const runKey = `aap_lastrun_${new Date().toISOString().slice(0, 10)}`;
  const { [runKey]: already } = await chrome.storage.local.get([runKey]);
  if (trigger === 'schedule' && already) return; // 每天只跑一次
  await chrome.storage.local.set({ [runKey]: Date.now() });

  let queue;
  try {
    const data = await apiGet(`/api/v1/queue?user_id=${encodeURIComponent(userId)}&top_n=${s.dailyCount}`);
    queue = data.jobs || [];
  } catch (e) {
    await pushViaNtfy('Applai：今日投递失败', `无法连接后端（${s.backendUrl}），请检查后端是否运行`);
    return;
  }
  if (!queue.length) {
    await desktopNotify('Applai：今日无可投职位', '匹配队列为空，先去后端添加职位源');
    return;
  }

  // 本地也镜像一份队列
  await chrome.storage.local.set({
    aap_queue: queue.map((j) => ({ jobId: j.id, url: j.url, title: j.title, company: j.company, status: 'queued', score: j.score })),
  });

  let submitted = 0, manual = 0, failed = 0;
  for (const job of queue) {
    let tab = null;
    try {
      tab = await chrome.tabs.create({ url: job.url, active: false });
      const loaded = await waitForTabLoad(tab.id);
      if (!loaded) throw new Error('page load timeout');
      await sleep(rand(1500, 3500)); // 等动态表单渲染

      const { aap_profile: profile } = await chrome.storage.local.get(['aap_profile']);
      const fill = await sendToTab(tab.id, { type: 'AUTOAPPLY_FILL', profile: profile || {}, options: {} }, 90000);
      if (!fill.ok) throw new Error('fill failed: ' + (fill.error || ''));
      const r = fill.result;

      if (r.captcha && r.captcha.present) {
        await recordApp(userId, job.id, 'needs_manual', `验证码（${r.captcha.type}）`);
        manual++;
        await pushViaNtfy('Applai：需要人工处理验证码', `${job.title} @ ${job.company}\n${job.url}`);
      } else if (r.missing.length || r.needsReview.length) {
        await recordApp(userId, job.id, 'needs_manual',
          `缺失字段: ${r.missing.map((m) => m.intent).join(',') || '无'}; 待确认: ${r.needsReview.map((m) => m.intent).join(',') || '无'}`);
        manual++;
      } else if (s.autoSubmit) {
        const sub = await sendToTab(tab.id, { type: 'AUTOAPPLY_SUBMIT', timeoutMs: 15000 }, 60000);
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
    // 限速：每份投递间隔 20–60 秒随机，避免触发反爬
    await sleep(rand(20000, 60000));
  }

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
      runDailyQueue('manual').then(() => sendResponse({ ok: true }));
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
  scheduleDaily();
});

chrome.runtime.onStartup.addListener(() => scheduleDaily());
