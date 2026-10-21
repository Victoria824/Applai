'use strict';
/* manual.js — 待人工职位页打开时自动重填
 *
 * 跑批投递时填好的表单在临时标签页里，标签一关就没了。用户从控制台点
 * 「打开职位页处理」进来看到的是空表单。这里在页面打开时向后台确认：
 * 这个职位是不是待人工/已填待提交？是的话用最新画像（含按岗简历）
 * 自动重填一遍，只把缺失/待确认项留给用户，并弹横幅说明还剩什么。
 *
 * 0.2.9 起失败不再静默：认出职位但这一页没找到表单时弹指引横幅；
 * 后台服务工作者冷启动丢响应时自动重试一次；Ashby 等 SPA 站内点
 * Apply 跳转到表单页（不整页刷新）后，URL 变化会再触发一次重填。
 */
(function () {
  if (window.__applaiManualBoot) return;
  window.__applaiManualBoot = true;

  const ZH = { sponsorship: '签证担保', resume: '简历文件', coverLetter: '求职信',
    workAuth: '工作许可', salary: '期望薪资', location: '所在地', linkedin: 'LinkedIn',
    github: 'GitHub', website: '个人网站', phone: '电话', email: '邮箱',
    startDate: '可入职日期', yearsExperience: '工作年限', education: '学历',
    gender: '性别', veteran: '退伍军人身份', disability: '残障状况',
    citizenship: '公民身份', firstName: '名', lastName: '姓' };
  const lbl = (i) => ZH[i] || i;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function showBanner(html) {
    if (document.getElementById('applai-manual-banner')) return;
    const box = document.createElement('div');
    box.id = 'applai-manual-banner';
    box.style.cssText = 'position:fixed;top:16px;right:16px;z-index:2147483647;max-width:340px;'
      + 'background:#fff;border:1px solid #c7dbff;border-left:4px solid #2f7cf6;border-radius:10px;'
      + 'box-shadow:0 8px 28px rgba(0,0,0,.18);padding:12px 36px 12px 14px;'
      + 'font:13px/1.6 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;color:#222';
    box.innerHTML = html
      + '<button id="applai-manual-close" style="position:absolute;top:6px;right:8px;border:none;'
      + 'background:none;font-size:15px;cursor:pointer;color:#888">✕</button>';
    document.documentElement.appendChild(box);
    const btn = document.getElementById('applai-manual-close');
    if (btn) btn.onclick = () => box.remove();
  }

  function checkManual() {
    // 后台服务工作者冷启动时首个响应可能丢失，重试一次
    const once = () => new Promise((res) => {
      try {
        chrome.runtime.sendMessage({ type: 'AUTOAPPLY_CHECK_MANUAL', url: location.href },
          (r) => res(chrome.runtime.lastError ? null : (r || null)));
      } catch (e) { res(null); }
    });
    return once().then((r) => (r ? r : sleep(800).then(once)));
  }

  const actedOn = (result) => !!(result && ((result.filled || []).length
    || (result.missing || []).length || (result.needsReview || []).length));

  // 页面上到底有没有申请表：没表单时引擎会把未填项一股脑报成待确认，
  // 不能据此弹「已填写」横幅——那是职位展示页，要给指引而不是假结果。
  // 注意：新版 Ashby 根本不用 <form> 元素，输入框直接躺在 tabpanel 里，
  // 也没有 data-testid——只能数真实的输入控件（2026-10-21 实测教训）。
  const hasFormFields = () => !!document.querySelector(
    'input:not([type="hidden"]), textarea, select, [role="combobox"]');
  const onApplicationPath = () => /\/application\/?$/.test(location.pathname);
  const filledSomething = (result) => actedOn(result)
    && ((result.filled || []).length > 0 || hasFormFields());

  async function tryRefill(api, profile, initialDelay = 1200) {
    await sleep(initialDelay); // 等 ATS 表单渲染
    let result = null;
    for (let attempt = 0; attempt < 2; attempt++) {
      try { result = await api.fillApplication(profile, { autoSubmit: false }); }
      catch (e) { result = null; }
      if (actedOn(result)) break;
      await sleep(2500); // 表单还没渲染好，再试一次
    }
    return result;
  }

  const versionTag = () => {
    try {
      const v = chrome.runtime.getManifest().version;
      return `<div style="margin-top:6px;color:#999;font-size:11px">Applai 插件 v${v}</div>`;
    } catch (e) { return ''; }
  };

  function showResultBanner(result) {
    const filledCount = (result.filled || []).length;
    const todo = [...(result.missing || []), ...(result.needsReview || [])]
      .map((m) => lbl(m.intent));
    let html = `<b>✅ Applai 已自动填写 ${filledCount} 项</b>`;
    if (result.captcha && result.captcha.present) html += '<br>⚠️ 页面有验证码，请先完成验证码';
    if (todo.length) html += `<br>还需要你处理：<b>${todo.join('、')}</b>`;
    else html += '<br>没有遗留项，检查一遍即可提交';
    html += '<br><span style="color:#666">检查无误后，请手动点击页面的提交按钮。</span>';
    html += versionTag();
    showBanner(html);
  }

  function showGuideBanner() {
    showBanner('<b>📌 Applai：这是你的待人工职位</b>'
      + '<br>这一页还没找到申请表——进入申请表页面后会自动重填；'
      + '如果已经在申请表页，刷新一次页面再试。'
      + versionTag());
  }

  async function boot() {
    const api = window.__autoapply || {};
    if (!api.detectATS || !api.fillApplication) return;
    let det = null;
    try { det = api.detectATS(); } catch (e) { /* ignore */ }
    if (!det || det.ats === 'unknown') return;

    const resp = await checkManual();
    if (!resp || !resp.ok || !resp.manual || !resp.profile) return;

    let done = false;
    const result = await tryRefill(api, resp.profile);
    if (filledSomething(result)) {
      done = true;
      showResultBanner(result);
    } else {
      showGuideBanner(); // 认出职位但没找到表单：给指引，不再静默
    }
    if (done) return;

    // 表单可能稍后才出现：Ashby 分标签页要先点 Application 标签
    // （引擎重填时会自动点；用户手动点也一样）——轮询等表单出现，
    // 一出现就重填。URL 不一定会变，所以不盯 URL、盯表单本身。
    if (typeof setInterval !== 'function') return;
    let ticks = 0;
    let busy = false;
    const watcher = setInterval(async () => {
      ticks++;
      if (done || ticks > 45) { clearInterval(watcher); return; }
      if (busy || !(hasFormFields() || onApplicationPath())) return;
      busy = true;
      try {
        const r2 = await checkManual();
        if (!r2 || !r2.ok || !r2.manual || !r2.profile) return;
        const res2 = await tryRefill(api, r2.profile, 300);
        if (filledSomething(res2)) {
          done = true;
          const old = document.getElementById('applai-manual-banner');
          if (old) old.remove();
          showResultBanner(res2);
          clearInterval(watcher);
        }
      } finally { busy = false; }
    }, 2000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(boot, 300));
  } else {
    setTimeout(boot, 300);
  }
})();
