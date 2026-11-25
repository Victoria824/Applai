'use strict';
/* dashboard.test.js — 用 jsdom 验证 Dashboard：向导弹出、删号弹窗、队列旧错清理、首页计数
   跑法：node test/dashboard.test.js
   对比修复前：DASH_HTML=/tmp/dashboard_before.html node test/dashboard.test.js
   （修复前 T1/T3/T4/T5 应失败，T2 通过；T6/T7/T8/T9 是 Deploy #7 新增的回归测试，
   修复前也应失败；T10 是 Deploy #8 新增：它测的是首页渲染 count 而非 jobs.length，
   对旧后端（count=页内数）用 DASH_HTML 跑不到失败——它的失败前对照在后端测试里；
   但把 HTML 里两处 .count 换成 jobs.length 后 T10 应失败，见下方注释）修复后应全部通过；
   T11/T12 是 Step-3 缺项提示新增：T11（Deploy #8 走查现场：空国家栏 + 空错误框）
   修复前应失败；T12（真正预填值 untouched 通过）是 DOM 读值的回归守护，前后都应通过 */
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
  const agentsBody = opts.agentsBody || { agents: [] };
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
    if (u.includes('/api/v1/agents')) return ok(agentsBody);
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
      if (opts.queueError400) {
        return Promise.resolve({ ok: false, status: 400,
          json: async () => ({ detail: 'profile not found, sync profile first' }),
          text: async () => JSON.stringify({ detail: 'profile not found, sync profile first' }) });
      }
      if (opts.deferQueue && !queueDeferred) {
        queueDeferred = new Promise((r) => { queueRelease = () => r(ok({ ...queueBody, jobs: [job1] })); });
        return queueDeferred;
      }
      return ok({ ...queueBody, jobs: queueBody.count ? [job1] : [] });
    }
    if (u.includes('/api/v1/profile/suggest-job') && method === 'POST') {
      return ok({ suggestion: { targetTitles: ['AI Engineer'], industries: ['Artificial Intelligence'],
        preferredLocations: ['Toronto'], salaryMin: 95000, salaryMax: 135000, reason: 'test' } });
    }
    if (u.includes('/api/v1/jobs/discover/auto') && method === 'POST') return ok({ task_id: 'task-1' });
    if (u.includes('/api/v1/resume/parse') && method === 'POST') {
      return ok({ fields: { firstName: 'Alex', lastName: 'Chen', email: 'alex@test.com',
        targetTitles: ['AI Engineer'], preferredLocations: ['Toronto'], yearsExperience: 5 } });
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
    releaseQueue: () => { if (queueRelease) queueRelease(); },
    setQueueCount: (n) => { queueBody.count = n; } };
}

/* 把向导从第 1 步带到第 3 步（上传假简历 → 第 2 步确认）。返回是否到达第 3 步。 */
async function driveToStep3(t) {
  await sleep(700);
  const nextBtn = () => t.document.getElementById('obNext');
  const fileInput = t.document.getElementById('obFile');
  const file = new t.window.File(['%PDF-1.4 fake'], 'resume.pdf', { type: 'application/pdf' });
  Object.defineProperty(fileInput, 'files', { value: [file] });
  fileInput.dispatchEvent(new t.window.Event('change', { bubbles: true }));
  await sleep(2200);  // 解析 + FileReader + obRunAI（800ms 停顿）+ 渲染
  if (!nextBtn() || !nextBtn().textContent.includes('就这样，下一步')) return false;
  nextBtn().click();
  await sleep(700);
  return !!(nextBtn() && nextBtn().textContent.includes('确认，下一步'));
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

  // T6 — 没画像时队列 400 不应挂“加载失败”：显示中性未就绪态
  console.log('T6 队列 400 显示中性未就绪态');
  {
    const t = boot({ queueError400: true });
    await sleep(900);
    const box = t.document.getElementById('queue');
    check('400 时不显示“加载失败”', !box.innerHTML.includes('加载失败'), box.innerHTML.slice(0, 120));
    check('400 时不出现 profile not found 原文', !box.innerHTML.includes('profile not found'), box.innerHTML.slice(0, 120));
    check('400 时显示队列未就绪', box.innerHTML.includes('还没准备好'), box.innerHTML.slice(0, 120));
    t.dom.window.close();
  }

  // T7 — 启动/重拉后首页与队列页一致：stats.queued=0 但队列 1 个时都显示 1
  console.log('T7 重拉后首页计数与队列页一致');
  {
    const t = boot({ statsBody: { stats: { queued: 0, submitted: 0, needs_manual: 0, skipped: 0 } },
      queueBody: { count: 0 } });
    await sleep(900);
    const firstStat = () => t.document.querySelector('#stats .stat .n');
    check('入队前首页待投递为 0', firstStat() && firstStat().textContent === '0',
      firstStat() && firstStat().textContent);
    t.setQueueCount(1);
    await t.window.loadAll();
    await sleep(300);
    const ql = t.document.getElementById('quickLinks').innerHTML;
    check('重拉后首页今日队列显示 1', ql.includes('<div class="n">1</div>'), ql.slice(0, 200));
    check('重拉后统计行待投递显示 1', firstStat() && firstStat().textContent === '1',
      firstStat() && firstStat().textContent);
    t.dom.window.close();
  }

  // T8 — 点启动立刻有反馈：有真职位时点“启动全自动投递”，马上出现 toast；
  // 无插件时 runNow 的诊断 toast 要留够 8 秒（旧 2.2 秒在走查里三次都没被看到）
  // T9 — 第 2 步确认保存期间按钮锁住并显示“保存中…”（旧代码数秒无反馈，走查里补点了一下）
  console.log('T8/T9 向导启动 toast 与保存忙态');
  {
    const t = boot({});
    await sleep(700);
    const nextBtn = () => t.document.getElementById('obNext');
    // 第 1 步：上传简历
    const fileInput = t.document.getElementById('obFile');
    const file = new t.window.File(['%PDF-1.4 fake'], 'resume.pdf', { type: 'application/pdf' });
    Object.defineProperty(fileInput, 'files', { value: [file] });
    fileInput.dispatchEvent(new t.window.Event('change', { bubbles: true }));
    await sleep(2200);  // 解析 + FileReader + obRunAI（800ms 停顿）+ 渲染
    check('上传后进入第 2 步', nextBtn() && nextBtn().textContent.includes('就这样，下一步'),
      nextBtn() && nextBtn().textContent);
    // 第 2 步：点确认——同步检查忙态（T9），再等切到第 3 步
    t.setQueueCount(1);  // 让第 4 步轮询一开始就看到 1 个职位
    nextBtn().click();
    check('保存期间按钮锁住', nextBtn().disabled === true, `disabled=${nextBtn().disabled}`);
    check('保存期间显示“保存中…”', (nextBtn().textContent || '').includes('保存中'), nextBtn().textContent);
    await sleep(700);
    check('第 2 步确认后进入第 3 步', nextBtn() && nextBtn().textContent.includes('确认，下一步'),
      nextBtn() && nextBtn().textContent);
    // 第 3 步：填必答项并确认
    const setVal = (id, v) => { const el = t.document.getElementById(id); if (el) el.value = v; };
    setVal('obqCountries', 'Canada, United States');
    setVal('obqSponsor', 'no');
    setVal('obqCompany', 'Fictional Test Corp');
    setVal('obqTitle', 'AI Engineer');
    nextBtn().click();
    await sleep(900);  // 保存 + 第 4 步渲染 + 首轮 obPollLaunch
    check('第 3 步确认后进入第 4 步', nextBtn() && nextBtn().textContent.includes('启动全自动投递'),
      nextBtn() && nextBtn().textContent);
    check('第 4 步有职位时启动按钮已放开', nextBtn().disabled === false, `disabled=${nextBtn().disabled}`);
    // 第 4 步：点启动——马上就应有 toast（T8）
    nextBtn().click();
    const toastEl = t.document.getElementById('toast');
    check('点启动后立刻出现 toast', toastEl && toastEl.style.display === 'block',
      toastEl ? `display=${toastEl.style.display} text=${toastEl.textContent}` : 'toast 缺失');
    check('启动 toast 文案提到已启动', (toastEl.textContent || '').includes('已启动'), toastEl.textContent);
    // runNow 无插件：3s 等待 + 1.2s 诊断后出“未检测到插件”，8 秒内不应消失
    await sleep(4600);
    check('无插件时出现“未检测到插件”提示', (toastEl.textContent || '').includes('未检测到插件'), toastEl.textContent);
    check('诊断 toast 仍可见', toastEl.style.display === 'block', `display=${toastEl.style.display}`);
    await sleep(3000);  // 距点击约 7.6 秒：旧 2.2 秒留存早已消失，新 8 秒还在
    check('诊断 toast 7.6 秒后仍可见（8 秒留存）', toastEl.style.display === 'block',
      `display=${toastEl.style.display} text=${toastEl.textContent}`);
    t.dom.window.close();
  }

  // T10 — 首页计数是总数不是页内数：队列接口 top_n=1 只回 1 条 jobs，
  // 但 count=4 时首页“今日队列”和统计行“待投递”都应显示 4（Deploy #7
  // 走查：首页 1，队列页 2→3→4；旧后端 count=截取后的页内数所以首页最多是 1）。
  // 注意：这是前端渲染 count 的契约测试——后端修法的失败前对照在
  // test_backend.py 的 test_queue_count_is_total_not_page_size 里。
  console.log('T10 首页计数取总数（count），不是页内 jobs 数');
  {
    const t = boot({ statsBody: { stats: { queued: 0, submitted: 0, needs_manual: 0, skipped: 0 } },
      queueBody: { count: 4 } });
    await sleep(900);
    const ql = t.document.getElementById('quickLinks').innerHTML;
    check('首页今日队列显示 4（总数，不是 1 条 jobs）', ql.includes('<div class="n">4</div>'), ql.slice(0, 200));
    const firstStat = () => t.document.querySelector('#stats .stat .n');
    check('统计行待投递显示 4', firstStat() && firstStat().textContent === '4',
      firstStat() && firstStat().textContent);
    t.dom.window.close();
  }

  // T11 — 第 3 步缺项时错误信息要常驻可读（Deploy #8 走查现场）：
  // 新用户画像里没有 authorizedWorkCountries（简历解析白名单不含它），国家栏是空的，
  // 只填其它三项就点确认：旧代码只弹 2.2 秒 toast，#obQErr 永远是空的，而空的 .ob-err
  // 框一直显示成粉色空框。修后：缺项提示写进 #obQErr（常驻），空框隐藏。
  console.log('T11 第 3 步缺项提示常驻在错误框里');
  {
    const t = boot({});
    const reached = await driveToStep3(t);
    check('到达第 3 步', reached);
    const c = t.document.getElementById('obqCountries');
    check('新用户国家栏值为空（不是预填）', c && c.value === '', c && JSON.stringify(c.value));
    check('国家栏没有会冒充已填值的 placeholder', c && !c.placeholder, c && JSON.stringify(c.placeholder));
    const qe = t.document.getElementById('obQErr');
    check('渲染时错误框内容为空', qe && qe.textContent === '', qe && JSON.stringify(qe.textContent));
    const cssText = Array.from(t.document.querySelectorAll('style')).map((s) => s.textContent).join('');
    check('样式里有空错误框隐藏规则', cssText.includes('.ob-err:empty'));
    const setVal = (id, v) => { const el = t.document.getElementById(id); if (el) el.value = v; };
    setVal('obqSponsor', 'no');
    setVal('obqCompany', 'TestCorp');
    setVal('obqTitle', 'AI Engineer');
    t.document.getElementById('obNext').click();
    await sleep(300);
    const toastEl = t.document.getElementById('toast');
    check('缺项时仍停在第 3 步', (t.document.getElementById('obNext').textContent || '').includes('确认，下一步'),
      t.document.getElementById('obNext').textContent);
    check('toast 提示缺项', (toastEl.textContent || '').includes('请先填好：合法工作国家'), toastEl.textContent);
    check('错误框里有可读的缺项提示', qe && qe.textContent === '请先填好：合法工作国家',
      qe && JSON.stringify(qe.textContent));
    // 走查里的 workaround：补填国家后一次点确认就应通过
    setVal('obqCountries', 'Canada, United States');
    t.document.getElementById('obNext').click();
    await sleep(900);
    check('补填国家后进入第 4 步', (t.document.getElementById('obNext').textContent || '').includes('启动全自动投递'),
      t.document.getElementById('obNext').textContent);
    t.dom.window.close();
  }

  // T12 — 真正预填好的值、没动过也能保存通过（DOM 实时读值的契约）：
  // 画像里已有 authorizedWorkCountries/公司/职位/担保时，第 3 步不碰任何栏直接点确认
  // 就应进入第 4 步，且错误框保持空白。Deploy #8 走查的原始假设（预填值不在表单
  // 状态里）经复现被证伪——读值走的是 DOM，这条是回归守护。
  console.log('T12 预填值 untouched 也能保存通过');
  {
    const t = boot({ profileBody: { user_id: 'probe-user-1',
      profile: { authorizedWorkCountries: ['Canada', 'United States'], currentCompany: 'TestCorp',
        currentTitle: 'AI Engineer', needsSponsorship: false }, person: {}, job: {}, tailor_enabled: false } });
    const reached = await driveToStep3(t);
    check('到达第 3 步', reached);
    const c = t.document.getElementById('obqCountries');
    check('国家栏预填了值', c && c.value === 'Canada, United States', c && JSON.stringify(c.value));
    t.document.getElementById('obNext').click();  // 一个栏都不碰
    await sleep(900);
    check('不碰任何栏直接确认进入第 4 步', (t.document.getElementById('obNext').textContent || '').includes('启动全自动投递'),
      t.document.getElementById('obNext').textContent);
    const qe = t.document.getElementById('obQErr');
    check('通过时错误框保持空白', !qe || qe.textContent === '', qe && JSON.stringify(qe.textContent));
    t.dom.window.close();
  }

  // T13 — 团队页卡通头像改版（Deploy #10）：5 张头像卡、状态 pill 跟 live 状态、
  // 点卡片选中进 spotlight、打招呼轮换台词、流水线映射、头顶 live pill、小队动态。
  // 修复前（DASH_HTML=/tmp/dashboard_before.html）应失败：旧团队页只有 4 张无头像卡、
  // 没有 spotlight / live pill / feed，流水线步骤也没有 data-agent。
  console.log('T13 团队页卡通头像改版');
  {
    const agentsBody = { agents: [
      { id: 'jennifer', name: 'Jennifer', status: 'active', current_task: '给新职位打分',
        today_stats: [{ label_zh: '今日已打分', label_en: 'Scored today', value: 57 },
          { label_zh: '待打分', label_en: 'Pending scoring', value: 162 }],
        recent_events: [{ summary: '打分完成：StackAdapt ML 岗 85 分', created_at: '2026-11-25T09:58:00Z' }] },
      { id: 'amy', name: 'Amy', status: 'idle', current_task: null,
        today_stats: [{ label_zh: '今日新增职位', label_en: 'New jobs today', value: 485 },
          { label_zh: '今日新发现', label_en: 'Newly found', value: 32 }],
        recent_events: [{ summary: '发现 3 个新职位', created_at: '2026-11-25T09:41:00Z' }] },
      { id: 'caroline', name: 'Caroline', status: 'idle', current_task: null,
        today_stats: [{ label_zh: '今日队列', label_en: "Today's queue", value: 103 }],
        recent_events: [] },
      { id: 'gloria', name: 'Gloria', status: 'active', current_task: '读新邮件',
        today_stats: [{ label_zh: '待回邮件', label_en: 'Replies pending', value: 2 }],
        recent_events: [] },
      { id: 'rachel', name: 'Rachel', status: 'idle', current_task: null,
        today_stats: [{ label_zh: '今日计划 · 10:00', label_en: "Today's plan · 10:00", value: 10 }],
        recent_events: [{ summary: '定时任务已排好', created_at: '2026-11-25T10:00:00Z' }] },
    ] };
    const t = boot({ agentsBody });
    await sleep(700);
    const spot = () => t.document.getElementById('teamSpotlight');
    // 默认选中 Rachel（改版前 spotlight 元素根本不存在）
    check('默认 spotlight 是 Rachel', spot() && spot().textContent.includes('Rachel')
      && spot().textContent.includes('🦊 小熊猫 · 全队总指挥'), spot() && spot().textContent.slice(0, 120));
    check('默认 spotlight 有第一句台词', spot() && spot().textContent.includes('全队就位，今天 10 点准时开工！'),
      spot() && spot().textContent.slice(0, 120));
    await t.window.loadTeam();
    const cards = () => Array.from(t.document.querySelectorAll('#teamCards .agent-card'));
    const card = (id) => t.document.querySelector(`#teamCards .agent-card[data-agent="${id}"]`);
    check('团队页有 5 张卡（含 Rachel）', cards().length === 5, `cards=${cards().length}`);
    // 头像：5 位都有 inline SVG，且颜色对得上
    const avaColors = { jennifer: '#2BB3A3', amy: '#FFB020', caroline: '#5B8DEF', gloria: '#F27EA9', rachel: '#8B7CF6' };
    let avaOk = cards().length === 5;
    for (const [id, color] of Object.entries(avaColors)) {
      const c = card(id);
      const svg = c && c.querySelector('.agent-ava svg');
      if (!svg || !svg.outerHTML.includes(color)) avaOk = false;
    }
    check('5 张卡都有头像 SVG 且颜色正确', avaOk);
    // 状态 pill 跟 live 状态走
    const jenBadge = card('jennifer') && card('jennifer').querySelector('.agent-badge');
    const amyBadge = card('amy') && card('amy').querySelector('.agent-badge');
    check('工作中（Jennifer）是绿色 active pill', jenBadge && jenBadge.classList.contains('active')
      && jenBadge.textContent.includes('工作中'), jenBadge && jenBadge.className);
    check('工作中 pill 有脉冲绿点', jenBadge && !!jenBadge.querySelector('.pulse-dot'));
    check('待命（Amy）是灰色 idle pill', amyBadge && amyBadge.classList.contains('idle')
      && amyBadge.textContent.includes('待命'), amyBadge && amyBadge.className);
    check('待命 pill 没有脉冲绿点', amyBadge && !amyBadge.querySelector('.pulse-dot'));
    // “正在做”：有 live 任务显示 live 文案，没有则显示固定文案
    check('有 live 任务时卡片显示它', card('jennifer') && card('jennifer').textContent.includes('正在做：给新职位打分'));
    check('无 live 任务时显示固定文案', card('amy') && card('amy').textContent.includes('正在逛招聘网站找新职位…'));
    // mini stats：只取前两项
    check('卡片 mini stats 显示前两项', card('jennifer') && card('jennifer').textContent.includes('57')
      && card('jennifer').textContent.includes('162'));
    // 头顶 live pill：工作人数 + Rachel 的 live 排班（不写死）
    const pill = t.document.getElementById('teamLiveText');
    check('live pill 有工作人数', pill && pill.textContent.includes('全队在线 · 2 位工作中'),
      pill && pill.textContent);
    check('live pill 有 Rachel 的 live 排班', pill && pill.textContent.includes('今日计划 · 10:00 10 份'),
      pill && pill.textContent);
    // 点卡片选中进 spotlight
    if (card('amy')) card('amy').click();
    check('点 Amy 卡后 spotlight 是 Amy', spot() && spot().textContent.includes('Amy')
      && spot().textContent.includes('🐝 小蜜蜂 · 勤劳搜罗员'), spot() && spot().textContent.slice(0, 120));
    check('点卡片后该卡有 sel 态', card('amy') && card('amy').classList.contains('sel'));
    check('点卡片后其它卡无 sel 态', card('jennifer') && !card('jennifer').classList.contains('sel'));
    // 打招呼轮换台词
    const sayHi = () => t.document.getElementById('teamSayHi');
    check('Amy 第一句台词', spot() && spot().textContent.includes('刚发现 3 个新职位，正在排队等打分！'));
    if (sayHi()) sayHi().click();
    check('打招呼后换第二句', spot() && spot().textContent.includes('招聘网站我都逛遍啦，一个不漏～'),
      spot() && spot().textContent.slice(0, 120));
    if (sayHi()) sayHi().click();
    check('再打招呼换第三句', spot() && spot().textContent.includes('有新坑位我就第一时间叼回来！'));
    if (sayHi()) sayHi().click();
    check('三句轮完回到第一句', spot() && spot().textContent.includes('刚发现 3 个新职位，正在排队等打分！'));
    // 流水线映射：Jennifer→画像，Amy→搜罗，Jennifer→打分，Caroline→投递，Gloria→复盘
    const steps = Array.from(t.document.querySelectorAll('#page-team .pipe-step'));
    check('流水线有 5 步', steps.length === 5, `steps=${steps.length}`);
    check('流水线映射到正确 agent',
      steps.map((s) => s.dataset.agent).join(',') === 'jennifer,amy,jennifer,caroline,gloria',
      steps.map((s) => s.dataset.agent).join(','));
    check('流水线圆点是 agent 首字母',
      steps.map((s) => (s.querySelector('.pipe-dot') || {}).textContent).join('') === 'JAJCG',
      steps.map((s) => (s.querySelector('.pipe-dot') || {}).textContent).join(''));
    // 点流水线一步也选中
    if (steps[3]) steps[3].click();
    check('点流水线投递步后 spotlight 是 Caroline', spot() && spot().textContent.includes('Caroline')
      && spot().textContent.includes('🐧 小企鹅 · 靠谱投递员'));
    // 小队动态：用同一份 recent_events，带 mini 头像的气泡
    const feed = t.document.getElementById('teamFeed');
    check('动态里有 Jennifer 的事件', feed && feed.textContent.includes('打分完成：StackAdapt ML 岗 85 分'),
      feed && feed.textContent.slice(0, 120));
    check('动态里有 Amy 的事件', feed && feed.textContent.includes('发现 3 个新职位'));
    check('动态气泡带 mini 头像', feed && feed.querySelectorAll('.feed-msg .feed-ava svg').length >= 2,
      feed && String(feed.querySelectorAll('.feed-msg').length));
    t.dom.window.close();
  }

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
