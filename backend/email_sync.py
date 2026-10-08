"""email_sync.py — Gmail 求职邮件同步（cron 每 4 小时跑一次）。

流程：拉最近求职邮件 → Qwen 分类（面试/拒信/其他）→ 关联申请 → 更新状态 → 面试推送。
"""
import time

import db
import gmail
import llm


def _match_application(user_id: str, company: str, job_title: str) -> int:
    """按公司名/职位名模糊匹配申请，返回 job_id（0=没匹配到）。"""
    if not company and not job_title:
        return 0
    apps = db.list_applications(user_id, limit=500)
    company_l = (company or "").lower()
    title_l = (job_title or "").lower()
    best, best_score = 0, 0
    for a in apps:
        score = 0
        ac, at = (a.get("company") or "").lower(), (a.get("title") or "").lower()
        if company_l and ac and (company_l in ac or ac in company_l):
            score += 2
        if title_l and at and (title_l in at or at in title_l):
            score += 1
        if score > best_score:
            best, best_score = a["job_id"], score
    return best


def sync_user(user_id: str, push_fn=None) -> dict:
    """同步一个用户的邮件。返回统计。"""
    stats = {"checked": 0, "new": 0, "interview": 0, "rejection": 0}
    tok = db.get_gmail_token(user_id)
    if not tok:
        return stats
    try:
        access = gmail.refresh_access_token(tok["refresh_token"])["access_token"]
    except Exception:
        return stats
    try:
        msgs = gmail.list_recent(access, gmail.JOB_QUERY, max_results=20)
    except Exception:
        return stats
    for m in msgs:
        stats["checked"] += 1
        try:
            em = gmail.get_message(access, m["id"])
        except Exception:
            continue
        cls = llm.llm_classify_email(em["from"], em["subject"], em["body"] or em["snippet"])
        if not cls:
            continue
        kind = cls.get("kind", "other")
        if kind not in ("interview", "rejection"):
            # 只记录面试和拒信，其他跳过
            continue
        company = cls.get("company", "")
        job_title = cls.get("job_title", "")
        job_id = _match_application(user_id, company, job_title)
        summary = cls.get("summary", "")[:200]
        is_new = db.add_email_event(user_id, m["id"], kind, company, job_title,
                                    em["subject"][:200], summary, job_id)
        if not is_new:
            continue
        stats["new"] += 1
        # 自动更新申请状态
        if job_id:
            new_status = "interviewing" if kind == "interview" else "rejected"
            db.record_application(user_id, job_id, new_status,
                                  f"邮件自动识别：{em['subject'][:100]}")
        if kind == "interview":
            stats["interview"] += 1
            if push_fn:
                try:
                    push_fn(user_id, "🎉 收到面试邀请",
                            f"{company or ''} {job_title or ''}".strip()[:100] or em["subject"][:100])
                except Exception:
                    pass
        else:
            stats["rejection"] += 1
    db.touch_gmail_sync(user_id)
    return stats


def sync_all(push_fn=None) -> dict:
    """所有 Gmail 用户跑一遍。"""
    total = {"users": 0, "checked": 0, "new": 0, "interview": 0, "rejection": 0}
    for u in db.gmail_users():
        total["users"] += 1
        try:
            s = sync_user(u["user_id"], push_fn)
        except Exception:
            continue
        for k in ("checked", "new", "interview", "rejection"):
            total[k] += s.get(k, 0)
    return total
