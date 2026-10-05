"""matcher.py — 画像 vs 职位匹配（规则版 v1，可解释、无 LLM 成本）

计分（0-100）：
- 职位标题 60 分：目标职位词与招聘标题的词重叠度
- 地点/远程 25 分：期望地点命中，或职位远程且用户接受远程
- 公司/新鲜度 15 分：暂留（v2 接 LLM 语义匹配时启用）
阈值 40 分进入候选队列，按分排序取 TopN。
"""
import re

WORD_RE = re.compile(r"[a-z0-9+#.]+", re.I)

STOPWORDS = {"and", "or", "the", "a", "an", "of", "for", "in", "on", "at", "to",
             "senior", "sr", "junior", "jr", "lead", "principal", "staff", "i", "ii", "iii"}


def words(s: str) -> set:
    return {w.lower() for w in WORD_RE.findall(s or "") if w.lower() not in STOPWORDS}


def title_score(target_titles: list, job_title: str) -> tuple[float, str]:
    jt = words(job_title)
    if not jt or not target_titles:
        return 0.0, "无目标职位"
    best, best_t = 0.0, ""
    for t in target_titles:
        tw = words(t)
        if not tw:
            continue
        overlap = len(tw & jt) / len(tw)
        if overlap > best:
            best, best_t = overlap, t
    score = round(best * 60, 1)
    reason = f"标题匹配 '{best_t}'（重叠 {int(best*100)}%）" if best else "标题无匹配"
    return score, reason


def location_score(profile: dict, job_location: str) -> tuple[float, str]:
    prefs = [p.lower() for p in (profile.get("preferredLocations") or [])]
    remote_pref = (profile.get("remotePreference") or "any").lower()
    jl = (job_location or "").lower()
    if not jl:
        return 12.5, "职位地点未知，给一半分"
    if any(p and p in jl for p in prefs):
        return 25.0, f"地点命中期望（{job_location}）"
    if "remote" in jl and remote_pref in ("remote", "any"):
        return 25.0, "远程职位且用户接受远程"
    if "hybrid" in jl and remote_pref in ("hybrid", "any"):
        return 18.0, "混合办公，部分匹配"
    return 0.0, f"地点不匹配（{job_location}）"


def score_job(profile: dict, job: dict) -> tuple[float, list]:
    reasons = []
    ts, tr = title_score(profile.get("targetTitles") or [], job.get("title") or "")
    reasons.append(tr)
    ls, lr = location_score(profile, job.get("location") or "")
    reasons.append(lr)
    total = round(ts + ls, 1)
    return total, reasons


def build_queue(profile: dict, jobs: list, top_n=10, threshold=40.0) -> list:
    scored = []
    for job in jobs:
        s, reasons = score_job(profile, job)
        if s >= threshold:
            scored.append({**job, "score": s, "score_reasons": reasons})
    scored.sort(key=lambda j: (-j["score"], j.get("created_at", 0)))
    return scored[:top_n]
