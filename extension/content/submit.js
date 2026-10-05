'use strict';
/* submit.js — 提交按钮点击 + 成功确认检测 */
(function () {
  const CONFIRM_RES = [
    /thank you for (your )?application/i,
    /application (has been )?submitted/i,
    /we('ve| have) received your application/i,
    /successfully applied/i,
    /your application was sent/i,
    /已提交|已收到|申请成功|感谢.*申请/i,
  ];

  function findSubmitButton() {
    // 1. 原生 submit
    let btn = document.querySelector('button[type="submit"], input[type="submit"]');
    if (btn && isClickable(btn)) return btn;
    // 2. 文案匹配的按钮（Greenhouse 常用 "Submit Application"）
    const cands = Array.from(document.querySelectorAll('button, input[type="button"], a.btn, [role="button"]'));
    const hit = cands.find((el) => {
      const t = ((el.textContent || el.value) || '').trim();
      return /^(submit( application)?|apply( now)?|send application|提交申请|提交)$/i.test(t) && isClickable(el);
    });
    return hit || null;
  }

  function isClickable(el) {
    if (!el || el.disabled) return false;
    const r = el.getBoundingClientRect ? el.getBoundingClientRect() : { width: 1, height: 1 };
    if (typeof el.checkVisibility === 'function') {
      try { return el.checkVisibility(); } catch (e) { return true; }
    }
    return r.width > 0 && r.height > 0;
  }

  function pageShowsConfirmation() {
    const text = (document.body ? document.body.innerText : '').slice(0, 8000);
    const url = location.href;
    if (/(thank|confirm|success|submitted)/i.test(url)) return true;
    return CONFIRM_RES.some((re) => re.test(text));
  }

  async function submitApplication(timeoutMs) {
    timeoutMs = timeoutMs || 12000;
    const btn = findSubmitButton();
    if (!btn) return { submitted: false, signal: 'no-submit-button' };
    const beforeUrl = location.href;
    btn.click();
    // 等待跳转或确认文案出现
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      await new Promise((r) => setTimeout(r, 500));
      if (location.href !== beforeUrl || pageShowsConfirmation()) {
        await new Promise((r) => setTimeout(r, 1500)); // 让确认页渲染完
        return { submitted: pageShowsConfirmation() || location.href !== beforeUrl,
                 signal: location.href !== beforeUrl ? 'url-changed' : 'confirmation-text' };
      }
    }
    // 超时：最后再检查一次
    return { submitted: pageShowsConfirmation(), signal: pageShowsConfirmation() ? 'confirmation-text' : 'timeout' };
  }

  window.__autoapply = window.__autoapply || {};
  window.__autoapply.submitApplication = submitApplication;
  window.__autoapply.findSubmitButton = findSubmitButton;
})();
