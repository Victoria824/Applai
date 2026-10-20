'use strict';
/* lib/match.js — 同义词词典字段匹配（纯函数，无 DOM）
 *
 * 移植自 vesaias/JobNavigator 的 extension/lib/autofill_match.js，
 * MIT License, Copyright (c) vesaias。按 MIT 条款保留原作者信息，
 * 本文件其余部分遵循本项目许可。
 */
(function () {
  function normalizeLabel(s) {
    return (s || '')
      .toLowerCase()
      .replace(/['’]/g, "'")
      .replace(/[^a-z0-9\u4e00-\u9fff]+/g, ' ')
      .trim();
  }

  // signature 中命中某 key 的同义词时，取最长命中者（避免 "name" 抢掉 "first name"）
  function matchFieldKey(signature, fieldPatterns) {
    const sig = normalizeLabel(signature);
    if (!sig) return null;
    let best = null, bestLen = 0;
    for (const key in fieldPatterns) {
      for (const syn of fieldPatterns[key]) {
        const n = normalizeLabel(syn);
        if (!n) continue;
        if ((' ' + sig + ' ').includes(' ' + n + ' ') && n.length > bestLen) {
          best = key; bestLen = n.length;
        }
      }
    }
    return best;
  }

  function matchOption(canonicalValue, optionTexts, synonymsForKey) {
    if (!canonicalValue || !synonymsForKey) return null;
    const syns = (synonymsForKey[canonicalValue] || []).map(normalizeLabel).sort((a, b) => b.length - a.length);
    let best = null, bestLen = 0;
    optionTexts.forEach((txt, i) => {
      const n = normalizeLabel(txt);
      for (const syn of syns) {
        if (syn && n.includes(syn) && syn.length > bestLen) { best = { index: i, text: txt }; bestLen = syn.length; }
      }
    });
    return best;
  }

  function boolToOption(boolValue, optionTexts, boolSynonyms) {
    // 极性判定（词边界 + 否定感知）：整句式选项如
    // "I will not require sponsorship" 应判为 No，而不是被子串误命中。
    const NEG = [' not ', ' never ', ' don t ', ' won t ', ' can t ', ' cannot '];
    let yesIdx = -1, noIdx = -1;
    optionTexts.forEach((txt, i) => {
      const n = ' ' + normalizeLabel(txt) + ' ';
      const neg = NEG.some((t) => n.includes(t));
      const hasNo = n.includes(' no ') || n.includes(' \u5426 ') || neg;
      const hasYes = (n.includes(' yes ') || n.includes(' \u662f ') || n.includes(' \u662f\u7684 ')) && !neg;
      if (hasYes && yesIdx < 0) yesIdx = i;
      if (hasNo && noIdx < 0) noIdx = i;
    });
    const pick = boolValue ? yesIdx : noIdx;
    if (pick >= 0) return { index: pick, text: optionTexts[pick] };
    // 兜底走同义词子串匹配，但拒绝单字母子串误命中（y 命中 you、n 命中 in）
    const legacy = matchOption(boolValue ? 'true' : 'false', optionTexts, boolSynonyms);
    if (!legacy) return null;
    const syns = ((boolSynonyms || {})[boolValue ? 'true' : 'false'] || []).map(normalizeLabel);
    const n = normalizeLabel(legacy.text);
    const ok = syns.some((s) => s && (s === n || (s.length >= 2 && n.includes(s))));
    return ok ? legacy : null;
  }

  window.__autofillMatch = { normalizeLabel, matchFieldKey, matchOption, boolToOption };
})();
