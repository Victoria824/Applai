'use strict';
/* sw.js — 后台：队列持久化 + ntfy 手机推送 */

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get(['aap_settings', 'aap_queue'], (cur) => {
    if (!cur.aap_settings) chrome.storage.local.set({ aap_settings: { topic: '', autoSubmit: false } });
    if (!cur.aap_queue) chrome.storage.local.set({ aap_queue: [] });
  });
});

async function pushViaNtfy(title, message) {
  const { aap_settings: s = {} } = await chrome.storage.local.get(['aap_settings']);
  const topic = (s.topic || '').trim();
  if (!topic) {
    chrome.notifications.create({
      type: 'basic', iconUrl: 'icons/icon48.png',
      title: 'AutoApply：未配置 ntfy 主题',
      message: '请在插件设置中填写 ntfy 主题以启用手机推送',
    }).catch(() => {});
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
    return { ok: false, error: String(e && e.message || e) };
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg.type === 'AUTOAPPLY_NOTIFY') {
      const r = await pushViaNtfy(msg.title || 'AutoApply', msg.message || '');
      // 同时在桌面弹一条，保证没配 ntfy 也能看到
      try {
        await chrome.notifications.create({
          type: 'basic', iconUrl: 'icons/icon48.png',
          title: msg.title || 'AutoApply', message: (msg.message || '').slice(0, 200),
        });
      } catch (e) { /* notifications 权限缺失时忽略 */ }
      sendResponse({ ok: true, ntfy: r });
    } else if (msg.type === 'AUTOAPPLY_GET_QUEUE') {
      const { aap_queue = [] } = await chrome.storage.local.get(['aap_queue']);
      sendResponse({ ok: true, queue: aap_queue });
    } else {
      sendResponse({ ok: false, error: 'unknown message' });
    }
  })();
  return true; // 异步响应
});
