'use strict';
/* ats.js — 识别当前职位页使用的 ATS 系统 */
(function () {
  const RULES = [
    {
      ats: 'greenhouse',
      urlRe: /(^|\.)(boards\.greenhouse\.io|greenhouse\.io)/i,
      dom: ['#first_name', 'form[action*="greenhouse"]', '[data-board-token]'],
      weight: { url: 70, dom: 30 },
    },
    {
      ats: 'lever',
      urlRe: /(^|\.)lever\.co/i,
      dom: ['#lever-jobs-container', '.lever-job-title', 'a[href*="lever.co"]'],
      weight: { url: 70, dom: 30 },
    },
    {
      ats: 'ashby',
      urlRe: /(^|[^a-z0-9])ashbyhq\.com/i,
      dom: ['[data-testid="ashby-application-form"]', 'input[name="_systemfield_name"]'],
      weight: { url: 70, dom: 30 },
    },
    {
      ats: 'workday',
      urlRe: /(myworkdayjobs\.com|\.workday\.com)/i,
      dom: ['[data-automation-id]', 'div[data-uxi-widget-type="applyButton"]'],
      weight: { url: 70, dom: 30 },
    },
    {
      ats: 'icims',
      urlRe: /icims\.com/i,
      dom: ['.iCIMS_ApplicantTable', '#jsb_form'],
      weight: { url: 70, dom: 30 },
    },
  ];

  function detectATS() {
    const url = location.href;
    let best = { ats: 'unknown', confidence: 0, via: 'none' };
    for (const r of RULES) {
      let score = 0;
      let via = [];
      if (r.urlRe.test(url)) { score += r.weight.url; via.push('url'); }
      for (const sel of r.dom) {
        try {
          if (document.querySelector(sel)) { score += r.weight.dom; via.push('dom:' + sel); break; }
        } catch (e) { /* invalid selector, skip */ }
      }
      if (score > best.confidence) best = { ats: r.ats, confidence: Math.min(score, 100), via: via.join(',') || 'none' };
    }
    return best;
  }

  window.__autoapply = window.__autoapply || {};
  window.__autoapply.detectATS = detectATS;
})();
