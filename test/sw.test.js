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
  const api = { progressPosts: [], controlState: 'running', apps: [], snapshots: [] };
  const tabs = { updatedListeners: [], created: [] };
  let fillStart = 0, loadStart = 0;
  const runtime = { messageHandler: null };
  const chromeStub = {
    storage: { local: { _store: {}, async get(keys) { const out = {}; const arr = Array.isArray(keys) ? keys : [keys]; for (const k of arr) out[k] = this._store[k]; return out; }, async set(obj) { Object.assign(this._store, obj); } } },
    tabs: {
      async create() { const tab = { id: 7 }; tabs.created.push(tab); loadStart = Date.now(); return tab; },
      async remove() {},
      sendMessage(tabId, msg, cb) {
        if (msg.type === 'AUTOAPPLY_FILL') {
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
    if (p.startsWith('/api/v1/resume/for-job')) return ok({});
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

console.log(`\n结果：${passed} 通过，${failed} 失败`);
process.exit(failed ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
