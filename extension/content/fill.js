'use strict';
/* fill.js — 表单自动填写引擎 v2
 *
 * 设计借鉴 vesaias/JobNavigator (MIT License)：
 *  - 富 label 签名：label[for] / aria-label / aria-labelledby / 祖先 label /
 *    name / id / placeholder / fieldset legend /
 *    Workday data-automation-id 容器 / Ashby data-field-path 包裹层 /
 *    前一个兄弟 label（本项目补充）
 *  - 同义词词典 + 最长匹配（lib/match.js）
 *  - 自定义下拉框（Workday / react-select / Oracle JET）由 lib/combobox.js 驱动
 *    （移植自 JobNavigator，原作者 strelov1/freehire，MIT）
 */
(function () {
  const TEXT_CSS = 'input:not([type="hidden"]):not([type="file"]):not([type="submit"]):not([type="button"]):not([type="checkbox"]):not([type="radio"]), textarea, select';
  const COMBO_CSS = '[role="combobox"], [aria-autocomplete], [aria-haspopup="listbox"]';

  /* 规范字段 key → 同义词表（中英）。匹配取最长命中，避免 "name" 抢 "first name"。 */
  const FIELD_SYNONYMS = {
    firstName: ['first name', 'given name', 'forename', '名字'],
    lastName: ['last name', 'family name', 'surname', 'last', '姓氏'],
    fullName: ['full name', 'your name', 'applicant name', 'candidate name', 'name', '姓名'],
    email: ['email address', 'e-mail', 'email', '邮箱', '电子邮件'],
    phone: ['phone number', 'mobile number', 'telephone', 'cell phone', 'phone', 'mobile', 'tel', '电话', '手机', '联系电话'],
    location: ['current location', 'location', 'city', 'address', '所在地', '现居地', '城市', '地址'],
    linkedin: ['linkedin url', 'linkedin profile', 'linkedin'],
    github: ['github url', 'github profile', 'github'],
    portfolio: ['portfolio url', 'personal website', 'website url', 'website', 'blog', '作品集', '个人网站'],
    coverLetter: ['cover letter', '求职信'],
    salary: ['salary expectation', 'expected salary', 'desired salary', 'desired compensation', 'compensation expectation', 'salary range', 'expected compensation', '期望薪资', '期望年薪', '薪资期望'],
    yearsExperience: ['years of experience', 'years experience', 'total experience', '工作年限', '相关工作经验'],
    workAuth: ['authorized to work', 'legally authorized', 'eligible to work', 'work authorization', 'authorised to work', '工作许可'],
    sponsorship: ['require sponsorship', 'need sponsorship', 'visa sponsorship', 'sponsorship required', 'sponsorship', '签证担保', '工作签证担保'],
    relocate: ['willing to relocate', 'relocate', 'relocation', '是否愿意搬迁'],
    startDate: ['start date', 'earliest start', 'available to start', '到岗时间'],
    referral: ['how did you hear', 'referral', 'referred by', '推荐人', '渠道'],
  };
  const BOOL_SYNONYMS = {
    true: ['yes', 'y', 'true', '是', '是的', '愿意'],
    false: ['no', 'n', 'false', '否', '不是', '不愿意'],
  };
  const BOOL_KEYS = new Set(['workAuth', 'sponsorship', 'relocate']);

  /* 排除词：签名里出现这些词时，即使命中同义词也不认领（防 "company name" 被当成人名） */
  const KEY_EXCLUSIONS = {
    firstName: ['company', 'employer', 'business', 'referral', 'reference', 'emergency'],
    lastName: ['company', 'employer', 'business', 'referral', 'reference', 'emergency'],
    fullName: ['company', 'employer', 'business', 'referral', 'reference', 'emergency'],
  };

  function matchKey(signature) {
    const M = window.__autofillMatch;
    const key = M.matchFieldKey(signature, FIELD_SYNONYMS);
    if (!key) return null;
    const sig = M.normalizeLabel(signature);
    const excl = KEY_EXCLUSIONS[key] || [];
    for (const w of excl) {
      if ((' ' + sig + ' ').includes(' ' + M.normalizeLabel(w) + ' ')) return null;
    }
    return key;
  }

  function formatSalary(p) {
    if (!p || (!p.salaryMin && !p.salaryMax)) return '';
    const cur = p.salaryCurrency || 'CAD';
    if (p.salaryMin && p.salaryMax) return `${p.salaryMin}-${p.salaryMax} ${cur}`;
    if (p.salaryMin) return `${p.salaryMin}+ ${cur}`;
    return `up to ${p.salaryMax} ${cur}`;
  }

  function valueForKey(key, p) {
    switch (key) {
      case 'firstName': return p.firstName || '';
      case 'lastName': return p.lastName || '';
      case 'fullName': return [p.firstName, p.lastName].filter(Boolean).join(' ');
      case 'email': return p.email || '';
      case 'phone': return p.phone || '';
      case 'location': return p.location || '';
      case 'linkedin': return p.linkedin || '';
      case 'github': return p.github || '';
      case 'portfolio': return p.website || '';
      case 'coverLetter': return p.coverLetter || '';
      case 'salary': return formatSalary(p);
      case 'yearsExperience': return p.yearsExperience || '';
      case 'workAuth':
        return (/canada/i.test(p.workAuth || '') || /authori[sz]ed/i.test(p.workAuth || '')) ? 'yes' : '';
      case 'sponsorship': return p.needsSponsorship ? 'yes' : 'no';
      case 'relocate': return '';
      case 'startDate': return '';
      case 'referral': return '';
      default: return '';
    }
  }

  /* ---------- 富 label 签名 ---------- */
  function fieldSignature(el) {
    const parts = [];
    const push = (s) => { if (s && s.trim()) parts.push(s.trim()); };
    try {
      if (el.id) {
        const esc = (window.CSS && window.CSS.escape) ? window.CSS.escape(el.id) : el.id;
        const forLbl = document.querySelector(`label[for="${esc}"]`);
        if (forLbl) push(forLbl.textContent);
      }
    } catch (e) { /* ignore */ }
    if (el.getAttribute) {
      push(el.getAttribute('aria-label'));
      const alby = el.getAttribute('aria-labelledby');
      if (alby) alby.split(/\s+/).forEach((id) => { const n = document.getElementById(id); if (n) push(n.textContent); });
    }
    const anc = el.closest && el.closest('label');
    if (anc) push(anc.textContent);
    // 前一个兄弟 label（无 for 属性的常见写法）
    let sib = el.previousElementSibling;
    while (sib) {
      if (sib.tagName === 'LABEL') { push(sib.textContent); break; }
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(sib.tagName)) break;
      sib = sib.previousElementSibling;
    }
    if (el.name) push(el.name);
    if (el.id) push(el.id);
    if (el.placeholder) push(el.placeholder);
    const fs = el.closest && el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg) push(lg.textContent); }
    // Workday：问题文本在 formField-* 容器里
    const ff = el.closest && el.closest('[data-automation-id^="formField-"]');
    if (ff) {
      const lbl = ff.querySelector('label, legend');
      if (lbl) push(lbl.textContent);
      push(ff.getAttribute('data-automation-id'));
    }
    if (el.getAttribute && el.getAttribute('data-automation-id')) push(el.getAttribute('data-automation-id'));
    // Ashby：[data-field-path] 包裹层带问题文本
    const dfp = el.closest && el.closest('[data-field-path]');
    if (dfp) {
      const q = dfp.querySelector('label:not([for]), legend, [class*="title" i], [class*="label" i], [class*="question" i], h1, h2, h3, h4');
      if (q && q.textContent) push(q.textContent);
      push(dfp.getAttribute('data-field-path'));
    }
    return parts.join(' ').replace(/\s+/g, ' ').trim();
  }

  function shortLabel(sig) { return (sig || '').slice(0, 60) || '(unknown field)'; }

  /* ---------- 可见性 ---------- */
  function isVisible(el) {
    if (!el || el.disabled) return false;
    if (el.type === 'hidden') return false;
    if (typeof el.checkVisibility === 'function') {
      try { if (!el.checkVisibility()) return false; } catch (e) { /* fall through */ }
    } else {
      // 无 layout 环境（测试）兜底：只看显式隐藏样式
      const st = el.getAttribute('style') || '';
      if (/display\s*:\s*none|visibility\s*:\s*hidden/i.test(st)) return false;
      if (el.closest && el.closest('[hidden]')) return false;
    }
    return true;
  }

  /* ---------- 字段发现 ---------- */
  function discoverFields(root) {
    root = root || document;
    const out = [];
    const seen = new Set();
    const cb = window.__autofillCombobox;
    root.querySelectorAll(`${TEXT_CSS}, ${COMBO_CSS}`).forEach((el) => {
      if (seen.has(el) || !isVisible(el)) return;
      seen.add(el);
      let kind = 'text';
      if (el.tagName === 'TEXTAREA') kind = 'textarea';
      else if (el.tagName === 'SELECT') kind = 'select';
      else if (cb && cb.isComboWidget(el)) kind = 'combo';
      out.push({ el, kind, sig: fieldSignature(el) });
    });
    return out;
  }

  /* ---------- 赋值（含 React 受控组件兼容） ---------- */
  function setNativeValue(el, value) {
    const proto = el instanceof window.HTMLTextAreaElement
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function mark(el, ok) {
    try {
      el.style.outline = ok ? '2px solid #10b981' : '2px solid #ef4444';
      el.style.outlineOffset = '1px';
    } catch (e) { /* ignore */ }
  }

  function fillSelect(el, value, key) {
    const M = window.__autofillMatch;
    const texts = Array.from(el.options).map((o) => (o.text || '').trim());
    let idx = -1;
    if (BOOL_KEYS.has(key)) {
      const m = M.boolToOption(value === 'yes', texts, BOOL_SYNONYMS);
      if (m) idx = m.index;
    } else {
      const nv = M.normalizeLabel(String(value));
      texts.forEach((t, i) => {
        const n = M.normalizeLabel(t);
        if (idx === -1 && n && nv && (n === nv || n.includes(nv) || nv.includes(n))) idx = i;
      });
    }
    if (idx < 0) return false;
    el.selectedIndex = idx;
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  }

  async function fillCombo(widget, value, key) {
    const cb = window.__autofillCombobox;
    const M = window.__autofillMatch;
    if (!cb || !M) return false;
    const isBool = BOOL_KEYS.has(key);
    const pick = (texts) => {
      if (isBool) {
        const m = M.boolToOption(value === 'yes', texts, BOOL_SYNONYMS);
        return m ? m.index : -1;
      }
      const nv = M.normalizeLabel(String(value));
      let best = -1, bestLen = 0;
      texts.forEach((t, i) => {
        const n = M.normalizeLabel(t);
        const overlap = n && nv && (n.includes(nv) || nv.includes(n)) ? Math.min(n.length, nv.length) : 0;
        if (overlap > bestLen) { best = i; bestLen = overlap; }
      });
      return best;
    };
    const res = await cb.fillCombobox(widget, pick, isBool ? '' : String(value));
    return res === 'filled';
  }

  /* ---------- radio 组（是否类问题常见形态） ---------- */
  function radioOptionLabel(radio) {
    if (radio.getAttribute && radio.getAttribute('aria-label')) return radio.getAttribute('aria-label');
    const anc = radio.closest && radio.closest('label');
    if (anc) return anc.textContent;
    try {
      if (radio.id) {
        const esc = (window.CSS && window.CSS.escape) ? window.CSS.escape(radio.id) : radio.id;
        const lb = document.querySelector(`label[for="${esc}"]`);
        if (lb) return lb.textContent;
      }
    } catch (e) { /* ignore */ }
    return radio.value || '';
  }

  function groupSignature(radios) {
    const first = radios[0];
    const container = (first.closest && (first.closest('fieldset') || first.closest('[data-automation-id^="formField-"]') || first.closest('[data-field-path]') || first.parentElement));
    const parts = [];
    if (container) {
      const q = container.querySelector('legend, label:not([for]), [class*="question" i], h1, h2, h3, h4');
      if (q) parts.push(q.textContent);
      if (container.getAttribute) {
        parts.push(container.getAttribute('data-automation-id') || '');
        parts.push(container.getAttribute('data-field-path') || '');
      }
    }
    return parts.join(' ').replace(/\s+/g, ' ').trim();
  }

  function fillRadioGroups(profile, report, filledKeys) {
    const M = window.__autofillMatch;
    const groups = new Map();
    document.querySelectorAll('input[type="radio"]').forEach((r) => {
      if (!isVisible(r)) return;
      let gkey = r.name;
      if (!gkey) {
        const c = r.closest && (r.closest('fieldset') || r.closest('[data-field-path]') || r.parentElement);
        gkey = '__c__' + (c ? Array.prototype.indexOf.call(c.parentElement ? c.parentElement.children : [], c) : Math.random());
      }
      if (!groups.has(gkey)) groups.set(gkey, []);
      groups.get(gkey).push(r);
    });
    for (const radios of groups.values()) {
      const sig = groupSignature(radios);
      const key = matchKey(sig);
      if (!key || !BOOL_KEYS.has(key) || filledKeys.has(key)) continue;
      const value = valueForKey(key, profile);
      if (value !== 'yes' && value !== 'no') {
        report.needsReview.push({ intent: key, label: shortLabel(sig), reason: '单选题需人工确认作答' });
        filledKeys.add(key);
        continue;
      }
      const labels = radios.map(radioOptionLabel);
      const m = M.boolToOption(value === 'yes', labels, BOOL_SYNONYMS);
      if (m && radios[m.index]) {
        radios[m.index].click();
        mark(radios[m.index], true);
        filledKeys.add(key);
        report.filled.push({ intent: key, label: shortLabel(sig) });
      } else {
        radios.forEach((r) => mark(r, false));
        report.needsReview.push({ intent: key, label: shortLabel(sig), reason: '单选选项无法匹配' });
        filledKeys.add(key);
      }
    }
  }

  /* ---------- 主流程 ---------- */
  async function fillApplication(profile, options) {
    options = options || {};
    const M = window.__autofillMatch;
    const report = {
      ats: (window.__autoapply.detectATS && window.__autoapply.detectATS().ats) || 'unknown',
      jobTitle: (document.title || '').slice(0, 120),
      filled: [], missing: [], needsReview: [],
      captcha: window.__autoapply.checkCaptcha ? window.__autoapply.checkCaptcha() : { present: false },
      autoSubmitted: false,
    };
    if (!M) { report.missing.push({ intent: 'engine', label: '匹配库', reason: 'lib/match.js 未加载' }); return report; }

    // 验证码优先：直接停
    if (report.captcha.present) return report;

    const filledKeys = new Set();
    const fields = discoverFields();

    for (const f of fields) {
      const key = matchKey(f.sig);
      if (!key || filledKeys.has(key)) continue;
      const value = valueForKey(key, profile);
      if (value === '' || value == null) continue;
      let ok = false;
      try {
        if (f.kind === 'select') ok = fillSelect(f.el, value, key);
        else if (f.kind === 'combo') ok = await fillCombo(f.el, value, key);
        else { setNativeValue(f.el, String(value)); ok = true; }
      } catch (e) { ok = false; }
      if (ok) {
        mark(f.el, true);
        filledKeys.add(key);
        report.filled.push({ intent: key, label: shortLabel(f.sig) });
      } else {
        mark(f.el, false);
        report.missing.push({ intent: key, label: shortLabel(f.sig), reason: '无法填入（选项无匹配或控件无响应）' });
        filledKeys.add(key);
      }
    }

    // radio 组
    fillRadioGroups(profile, report, filledKeys);

    // bool 类 key 有值但没填上 → 标人工（避免静默跳过）
    for (const key of BOOL_KEYS) {
      if (!filledKeys.has(key)) {
        const v = valueForKey(key, profile);
        if (v === 'yes' || v === 'no') {
          report.needsReview.push({ intent: key, label: key, reason: '未找到对应问题，请人工确认' });
        }
      }
    }

    // 简历上传：只能高亮，提示手动
    const fileInputs = Array.from(document.querySelectorAll('input[type="file"]')).filter(isVisible);
    if (fileInputs.length) {
      fileInputs.forEach((f) => mark(f, false));
      const rn = profile.resumeFile && profile.resumeFile.name ? `（画像中的简历：${profile.resumeFile.name}）` : '';
      report.missing.push({ intent: 'resume', label: '简历上传', reason: `浏览器安全限制：插件无法代选文件，请手动点击红色框选择简历${rn}` });
    } else {
      report.needsReview.push({ intent: 'resume', label: '简历上传', reason: '未找到文件上传框，请确认页面' });
    }

    // EEO / 自愿披露：默认不动，标人工
    const eeoHit = Array.from(document.querySelectorAll('label, legend')).some((lb) =>
      /equal employment|voluntary self-identification|diversity survey/i.test(lb.textContent || ''));
    if (eeoHit) {
      report.needsReview.push({ intent: 'eeo', label: 'EEO/多样性问卷', reason: '默认不自动作答，请人工处理' });
    }

    // 自动提交（默认关闭；原型阶段仅记录意图，不自动点）
    if (options.autoSubmit && !report.missing.length && !report.needsReview.length) {
      const submitBtn = document.querySelector('button[type="submit"], input[type="submit"]');
      if (submitBtn) {
        report.needsReview.push({ intent: 'submit', label: '提交', reason: '已就绪，待用户点击提交按钮（原型不自动点）' });
      }
    }

    return report;
  }

  window.__autoapply = window.__autoapply || {};
  window.__autoapply.fillApplication = fillApplication;
  window.__autoapply.FIELD_SYNONYMS = FIELD_SYNONYMS;
})();
