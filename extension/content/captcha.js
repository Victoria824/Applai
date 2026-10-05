'use strict';
/* captcha.js — 检测页面是否存在验证码 */
(function () {
  const TYPE_RES = [
    { type: 'reCAPTCHA', re: /recaptcha/i },
    { type: 'hCaptcha', re: /hcaptcha/i },
    { type: 'Turnstile', re: /turnstile|challenges\.cloudflare/i },
    { type: 'generic-captcha', re: /captcha/i },
  ];

  function classify(src) {
    for (const t of TYPE_RES) if (t.re.test(src || '')) return t.type;
    return null;
  }

  function checkCaptcha() {
    // 1. iframe（最常见形态）
    const frames = Array.from(document.querySelectorAll('iframe'));
    for (const f of frames) {
      const t = classify(f.src || '') || classify(f.title || '') || classify(f.name || '');
      if (t) return { present: true, type: t, detail: 'iframe' };
    }
    // 2. 显式挂载点
    const mount = document.querySelector('.g-recaptcha, #g-recaptcha, [data-sitekey], #hcaptcha, .h-captcha, .cf-turnstile');
    if (mount) {
      const cls = mount.className || '';
      return { present: true, type: classify(cls) || 'unknown', detail: 'mount-point' };
    }
    // 3. 文本兜底（多语言）
    const bodyText = (document.body && document.body.innerText || '').slice(0, 20000);
    if (/verify you are human|i'm not a robot|complete the (security )?check|请完成安全验证|拖动滑块/i.test(bodyText)) {
      // 保守：仅文本命中不判死，标记为可疑
      return { present: false, type: null, suspicious: true };
    }
    return { present: false, type: null };
  }

  window.__autoapply = window.__autoapply || {};
  window.__autoapply.checkCaptcha = checkCaptcha;
})();
