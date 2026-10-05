'use strict';
/* bridge.js — popup 与 content script 的消息路由 */
(function () {
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    try {
      const api = window.__autoapply || {};
      if (msg.type === 'AUTOAPPLY_DETECT') {
        sendResponse({ ok: true, result: api.detectATS ? api.detectATS() : { ats: 'unknown', confidence: 0, via: 'none' } });
      } else if (msg.type === 'AUTOAPPLY_CAPTCHA') {
        sendResponse({ ok: true, result: api.checkCaptcha ? api.checkCaptcha() : { present: false } });
      } else if (msg.type === 'AUTOAPPLY_FILL') {
        if (!api.fillApplication) { sendResponse({ ok: false, error: 'fill engine not loaded' }); return true; }
        // 填写引擎是异步的（自定义下拉框需要等待展开/提交）
        Promise.resolve(api.fillApplication(msg.profile || {}, msg.options || {})).then(
          (result) => sendResponse({ ok: true, result }),
          (err) => sendResponse({ ok: false, error: String((err && err.message) || err) })
        );
        return true; // 异步响应
      } else {
        sendResponse({ ok: false, error: 'unknown message' });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String(e && e.message || e) });
    }
    return false; // 同步响应
  });
})();
