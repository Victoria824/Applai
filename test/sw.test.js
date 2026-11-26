'use strict';
/* sw.test.js — Service Worker 跑批长等待心跳 (node test/sw.test.js)
 *
 * 背景：2026-11-25 Victoria 的真实跑批在第 1 个职位（待人工）之后无声无息地停了，
 * 剩下 9 个没跑、没有报错、进度停在 1/10。根因是 MV3 Service Worker 在 ~30 秒无活动
 * 后会被 Chrome 回收，而跑批循环里有几处裸等（等标签页加载 ≤30s、等插件回填 ≤120s）
 * 期间没有任何活动，SW 一死，内存里的循环状态就全丢了，也没有恢复路径。
 * 修复：跑批里的每一次长等待都要带心跳（每 5 秒向后端报一次进度），让 SW 一直有活动。
 * 本文件验证：长等待期间确实有心跳；心跳不会把用户点的暂停/停止写回 running；
 * 正常跑批仍能走完。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { webcrypto } = require('node:crypto');

const SW_SRC = fs.readFileSync(path.join(__dirname, '..', 'extension', 'background', 'sw.js'), 'utf8');

const JOB = { id: 101, url: 'https://job-boards.greenhouse.io/testco/jobs/101', title: 'Test Engineer', company: 'TestCo', location: 'Toronto, ON, Canada' };
const FILL_MANUAL = { ok: true, result: { filled: [], missing: [], needsReview: [{ intent: 'workAuth' }], captcha: { present: false }, noForm: false } };

let passed = 0, failed = 0;
function check(name, cond, extra) {
  if (cond) { passed++; console.log(`  ✓ ${name}`); }
  else { failed++; console.log(`  ✗ ${name}${extra ? ' — ' + extra : ''}`); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(cond, timeoutMs, stepMs = 100) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) { if (cond()) return true; await sleep(stepMs); }
  return cond();
}
function ok(body) { return { ok: true, status: 200, json: async () => body }; }

function makeEnv(cfg = {}) {
  const api = { progressPosts: [], controlState: 'running', apps: [], snapshots: [], countriesCalls: [] };
  const tabs = { updatedListeners: [], created: [], fillMessages: [] };
  let fillStart = 0, loadStart = 0;
  const runtime = { messageHandler: null };
  const chromeStub = {
    storage: { local: { _store: {}, async get(keys) { const out = {}; const arr = Array.isArray(keys) ? keys : [keys]; for (const k of arr) out[k] = this._store[k]; return out; }, async set(obj) { Object.assign(this._store, obj); } } },
    tabs: {
      async create() { const tab = { id: 7 }; tabs.created.push(tab); loadStart = Date.now(); return tab; },
      async remove() {},
      sendMessage(tabId, msg, cb) {
        if (msg.type === 'AUTOAPPLY_FILL') {
          tabs.fillMessages.push(msg);
          fillStart = Date.now();
          if (cfg.flipControlAtFillMs != null) setTimeout(() => { api.controlState = 'stopped'; }, cfg.flipControlAtFillMs);
          setTimeout(() => cb(cfg.fillResponse || FILL_MANUAL), cfg.fillDelayMs ?? 0);
        } else if (msg.type === 'AUTOAPPLY_SUBMIT') {
          setTimeout(() => cb({ ok: true, result: { submitted: true } }), cfg.submitDelayMs ?? 0);
        } else { setTimeout(() => cb({ ok: false, error: 'unknown' }), 0); }
      },
      onUpdated: { addListener(fn) { tabs.updatedListeners.push(fn); }, removeListener(fn) { const i = tabs.updatedListeners.indexOf(fn); if (i >= 0) tabs.updatedListeners.splice(i, 1); } },
    },
    alarms: { async create() {}, async clear() {}, onAlarm: { addListener() {} } },
    notifications: { async create() { return 'n1'; } },
    runtime: {
      onMessage: { addListener(fn) { runtime.messageHandler = fn; } },
      onInstalled: { addListener() {} },
      onStartup: { addListener() {} },
    },
  };
  async function fetchStub(url, opts = {}) {
    const u = String(url);
    const method = (opts.method || 'GET').toUpperCase();
    const p = u.replace('https://applai-backend.fly.dev', '');
    if (p.startsWith('/api/v1/run-progress') && method === 'POST') {
      api.progressPosts.push({ t: Date.now(), ...JSON.parse(opts.body || '{}') });
      return ok({ ok: true });
    }
    if (p.startsWith('/api/v1/run-progress')) return ok({ progress: { state: api.controlState } });
    if (p.startsWith('/api/v1/jobs/discover/auto')) return ok({});
    if (p.startsWith('/api/v1/queue')) return ok({ jobs: cfg.jobs ?? [JOB] });
    if (p.startsWith('/api/v1/profile')) return ok({ profile: null });
    if (p.startsWith('/api/v1/resume/for-job')) return ok(cfg.forJobBody ?? {});
    if (p.startsWith('/api/v1/location/countries')) {
      if (cfg.countriesFail) throw new Error('countries endpoint down');
      api.countriesCalls.push(JSON.parse(opts.body || '{}'));
      return ok(cfg.countriesBody ?? { countries: [], source: 'none' });
    }
    if (p.startsWith('/api/v1/applications/snapshot')) { api.snapshots.push(JSON.parse(opts.body || '{}')); return ok({ ok: true }); }
    if (p.startsWith('/api/v1/applications')) { api.apps.push(JSON.parse(opts.body || '{}')); return ok({ ok: true }); }
    if (p.startsWith('/api/v1/settings')) return ok({ schedule_enabled: false });
    return ok({});
  }
  const sandbox = {
    console, setTimeout, clearTimeout, setInterval, clearInterval,
    URL, TextEncoder, crypto: webcrypto, fetch: fetchStub, chrome: chromeStub,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext('Math.random = () => 0;', sandbox);
  vm.runInContext(SW_SRC, sandbox, { filename: 'sw.js' });
  return {
    sandbox, api, tabs,
    fillStart: () => fillStart,
    loadStart: () => loadStart,
    fireTabComplete(tabId = 7) { for (const fn of [...tabs.updatedListeners]) fn(tabId, { status: 'complete' }); },
    startRun() { runtime.messageHandler({ type: 'AUTOAPPLY_RUN_NOW' }, {}, () => {}); },
    lastProgress() { return api.progressPosts[api.progressPosts.length - 1]; },
  };
}

async function main() {
/* ---------- S1: 等插件回填（≤120s）期间必须有心跳 ---------- */
console.log('\n[S1] 等回填 11s，期间应有心跳；中途点停止，心跳不得写回 running');
{
  const env = makeEnv({ fillDelayMs: 11000, flipControlAtFillMs: 6000 });
  env.startRun();
  await waitFor(() => env.tabs.created.length === 1, 5000);
  env.fireTabComplete();
  await waitFor(() => env.fillStart() > 0, 5000);
  const fs0 = env.fillStart();
  await waitFor(() => env.api.apps.length === 1, 20000);
  const beats = env.api.progressPosts.filter((p) => p.t > fs0 + 1000 && p.t < fs0 + 11000);
  check('回填等待期间至少 2 次心跳上报', beats.length >= 2, `实际 ${beats.length} 次`);
  check('翻转为 stopped 之前有 running 心跳', beats.some((p) => p.state === 'running' && p.t < fs0 + 6000), JSON.stringify(beats.map((p) => [p.t - fs0, p.state])));
  check('翻转为 stopped 之后有 stopped 心跳', beats.some((p) => p.state === 'stopped' && p.t > fs0 + 6000), JSON.stringify(beats.map((p) => [p.t - fs0, p.state])));
  check('翻转后没有 running 心跳（不把停止写回去）', !beats.some((p) => p.state === 'running' && p.t > fs0 + 6000), JSON.stringify(beats.map((p) => [p.t - fs0, p.state])));
  await waitFor(() => env.lastProgress() && env.lastProgress().state === 'stopped', 15000);
  const last = env.lastProgress();
  check('本轮最终状态 stopped', last && last.state === 'stopped', JSON.stringify(last));
  check('已处理 1 个、待人工 1 个', last && last.processed === 1 && last.manual === 1, JSON.stringify(last));
}

/* ---------- S2: 等标签页加载（≤30s）期间必须有心跳 ---------- */
console.log('\n[S2] 等标签页加载 11s，期间应有心跳');
{
  const env = makeEnv({ fillDelayMs: 0 });
  env.startRun();
  await waitFor(() => env.tabs.created.length === 1, 5000);
  const ls0 = env.loadStart();
  setTimeout(() => env.fireTabComplete(), 11000);
  setTimeout(() => { env.api.controlState = 'stopped'; }, 12500);
  await waitFor(() => env.api.apps.length === 1, 20000);
  const beats = env.api.progressPosts.filter((p) => p.t > ls0 + 1000 && p.t < ls0 + 11000);
  check('加载等待期间至少 2 次心跳上报', beats.length >= 2, `实际 ${beats.length} 次`);
  check('加载等待期间心跳状态 running', beats.every((p) => p.state === 'running'), JSON.stringify(beats.map((p) => [p.t - ls0, p.state])));
  await waitFor(() => env.lastProgress() && env.lastProgress().state === 'stopped', 15000);
  const last = env.lastProgress();
  check('本轮最终状态 stopped', last && last.state === 'stopped', JSON.stringify(last));
}

/* ---------- S3: 正常跑批仍能走完（回归） ---------- */
console.log('\n[S3] 快速跑批：1 个待人工职位，应以 done 结束');
{
  const env = makeEnv({ fillDelayMs: 0 });
  env.startRun();
  await waitFor(() => env.tabs.created.length === 1, 5000);
  env.fireTabComplete();
  await waitFor(() => env.lastProgress() && env.lastProgress().state === 'done', 40000);
  const last = env.lastProgress();
  check('本轮最终状态 done', last && last.state === 'done', JSON.stringify(last));
  check('已处理 1 个、待人工 1 个、成功 0、失败 0', last && last.processed === 1 && last.manual === 1 && last.submitted === 0 && last.failed === 0, JSON.stringify(last));
  check('待人工记录已上报', env.api.apps.length === 1 && env.api.apps[0].status === 'needs_manual', JSON.stringify(env.api.apps));
}

/* ---------- S4: 母版简历走 for-job 的 data_url（Issue 2） ---------- */
// 后端 for-job 在非改写分支直接回了母版 data_url；旧 sw.js 只认 tailored 分支，
// 其余一律回退到插件侧 profile.resumeFile——它可能来自旧缓存或已被剥离，
// 结果就是填表时没有简历可附、职位进待人工。修好后 for-job 的 data_url 要进
// 填表消息和快照。修复前（旧 sw.js）应失败。
console.log('\n[S4] 母版简历用 for-job 返回的 data_url');
{
  const MASTER = 'data:application/pdf;base64,JVBERi0xLjQKZmFrZS1tYXN0ZXI=';
  const env = makeEnv({ fillDelayMs: 0,
    forJobBody: { tailored: false, pdf_hash: '', filename: 'Victoria_Liu_Resume.pdf', version_id: 0, data_url: MASTER } });
  env.startRun();
  await waitFor(() => env.tabs.created.length === 1, 5000);
  env.fireTabComplete();
  await waitFor(() => env.api.apps.length === 1, 20000);
  const sent = env.tabs.fillMessages[0] || {};
  check('填表消息里带上母版简历 dataUrl', sent.profile && sent.profile.resumeFile
    && sent.profile.resumeFile.dataUrl === MASTER, JSON.stringify(sent.profile && sent.profile.resumeFile));
  const snap = env.api.snapshots[0] || {};
  check('快照里 resume_data_url 是母版', snap.resume_data_url === MASTER, JSON.stringify(snap.resume_data_url));
  await waitFor(() => env.lastProgress() && env.lastProgress().state === 'done', 40000);
}

/* ---------- S5: 职位国家解析进填表消息（Issue 3） ---------- */
// 跑批时插件先问后端要归一化国家（已存的直接用；没有时后端确定性直认，
// 认不出再调 LLM）；同一地点串只问一次；后端挂了也不许中断跑批。
console.log('\n[S5] 职位国家解析进填表消息');
{
  const PARIS1 = { ...JOB, id: 201, location: 'Paris, France' };
  const PARIS2 = { ...JOB, id: 202, location: 'Paris, France' };
  // (a) 同地点两职位：端点只问一次（第二份走本地缓存），两份填表消息都带 France
  const envA = makeEnv({ fillDelayMs: 0, jobs: [PARIS1, PARIS2],
    countriesBody: { countries: ['France'], source: 'llm' } });
  envA.startRun();
  await waitFor(() => envA.tabs.created.length === 1, 5000);
  envA.fireTabComplete();
  await waitFor(() => envA.tabs.fillMessages.length === 2, 45000);
  check('同地点只问一次端点', envA.api.countriesCalls.length === 1,
    JSON.stringify(envA.api.countriesCalls));
  check('端点请求带 job_id 和地点', envA.api.countriesCalls[0]
    && envA.api.countriesCalls[0].job_id === 201 && envA.api.countriesCalls[0].location === 'Paris, France',
    JSON.stringify(envA.api.countriesCalls[0]));
  check('两份填表消息都带 jobCountries France',
    envA.tabs.fillMessages.every((m) => JSON.stringify(m.options.jobCountries) === '["France"]'),
    JSON.stringify(envA.tabs.fillMessages.map((m) => m.options)));
  await waitFor(() => envA.lastProgress() && envA.lastProgress().state === 'done', 45000);
  // (b) 队列自带国家：不问端点，直接用
  const envB = makeEnv({ fillDelayMs: 0, jobs: [{ ...JOB, id: 203, countries: ['Canada'] }],
    countriesBody: { countries: ['France'], source: 'llm' } });
  envB.startRun();
  await waitFor(() => envB.tabs.created.length === 1, 5000);
  envB.fireTabComplete();
  await waitFor(() => envB.tabs.fillMessages.length === 1, 20000);
  check('队列自带国家时不问端点', envB.api.countriesCalls.length === 0,
    JSON.stringify(envB.api.countriesCalls));
  check('填表消息用队列自带的 Canada',
    JSON.stringify(envB.tabs.fillMessages[0].options.jobCountries) === '["Canada"]',
    JSON.stringify(envB.tabs.fillMessages[0].options));
  await waitFor(() => envB.lastProgress() && envB.lastProgress().state === 'done', 40000);
  // (c) 端点挂了：跑批照常走完，jobCountries 为空（填表侧再就地解析）
  const envC = makeEnv({ fillDelayMs: 0, countriesFail: true });
  envC.startRun();
  await waitFor(() => envC.tabs.created.length === 1, 5000);
  envC.fireTabComplete();
  await waitFor(() => envC.tabs.fillMessages.length === 1, 20000);
  check('端点挂了填表消息 jobCountries 为空',
    JSON.stringify(envC.tabs.fillMessages[0].options.jobCountries) === '[]',
    JSON.stringify(envC.tabs.fillMessages[0].options));
  await waitFor(() => envC.lastProgress() && envC.lastProgress().state === 'done', 40000);
  check('端点挂了跑批仍以 done 结束', envC.lastProgress() && envC.lastProgress().state === 'done',
    JSON.stringify(envC.lastProgress()));
}

console.log(`\n结果：${passed} 通过，${failed} 失败`);
process.exit(failed ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
