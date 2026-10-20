'use strict';
/* manual.js — 待人工职位页打开时自动重填
 *
 * 跑批投递时填好的表单在临时标签页里，标签一关就没了。用户从控制台点
 * 「打开职位页处理」进来看到的是空表单。这里在页面打开时向后台确认：
 * 这个职位是不是待人工/已填待提交？是的话用最新画像（含按岗简历）
 * 自动重填一遍，只把缺失/待确认项留给用户，并弹横幅说明还剩什么。
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

  async function boot() {
    const api = window.__autoapply || {};
    if (!api.detectATS || !api.fillApplication) return;
    let det = null;
    try { det = api.detectATS(); } catch (e) { /* ignore */ }
    if (!det || det.ats === 'unknown') return;

    let resp = null;
    try {
      resp = await new Promise((res) => {
        chrome.runtime.sendMessage({ type: 'AUTOAPPLY_CHECK_MANUAL', url: location.href },
          (r) => res(chrome.runtime.lastError ? null : r));
      });
    } catch (e) { return; }
    if (!resp || !resp.ok || !resp.manual || !resp.profile) return;

    await sleep(1200); // 等 ATS 表单渲染
    let result = null;
    for (let attempt = 0; attempt < 2; attempt++) {
      try { result = await api.fillApplication(resp.profile, { autoSubmit: false }); }
      catch (e) { result = null; }
      const acted = result && ((result.filled || []).length
        || (result.missing || []).length || (result.needsReview || []).length);
      if (acted) break;
      await sleep(2500); // 表单还没渲染好，再试一次
    }
    if (!result) return;
    const filledCount = (result.filled || []).length;
    const todo = [...(result.missing || []), ...(result.needsReview || [])]
      .map((m) => lbl(m.intent));
    if (!filledCount && !todo.length) return; // 什么都没发生（比如登录墙），不打扰
    let html = `<b>✅ Applai 已自动填写 ${filledCount} 项</b>`;
    if (result.captcha && result.captcha.present) html += '<br>⚠️ 页面有验证码，请先完成验证码';
    if (todo.length) html += `<br>还需要你处理：<b>${todo.join('、')}</b>`;
    else html += '<br>没有遗留项，检查一遍即可提交';
    html += '<br><span style="color:#666">检查无误后，请手动点击页面的提交按钮。</span>';
    showBanner(html);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(boot, 300));
  } else {
    setTimeout(boot, 300);
  }
})();
