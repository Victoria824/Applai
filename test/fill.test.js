'use strict';
/* fill.test.js — 用 jsdom 验证填写链路 v2（node test/fill.test.js） */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { JSDOM } = require('jsdom');

const EXT = path.join(__dirname, '..', 'extension', 'content');
const PROFILE = {
  firstName: 'Victoria', lastName: 'Liu', email: 'v.liu@example.com',
  phone: '+14165550132', location: 'Toronto, ON, Canada',
  linkedin: 'https://linkedin.com/in/victoria-liu', github: 'https://github.com/victoria-liu',
  website: 'https://victoria.dev', coverLetter: 'Dear Hiring Manager, ...',
  workAuth: 'I am authorized to work in Canada', needsSponsorship: false,
  resumeFile: { name: 'Victoria_Liu_Resume.pdf', size: 123456 },
};

function makeDom(html, url) {
  const dom = new JSDOM(html, { url: url || 'https://example.com/' });
  const { window } = dom;
  window.chrome = { runtime: { onMessage: { addListener() {} } } };
  // jsdom 无 DataTransfer：用最小 mock 补齐（files 可赋值的行为由用例自行在 input 上装 setter 模拟）
  const DataTransferImpl = window.DataTransfer || function MockDataTransfer() {
    const items = [];
    this.items = { add: (f) => { items.push(f); } };
    Object.defineProperty(this, 'files', { get: () => items });
  };
  const sandbox = {
    window, document: window.document, location: window.location,
    Event: window.Event, MouseEvent: window.MouseEvent, KeyboardEvent: window.KeyboardEvent,
    PointerEvent: window.PointerEvent, Node: window.Node, navigator: window.navigator, CSS: window.CSS,
    File: window.File, DataTransfer: DataTransferImpl, atob: window.atob.bind(window),
    setTimeout, clearTimeout,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const f of ['lib/match.js', 'lib/combobox.js', 'ats.js', 'fill.js', 'captcha.js']) {
    vm.runInContext(fs.readFileSync(path.join(EXT, f), 'utf8'), sandbox, { filename: f });
  }
  return window;
}

let passed = 0, failed = 0;
function check(name, cond, extra) {
  if (cond) { passed++; console.log(`  ✓ ${name}`); }
  else { failed++; console.log(`  ✗ ${name}${extra ? ' — ' + extra : ''}`); }
}

async function main() {
/* ---------- 1. Greenhouse 风格表单 ---------- */
console.log('\n[1] Greenhouse 风格表单');
{
  const w = makeDom(`<html><body><form>
    <label for="first_name">First Name *</label><input id="first_name">
    <label for="last_name">Last Name *</label><input id="last_name">
    <label for="email">Email *</label><input id="email" type="email">
    <label for="phone">Phone</label><input id="phone" type="tel">
    <label>LinkedIn URL</label><input name="job_application[answers][1][answer]">
    <label for="cover">Cover Letter</label><textarea id="cover"></textarea>
    <label>Are you legally authorized to work in Canada?</label>
    <select id="auth"><option value="">Select...</option><option>Yes</option><option>No</option></select>
    <label>Resume</label><input type="file" id="resume">
  </form></body></html>`, 'https://boards.greenhouse.io/acme/jobs/123');

  const det = w.__autoapply.detectATS();
  check('识别出 greenhouse', det.ats === 'greenhouse', JSON.stringify(det));

  const r = await w.__autoapply.fillApplication(PROFILE, {});
  check('firstName 填入', w.document.getElementById('first_name').value === 'Victoria', w.document.getElementById('first_name').value);
  check('lastName 填入', w.document.getElementById('last_name').value === 'Liu');
  check('email 填入', w.document.getElementById('email').value === 'v.liu@example.com');
  check('phone 填入', w.document.getElementById('phone').value === '+14165550132');
  check('coverLetter 经 label 匹配填入', w.document.getElementById('cover').value.includes('Dear Hiring Manager'));
  check('linkedin 经兄弟 label 匹配填入', w.document.querySelector('input[name="job_application[answers][1][answer]"]').value.includes('linkedin.com'),
    w.document.querySelector('input[name="job_application[answers][1][answer]"]').value);
  check('工作许可 select 自动选 Yes', w.document.getElementById('auth').value === 'Yes', w.document.getElementById('auth').value);
  check('简历 file input 标记为缺失（含文件名提示）',
    r.missing.some((m) => m.intent === 'resume' && /Victoria_Liu_Resume\.pdf/.test(m.reason)),
    JSON.stringify(r.missing.map((m) => m.reason)));
  check('无验证码时不中断', r.captcha.present === false);
}

/* ---------- 2. Lever 风格（单个姓名框） ---------- */
console.log('\n[2] Lever 风格表单（fullName 兜底）');
{
  const w = makeDom(`<html><body><div class="application-form">
    <label for="name">Full Name *</label><input id="name">
    <label for="email">Email *</label><input id="email">
    <label>GitHub URL</label><input id="gh">
  </div></body></html>`, 'https://jobs.lever.co/acme/abc');

  const det = w.__autoapply.detectATS();
  check('识别出 lever', det.ats === 'lever', JSON.stringify(det));

  const r = await w.__autoapply.fillApplication(PROFILE, {});
  check('fullName 拼接填入', w.document.getElementById('name').value === 'Victoria Liu',
    w.document.getElementById('name').value);
  check('github 经 label 匹配填入', w.document.getElementById('gh').value.includes('github.com'));
  check('报告含 fullName', r.filled.some((x) => x.intent === 'fullName'));
}

/* ---------- 3. 验证码中断 ---------- */
console.log('\n[3] 验证码检测与中断');
{
  const w = makeDom(`<html><body><form>
    <input id="first_name">
    <iframe src="https://www.google.com/recaptcha/api2/anchor?k=abc" title="reCAPTCHA"></iframe>
  </form></body></html>`, 'https://boards.greenhouse.io/acme/jobs/999');

  const cap = w.__autoapply.checkCaptcha();
  check('检测到 reCAPTCHA', cap.present === true && cap.type === 'reCAPTCHA', JSON.stringify(cap));

  const r = await w.__autoapply.fillApplication(PROFILE, {});
  check('有验证码时直接返回、不填写', r.captcha.present === true && r.filled.length === 0,
    `filled=${r.filled.length}`);
}

/* ---------- 4. hCaptcha / 未知站点 ---------- */
console.log('\n[4] hCaptcha 与未知 ATS');
{
  const w = makeDom(`<html><body><div class="h-captcha" data-sitekey="x"></div></body></html>`,
    'https://careers.example.com/jobs/1');
  const cap = w.__autoapply.checkCaptcha();
  check('检测到 hCaptcha 挂载点', cap.present === true, JSON.stringify(cap));
  const det = w.__autoapply.detectATS();
  check('未知站点返回 unknown', det.ats === 'unknown', JSON.stringify(det));
}

/* ---------- 5. 薪资期望 / 工作年限 ---------- */
console.log('\n[5] 薪资期望与工作年限');
{
  const w = makeDom(`<html><body><form>
    <label for="first_name">First Name</label><input id="first_name">
    <label>Expected salary</label><input id="sal">
    <label>Years of experience</label><input id="yoe">
  </form></body></html>`, 'https://boards.greenhouse.io/acme/jobs/5');

  const prof = { ...PROFILE, salaryMin: 120000, salaryMax: 150000, salaryCurrency: 'CAD', yearsExperience: 5 };
  const r = await w.__autoapply.fillApplication(prof, {});
  check('薪资期望按 label 填入', w.document.getElementById('sal').value === '120000-150000 CAD',
    w.document.getElementById('sal').value);
  check('工作年限按 label 填入', w.document.getElementById('yoe').value === '5',
    w.document.getElementById('yoe').value);
  check('报告含 salary/yearsExperience', r.filled.some((x) => x.intent === 'salary') &&
    r.filled.some((x) => x.intent === 'yearsExperience'));

  const w2 = makeDom(`<html><body><form>
    <label>Desired compensation</label><input id="sal2">
  </form></body></html>`, 'https://boards.greenhouse.io/acme/jobs/6');
  const r2 = await w2.__autoapply.fillApplication(PROFILE, {});
  check('未配置薪资时不填写薪资框', w2.document.getElementById('sal2').value === '' &&
    !r2.filled.some((x) => x.intent === 'salary'));
}

/* ---------- 6. Workday 风格（data-automation-id 容器） ---------- */
console.log('\n[6] Workday 风格字段');
{
  const w = makeDom(`<html><body><form>
    <div data-automation-id="formField-email"><label>Email Address</label><input data-automation-id="email"></div>
    <div data-automation-id="formField-phone"><label>Phone Number</label><input data-automation-id="phone"></div>
  </form></body></html>`, 'https://acme.myworkdayjobs.com/en-US/careers/job/123');

  const det = w.__autoapply.detectATS();
  check('识别出 workday', det.ats === 'workday', JSON.stringify(det));

  const r = await w.__autoapply.fillApplication(PROFILE, {});
  check('email 经 data-automation-id 签名填入',
    w.document.querySelector('[data-automation-id="email"]').value === 'v.liu@example.com',
    w.document.querySelector('[data-automation-id="email"]').value);
  check('phone 经 data-automation-id 签名填入',
    w.document.querySelector('[data-automation-id="phone"]').value === '+14165550132');
  check('报告含 email/phone', r.filled.some((x) => x.intent === 'email') && r.filled.some((x) => x.intent === 'phone'));
}

/* ---------- 7. Ashby 风格（data-field-path 包裹层） ---------- */
console.log('\n[7] Ashby 风格字段');
{
  const w = makeDom(`<html><body><form>
    <div data-field-path="phone"><h3>Phone Number</h3><input type="tel"></div>
    <div data-field-path="location"><h3>Current Location</h3><input type="text"></div>
  </form></body></html>`, 'https://jobs.ashbyhq.com/acme/abc');

  const det = w.__autoapply.detectATS();
  check('识别出 ashby', det.ats === 'ashby', JSON.stringify(det));

  const r = await w.__autoapply.fillApplication(PROFILE, {});
  check('phone 经 field-path 签名填入',
    w.document.querySelector('[data-field-path="phone"] input').value === '+14165550132');
  check('location 经 field-path 签名填入',
    w.document.querySelector('[data-field-path="location"] input').value === 'Toronto, ON, Canada');
  void r;
}

/* ---------- 8. aria-label 字段 ---------- */
console.log('\n[8] aria-label / 无 label 字段');
{
  const w = makeDom(`<html><body><form>
    <input aria-label="Mobile phone" type="tel">
    <input placeholder="Portfolio URL">
  </form></body></html>`, 'https://careers.example.com/jobs/8');

  const r = await w.__autoapply.fillApplication(PROFILE, {});
  check('aria-label 匹配 phone', w.document.querySelector('[aria-label="Mobile phone"]').value === '+14165550132');
  check('placeholder 匹配 portfolio', w.document.querySelector('[placeholder="Portfolio URL"]').value === 'https://victoria.dev');
  void r;
}

/* ---------- 9. 自定义下拉框（react-select 风格） ---------- */
console.log('\n[9] 自定义下拉框 combobox');
{
  let chosen = null;
  const w = makeDom(`<html><body><form>
    <label id="auth-q">Are you authorized to work in Canada?</label>
    <div role="combobox" aria-expanded="false" aria-controls="lb1" aria-labelledby="auth-q" tabindex="0">
      <div class="placeholder">Select...</div>
    </div>
    <div role="listbox" id="lb1" style="display:block">
      <div role="option" id="opt-yes">Yes</div>
      <div role="option" id="opt-no">No</div>
    </div>
  </form></body></html>`, 'https://acme.myworkdayjobs.com/careers/job/9');
  w.document.querySelectorAll('[role="option"]').forEach((o) => {
    o.addEventListener('click', () => { chosen = o.textContent.trim(); });
  });

  const r = await w.__autoapply.fillApplication(PROFILE, {});
  check('combobox 选中 Yes', chosen === 'Yes', `chosen=${chosen}`);
  check('报告含 workAuth', r.filled.some((x) => x.intent === 'workAuth'), JSON.stringify(r.filled));
}

/* ---------- 10. radio 单选题 ---------- */
console.log('\n[10] radio 单选题（sponsorship）');
{
  const w = makeDom(`<html><body><form>
    <fieldset><legend>Do you require sponsorship?</legend>
      <label><input type="radio" name="sp" value="yes"> Yes</label>
      <label><input type="radio" name="sp" value="no"> No</label>
    </fieldset>
  </form></body></html>`, 'https://boards.greenhouse.io/acme/jobs/10');

  const r = await w.__autoapply.fillApplication(PROFILE, {});
  const radios = w.document.querySelectorAll('input[name="sp"]');
  check('sponsorship=false 选中 No', radios[1].checked === true && radios[0].checked === false,
    `yes=${radios[0].checked} no=${radios[1].checked}`);
  check('报告含 sponsorship', r.filled.some((x) => x.intent === 'sponsorship'));
}

/* ---------- 11. 同义词：最长匹配 ---------- */
console.log('\n[11] 同义词最长匹配');
{
  const w = makeDom(`<html><body><form>
    <label>Given name</label><input id="g">
    <label>Name of applicant</label><input id="n">
  </form></body></html>`, 'https://careers.example.com/jobs/11');

  const r = await w.__autoapply.fillApplication(PROFILE, {});
  check('"Given name" → firstName', w.document.getElementById('g').value === 'Victoria',
    w.document.getElementById('g').value);
  check('"Name of applicant" → fullName（不被 firstName 抢）',
    w.document.getElementById('n').value === 'Victoria Liu',
    w.document.getElementById('n').value);
  void r;
}

/* ---------- 12. 排除词：公司名不被当成人名 ---------- */
console.log('\n[12] 排除词回归');
{
  const w = makeDom(`<html><body><form>
    <label>Company name</label><input id="c">
    <label>First name</label><input id="f">
  </form></body></html>`, 'https://careers.example.com/jobs/12');

  const r = await w.__autoapply.fillApplication(PROFILE, {});
  check('"Company name" 不被填入人名', w.document.getElementById('c').value === '',
    w.document.getElementById('c').value);
  check('"First name" 正常填入', w.document.getElementById('f').value === 'Victoria');
  void r;
}

await testResumeAutoAttach();
await testResumeFallbackNoData();
await testResumeZhAndMalformed();

console.log(`\n结果：${passed} 通过，${failed} 失败`);
process.exit(failed ? 1 : 0);
}

/* ---------- 13. 简历自动上传（DataTransfer） ---------- */
async function testResumeAutoAttach() {
console.log('\n[13] 简历自动上传（DataTransfer）');
{
  const prof = { ...PROFILE, resumeFile: { name: 'Victoria_Liu_Resume.pdf', size: 1024, dataUrl: 'data:application/pdf;base64,JVBERi0=' } };
  const w = makeDom(`<html><body><form>
    <label for="resume">Upload Resume *</label><input type="file" id="resume">
    <label for="portfolio">Portfolio files</label><input type="file" id="portfolio">
  </form></body></html>`, 'https://boards.greenhouse.io/acme/jobs/13');

  // 模拟浏览器中 input.files 可被赋值的行为，并记录事件
  const wire = (input) => {
    let files = null; const st = { input: 0, change: 0 };
    Object.defineProperty(input, 'files', { configurable: true, get: () => files, set: (v) => { files = v; } });
    input.addEventListener('input', () => st.input++);
    input.addEventListener('change', () => st.change++);
    return { get files() { return files; }, st };
  };
  const rBox = wire(w.document.getElementById('resume'));
  const pBox = wire(w.document.getElementById('portfolio'));

  const r = await w.__autoapply.fillApplication(prof, {});
  const rf = rBox.files && rBox.files[0];
  check('(a) 简历框被附上 File', !!rf && rf.name === 'Victoria_Liu_Resume.pdf' && rf.type === 'application/pdf' && rf.size > 0,
    JSON.stringify(rf && { name: rf.name, type: rf.type, size: rf.size }));
  check('(a) input+change 事件均触发', rBox.st.input === 1 && rBox.st.change === 1, JSON.stringify(rBox.st));
  check('(a) 报告记为已填', r.filled.some((x) => x.intent === 'resume'), JSON.stringify(r.filled));
  check('(b) 作品集框未被触碰', pBox.files === null && pBox.st.change === 0, JSON.stringify({ files: pBox.files, st: pBox.st }));
  check('(b) 非简历框仍进人工队列', r.missing.some((m) => m.intent === 'resume' && /Victoria_Liu_Resume\.pdf/.test(m.reason)),
    JSON.stringify(r.missing.map((m) => m.reason)));
}
}

/* ---------- 14. 无简历数据回退人工队列 ---------- */
async function testResumeFallbackNoData() {
console.log('\n[14] 无 resumeFile 时回退人工队列');
{
  const prof = { ...PROFILE };
  delete prof.resumeFile;
  const w = makeDom(`<html><body><form>
    <label for="resume">Resume</label><input type="file" id="resume">
  </form></body></html>`, 'https://boards.greenhouse.io/acme/jobs/14');
  const r = await w.__autoapply.fillApplication(prof, {});
  check('(c) 无 resumeFile 时进人工队列且不记已填',
    r.missing.some((m) => m.intent === 'resume') && !r.filled.some((x) => x.intent === 'resume'),
    JSON.stringify(r.missing));
}
}

/* ---------- 15. 中文简历框 / 异常 dataUrl 回退 ---------- */
async function testResumeZhAndMalformed() {
console.log('\n[15] 中文简历框与异常 dataUrl');
{
  const prof = { ...PROFILE, resumeFile: { name: '简历.pdf', size: 5, dataUrl: 'data:application/pdf;base64,JVBERi0=' } };
  const w = makeDom(`<html><body><form>
    <label for="cv">请上传简历（PDF 格式）</label><input type="file" id="cv">
  </form></body></html>`, 'https://jobs.ashbyhq.com/acme/abc');
  const cv = w.document.getElementById('cv');
  let files = null;
  Object.defineProperty(cv, 'files', { configurable: true, get: () => files, set: (v) => { files = v; } });
  const r = await w.__autoapply.fillApplication(prof, {});
  check('中文「简历」标签命中并自动上传', !!(files && files[0] && files[0].name === '简历.pdf'),
    JSON.stringify(files && files[0] && files[0].name));

  const prof2 = { ...PROFILE, resumeFile: { name: 'x.pdf', size: 1, dataUrl: 'not-a-data-url' } };
  const w2 = makeDom(`<html><body><form>
    <label>Resume</label><input type="file" id="r2">
  </form></body></html>`, 'https://boards.greenhouse.io/acme/jobs/15');
  const r2 = await w2.__autoapply.fillApplication(prof2, {});
  check('非法 dataUrl 回退人工队列',
    r2.missing.some((m) => m.intent === 'resume') && !r2.filled.some((x) => x.intent === 'resume'),
    JSON.stringify(r2.missing));
  void r;
}
}

main().catch((e) => { console.error(e); process.exit(1); });
