'use strict';
/* dashboard.test.js — 用 jsdom 验证 Dashboard：向导弹出、删号弹窗、队列旧错清理、首页计数
   跑法：node test/dashboard.test.js
   对比修复前：DASH_HTML=/tmp/dashboard_before.html node test/dashboard.test.js
   （修复前 T1/T3/T4/T5 应失败，T2 通过；修复后应全部通过） */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const HTML_PATH = process.env.DASH_HTML ||
  path.join(__dirname, '..', 'backend', 'dashboard.html');
const HTML = fs.readFileSync(HTML_PATH, 'utf8');

let passed = 0, failed = 0;
function check(name, cond, extra) {
  if (cond) { passed++; console.log(`  ✓ ${name}`); }
  else { failed++; console.log(`  ✗ ${name}${extra ? ' — ' + extra : ''}`); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* 启动一页 Dashboard，所有接口用桩返回。opts 可改：
   profileDelayMs（画像接口延迟；Infinity 表示永不返回）、profileBody、
   statsBody、queueBody、deferQueue（下一次 queue 请求挂起，手动放行）。 */
function boot(opts = {}) {
  const calls = [];
  let queueRelease = null;
  let queueDeferred = null;
  const profileDelay = opts.profileDelayMs === undefined ? 0 : opts.profileDelayMs;
  const profileBody = opts.profileBody === undefined
    ? { user_id: 'probe-user-1', profile: {}, person: {}, job: {}, tailor_enabled: false }
    : opts.profileBody;
  const statsBody = opts.statsBody || { stats: { queued: 0, submitted: 0, needs_manual: 0, skipped: 0 } };
  const queueBody = opts.queueBody || { count: 0, jobs: [] };
  const job1 = { id: 101, title: 'Data Scientist', company: 'Wealthsimple',
    location: 'Remote (Canada)', score: 64, score_reasons: ['Python'], url: 'https://example.com/j1' };

  function stubFetch(url, o) {
    const u = String(url);
    const method = (o && o.method) || 'GET';
    calls.push({ url: u, method });
    const ok = (body) => Promise.resolve({
      ok: true, status: 200,
      json: async () => body, text: async () => JSON.stringify(body),
    });
    if (u.includes('/api/v1/auth/me')) return ok({ app_user_id: 'probe-user-1', username: 'wobtest-probe' });
    if (u.includes('/health')) return ok({ ok: true, version: '0.2.0', llm: true });
    if (u.includes('/api/v1/profile')) {
      if (profileDelay === Infinity) return new Promise(() => {});
      if (profileDelay > 0) return new Promise((r) => setTimeout(() => r(ok(profileBody)), profileDelay));
      return ok(profileBody);
    }
    if (u.includes('/api/v1/profiles')) return ok({ profiles: [] });
    if (u.includes('/api/v1/sources')) return ok({ sources: [] });
    if (u.includes('/api/v1/jobs/discover/latest')) return ok({ task: null, jobs: [], pending_scoring: 0 });
    if (u.includes('/api/v1/queue')) {
      if (opts.deferQueue && !queueDeferred) {
        queueDeferred = new Promise((r) => { queueRelease = () => r(ok({ ...queueBody, jobs: [job1] })); });
        return queueDeferred;
      }
      return ok({ ...queueBody, jobs: queueBody.count ? [job1] : [] });
    }
    if (u.includes('/api/v1/applications')) return ok({ applications: [] });
    if (u.includes('/api/v1/stats')) return ok(statsBody);
    if (u.includes('/api/v1/chat/conversations') && method === 'POST') return ok({ id: 1, title: '新对话' });
    if (u.includes('/api/v1/chat/conversations/1/messages')) return ok({ messages: [] });
    if (u.includes('/api/v1/chat/conversations')) return ok({ conversations: [] });
    if (u.includes('/api/v1/auth/tokens')) return ok({ tokens: [] });
    if (u.includes('/api/v1/invites')) return ok({ invites: [] });
    if (u.includes('/api/v1/gmail/status')) return ok({ configured: false });
    if (u.includes('/api/v1/settings/keywords')) return ok({ keywords: [] });
    if (u.includes('/api/v1/settings')) return ok({ schedule_enabled: false, schedule_time: '10:00', daily_count: 10 });
    if (u.includes('/api/v1/run-progress')) return ok({ progress: null });
    if (u.includes('/api/v1/account') && method === 'DELETE') return ok({ ok: true });
    return ok({});
  }

  const jsdomErrors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (e) => {
    const m = String((e && e.stack) || e);
    if (/Not implemented: navigation/.test(m)) return; // 删号成功后 location.href="/" 的正常噪音
    jsdomErrors.push(m);
  });
  const dom = new JSDOM(HTML, {
    url: 'https://applai-backend.fly.dev/dashboard',
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole,
    beforeParse(window) {
      window.fetch = stubFetch;
      // 注意：这里故意不用自定义 confirm/prompt——jsdom 默认桩返回 undefined，
      // 正好复现“原生弹窗在无头环境被自动关掉”的现场。
    },
  });
  return { dom, window: dom.window, document: dom.window.document, calls, jsdomErrors,
    releaseQueue: () => { if (queueRelease) queueRelease(); } };
}

async function main() {
  console.log(`HTML: ${HTML_PATH}`);

  // T1 — 向导不等画像接口：画像接口永不返回，向导仍须马上弹出
  console.log('T1 向导不等画像接口就弹出');
  {
    const t = boot({ profileDelayMs: Infinity });
    await sleep(900);
    const modal = t.document.getElementById('obModal');
    check('画像接口挂起时向导已弹出', modal && modal.style.display === 'flex',
      modal ? `display=${modal.style.display}` : 'obModal 缺失');
    check('启动过程脚本无异常', t.jsdomErrors.length === 0, t.jsdomErrors[0] || '');
    t.dom.window.close();
  }

  // T2 — 画像已完整的老用户：后台核对后向导收回（乐观弹出的反向保护）
  console.log('T2 画像完整时向导收回');
  {
    const t = boot({ profileBody: { user_id: 'probe-user-1',
      profile: { email: 'a@b.c', targetTitles: ['AI Engineer'] }, person: {}, job: {}, tailor_enabled: false } });
    await sleep(900);
    const modal = t.document.getElementById('obModal');
    check('画像完整时向导已收回', modal && modal.style.display === 'none',
      modal ? `display=${modal.style.display}` : 'obModal 缺失');
    t.dom.window.close();
  }

  // T3 — 删号走页内弹窗：点按钮出弹窗，输 DELETE 确认后发出 DELETE 请求
  console.log('T3 删号页内弹窗流程');
  {
    const t = boot({});
    await sleep(700);
    const btn = t.document.getElementById('deleteAcctBtn');
    const modal = t.document.getElementById('delAcctModal');
    check('删号弹窗元素存在', !!modal, 'delAcctModal 缺失');
    btn.click();
    await sleep(100);
    check('点删除账号后弹窗出现', modal && modal.style.display === 'flex',
      modal ? `display=${modal.style.display}` : 'delAcctModal 缺失');
    // 先输错的：不应发出 DELETE
    const input = t.document.getElementById('delAcctInput');
    const confirmBtn = t.document.getElementById('delAcctConfirm');
    if (input && confirmBtn) {
      input.value = 'nope';
      confirmBtn.click();
      await sleep(100);
      check('输错确认词不发 DELETE', !t.calls.some((c) => c.method === 'DELETE' && c.url.includes('/api/v1/account')));
      check('输错时显示错误提示', (t.document.getElementById('delAcctErr').textContent || '').length > 0);
      input.value = 'DELETE';
      confirmBtn.click();
      await sleep(200);
      check('输 DELETE 后发出 DELETE /api/v1/account',
        t.calls.some((c) => c.method === 'DELETE' && c.url.includes('/api/v1/account')));
    } else {
      check('删号弹窗输入/确认按钮存在', false, 'delAcctInput 或 delAcctConfirm 缺失');
    }
    t.dom.window.close();
  }

  // T4 — 队列重拉时先清旧错：旧 400 文案在请求飞行途中就不应再挂着
  console.log('T4 队列重拉先清旧错');
  {
    const t = boot({ deferQueue: true, queueBody: { count: 1 } });
    await sleep(700);
    const box = t.document.getElementById('queue');
    box.innerHTML = '<div class="empty">加载失败<span class="en">Load failed</span>：请求失败 400 {"detail":"profile not found, sync profile first"}</div>';
    t.window.loadQueue();
    check('重拉发出后旧 400 文案已清除', !box.innerHTML.includes('profile not found'),
      box.innerHTML.slice(0, 80));
    t.releaseQueue();
    await sleep(200);
    check('重拉完成后显示新职位', box.innerHTML.includes('Data Scientist'), box.innerHTML.slice(0, 80));
    t.dom.window.close();
  }

  // T5 — 首页“今日队列”用队列口径：stats.queued=0 但队列有 7 个时应显示 7
  console.log('T5 首页今日队列与队列页一致');
  {
    const t = boot({ statsBody: { stats: { queued: 0, submitted: 0, needs_manual: 0, skipped: 0 } },
      queueBody: { count: 7 } });
    await sleep(900);
    const ql = t.document.getElementById('quickLinks').innerHTML;
    check('首页今日队列显示 7（不是 stats.queued 的 0）', ql.includes('<div class="n">7</div>'), ql.slice(0, 200));
    t.dom.window.close();
  }

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
