"""backend/agents/gloria.py — Gloria / 复盘 (Review Coach).

Moved as-is from backend/app.py (multi-agent rebuild, branch `multi-agent`).
Gloria owns: Gmail 只读 OAuth, 邮件同步 (cron), 投递复盘 (digest/advice),
邮件动态, 聊天助手 (conversation CRUD + chat).

Rules (see docs/multi-agent-design.md):
- No FastAPI decorators, no Depends, no Request in this module. Route
  handlers are plain functions taking uid explicitly; app.py stays thin
  and wires routes to these functions.
- Agents communicate via DB rows, never direct cross-agent calls — except
  chatbot commands, which are executed by Rachel's run_chat_action
  (agents.rachel), imported lazily inside the call so there is no import
  cycle and no hard dependency at import time.
- Shared services (db, llm, gmail, email_sync, matcher) are imported,
  never rewritten. Gmail stays read-only OAuth.
- Coarse milestones ONLY via db.log_agent_event(uid, "gloria", ...):
  email sync finished (counts only), chat action executed (action name
  only). NEVER log email bodies, subjects, or chat message content.

Original app.py line numbers are noted on each function (branch
multi-agent, app.py @ 1669 lines at time of move).
"""
import datetime as _dt
import os
import time

from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

import db
import email_sync
import gmail
import llm
from agents.shared.push import send_web_push


# ---------- Gmail 只读 OAuth ----------
# Originals: app.py:998 / 1007 / 1017 / 1038.

def gmail_status(uid: str):
    """app.py:998 — Gmail 连接状态。"""
    t = db.get_gmail_token(uid)
    return {"connected": bool(t), "email": (t or {}).get("email", ""),
            "last_sync": (t or {}).get("last_sync", 0),
            "configured": gmail.is_configured()}


def gmail_auth_url(uid: str):
    """app.py:1007 — 生成 Gmail OAuth 授权链接。"""
    if not gmail.is_configured():
        raise HTTPException(400, "Gmail 未配置（需要 GOOGLE_CLIENT_ID/SECRET）")
    import secrets as _secrets
    state = _secrets.token_urlsafe(24)
    db.save_gmail_state(state, uid)
    return {"url": gmail.auth_url(state)}


def gmail_callback(code: str = "", state: str = "", error: str = ""):
    """app.py:1017 — Gmail OAuth 回调（无鉴权，query 参数），返回 redirect.

    Signature unchanged from app.py: already a plain function.
    """
    if error:
        return RedirectResponse("/dashboard?gmail=error", status_code=302)
    uid = db.consume_gmail_state(state)
    if not uid or not code:
        return RedirectResponse("/dashboard?gmail=error", status_code=302)
    try:
        toks = gmail.exchange_code(code)
        access = toks.get("access_token", "")
        refresh = toks.get("refresh_token", "")
        if not refresh:
            # 用户之前授权过没给 refresh_token：提示重连
            return RedirectResponse("/dashboard?gmail=norefresh", status_code=302)
        email = gmail.get_profile_email(access)
        db.save_gmail_token(uid, email, refresh)
    except Exception:
        return RedirectResponse("/dashboard?gmail=error", status_code=302)
    return RedirectResponse("/dashboard?gmail=ok", status_code=302)


def gmail_disconnect(uid: str):
    """app.py:1038 — 断开 Gmail。"""
    db.delete_gmail_token(uid)
    return {"ok": True}


# ---------- 邮件同步 (cron) ----------
# Original: app.py:1044 admin_email_sync(request: Request).
# New plain signature: the caller passes the x-cron-token request header
# value; the CRON_TOKEN check is identical to app.py.

def admin_email_sync(x_cron_token: str = ""):
    """每 4 小时跑一次的邮件同步（cron）。用 CRON_TOKEN 保护。

    Behaviour note: app.py called email_sync.sync_all(push_fn=...), which
    loops db.gmail_users() -> sync_user() and aggregates totals. That loop
    is inlined here verbatim (same calls, same per-user try/except-continue,
    same totals) so each user's finished sync can be logged as a Gloria
    milestone with its own counts — sync_all's aggregated totals carry no
    per-user uid to attribute a log row to. Return value is identical:
    {"ok": True, **totals} with the same keys sync_all returns.
    """
    token = os.environ.get("APPLAI_CRON_TOKEN", "")
    if not token or x_cron_token != token:
        raise HTTPException(403, "forbidden")
    total = {"users": 0, "checked": 0, "new": 0, "interview": 0, "rejection": 0}
    for u in db.gmail_users():
        total["users"] += 1
        try:
            s = email_sync.sync_user(u["user_id"], push_fn=send_web_push)
        except Exception:
            continue
        for k in ("checked", "new", "interview", "rejection"):
            total[k] += s.get(k, 0)
        # Coarse milestone only: counts, never subjects/bodies.
        try:
            db.log_agent_event(
                u["user_id"], "gloria", "email_sync",
                f"邮件同步完成：新增 {s.get('new', 0)} 条回复，面试 {s.get('interview', 0)} 条")
        except Exception:
            pass
    return {"ok": True, **total}


# ---------- 投递复盘 ----------
# Originals: app.py:1054 report_digest / 1083 report_advice.

def report_digest(uid: str, period: str = "weekly"):
    """app.py:1054 — 投递复盘数据。period=daily|weekly。"""
    days = 1 if period == "daily" else 7
    cutoff = time.time() - days * 86400
    apps = [a for a in db.list_applications(uid, limit=1000) if a.get("updated_at", 0) >= cutoff]
    by_status = {}
    for a in apps:
        by_status[a["status"]] = by_status.get(a["status"], 0) + 1
    submitted = by_status.get("submitted", 0)
    interviewing = by_status.get("interviewing", 0)
    rejected = by_status.get("rejected", 0)
    responded = interviewing + rejected
    # 来源分析：从 jobs 表按 source 聚合面试数
    by_source = {}
    for a in apps:
        if a["status"] in ("interviewing", "submitted"):
            src_name = "unknown"
            by_source[src_name] = by_source.get(src_name, 0) + 1
    events = [e for e in db.list_email_events(uid, limit=20) if e.get("created_at", 0) >= cutoff]
    return {"period": period, "days": days,
            "total": len(apps), "by_status": by_status,
            "submitted": submitted, "interviewing": interviewing,
            "rejected": rejected,
            "response_rate": round(responded / submitted * 100, 1) if submitted else 0,
            "events": events}


def report_advice(uid: str):
    """app.py:1083 — AI 复盘建议。"""
    apps = db.list_applications(uid, limit=200)
    prof = db.get_profile(uid) or {}
    if not llm.is_configured():
        raise HTTPException(400, "AI 未配置")
    # 汇总数据给 AI
    by_status = {}
    for a in apps:
        by_status[a["status"]] = by_status.get(a["status"], 0) + 1
    prompt = f"""你是求职顾问。根据以下投递数据，给出 3 条具体可执行的优化建议（中文，每条 30 字内）。

画像目标职位：{(prof.get("targetTitles") or [])}
投递总数：{len(apps)}
状态分布：{by_status}

只返回 JSON：{{"advice": ["建议1", "建议2", "建议3"]}}"""
    try:
        import httpx as _hx
        cfg = llm.get_config()
        r = _hx.post(f"{cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
            json={"model": cfg["model"], "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.3, "max_tokens": 300}, timeout=30)
        r.raise_for_status()
        data = llm._extract_json(r.json()["choices"][0]["message"]["content"])
        return {"advice": (data or {}).get("advice", [])[:3]}
    except Exception as e:
        raise HTTPException(500, str(e)[:200])


# ---------- 邮件动态 ----------
# Original: app.py:1282 email_events.

def email_events(uid: str):
    """app.py:1282 — 邮件事件列表。"""
    return {"events": db.list_email_events(uid)}


# ---------- 聊天助手 ----------
# Originals: app.py:1380 ChatIn / 1401 / 1406 / 1411 / 1419 / 1425.
# The three admin_chat_debug dead functions (app.py ~866/937/966) were
# NOT moved - left in app.py untouched.

class ChatIn(BaseModel):
    message: str
    history: list = Field(default_factory=list)  # [{role, content}]
    conversation_id: int = 0


CHAT_SYSTEM = """你是 Applai 的求职助手，一个友好、专业的 AI 顾问。
你掌握用户求职画像（含简历摘要）、职位匹配队列、投递进度和职位描述（JD）全文。
关于职位来源，只说真话：Applai 通过以下渠道发现职位 —— 加拿大 Job Bank（联邦政府官方招聘站）、14 家多伦多/加拿大公司的官方招聘页（Wealthsimple、1Password、StackAdapt 等，通过 Greenhouse/Lever/Ashby 公开 API）、免费聚合源 Arbeitnow 和 Remotive，以及用户手动添加的公司。绝对不要提 Indeed、LinkedIn、Glassdoor 或"人工审核"，我们没有这些。
职责：
1. 回答投递进展问题（如"今天投了几个""哪些要人工处理"），用下面的实时数据回答，不要编造。
2. 结合 JD 回答更广泛的问题：分析职位要求、对比多个职位、给面试/简历建议、评估匹配度。用户问简历分析时，用画像里的简历摘要回答，不要说"没有简历数据"。
3. 队列为空时，先看实时数据找原因（是从未抓取？还是抓到了但分数不够？），给出可操作的建议（如去"来源"页点"立即抓取"、检查画像关键词），不要说"市面上没有岗位"这种无法验证的话。
4. 回答简洁，中文为主，关键信息用条列。不要输出 JSON。
5. 数据里没有的信息要承认不知道，不要 hallucinate。
6. 你也是复盘助手：用户说"复盘一下""这周怎么样"时，用【本周复盘】和【邮件动态】给出结构化复盘：投递数、回复率、进展亮点、问题、下周建议。"""

MAX_CTX = 12000


# app.py:1401 chat_list_conversations -> chat_list_conversations(uid)
def chat_list_conversations(uid: str):
    return {"conversations": db.list_conversations(uid)}


# app.py:1406 chat_create_conversation -> chat_create_conversation(uid)
def chat_create_conversation(uid: str):
    return db.create_conversation(uid)


# app.py:1411 chat_get_messages -> chat_get_messages(uid, cid)
def chat_get_messages(uid: str, cid: int):
    if not db.get_conversation(uid, cid):
        raise HTTPException(404, "conversation not found")
    return {"messages": db.get_chat_messages(uid, cid)}


# app.py:1419 chat_delete_conversation -> chat_delete_conversation(uid, cid)
def chat_delete_conversation(uid: str, cid: int):
    db.delete_conversation(uid, cid)
    return {"ok": True}


# Queue is Caroline's — delegate to the canonical implementation.
# Why NOT matcher.build_queue: it recomputes rules-only scores, but the
# stored scores sorted here are hybrid (rules*0.35 + LLM*0.65); it also
# has no manual-queue priority and no applied-job exclusion. Using it
# would change chat-context order/scores and could resurface
# already-applied jobs.
def _queue_jobs(uid: str, top_n: int = 10) -> list:
    """待投递队列：chatbot 手动加入的（status='queued'）最优先，其余按混合
    分数排序并排除已处理过的职位 — canonical in agents.caroline."""
    from agents.caroline import _queue_jobs as _c_queue_jobs
    return _c_queue_jobs(uid, top_n)


# app.py:1425 chat -> chat(uid, body). Logic verbatim except the two
# replaced calls: _queue_jobs -> agents.caroline (delegate, see above),
# _run_chat_action -> _execute_chat_action (Rachel, lazy import).
def chat(uid: str, body: ChatIn):
    import llm as llm_mod
    msg = (body.message or "").strip()[:2000]
    if not msg:
        raise HTTPException(400, "message 为空")
    profile = db.get_profile(uid) or {}
    stats = db.stats(uid)
    apps = db.list_applications(uid, 30)

    ctx = []
    ctx.append("【用户画像】" + llm_mod._profile_text(profile)[:800])
    if profile.get("summary"):
        ctx.append("【简历摘要】" + str(profile["summary"])[:600])
    ctx.append("【统计】" + str(stats))
    # 复盘数据：本周投递、回复率、邮件动态（供"复盘一下"类问题）
    try:
        _cutoff = time.time() - 7 * 86400
        _apps7 = [a for a in db.list_applications(uid, limit=1000) if a.get("updated_at", 0) >= _cutoff]
        _bs = {}
        for _a in _apps7:
            _bs[_a["status"]] = _bs.get(_a["status"], 0) + 1
        _sub = _bs.get("submitted", 0)
        _resp = _bs.get("interviewing", 0) + _bs.get("rejected", 0)
        _rr = round(_resp / _sub * 100, 1) if _sub else 0
        _evs = [e for e in db.list_email_events(uid, limit=10) if e.get("created_at", 0) >= _cutoff]
        _evl = [f"- {e.get('kind','?')}：{e.get('subject','')[:50]}" for e in _evs]
        ctx.append(f"【本周复盘】投递{_sub} · 面试中{_bs.get('interviewing',0)} · 拒信{_bs.get('rejected',0)} · 回复率{_rr}%")
        ctx.append("【邮件动态】\n" + ("\n".join(_evl) if _evl else "暂无"))
    except Exception:
        pass
    lines = []
    for a in apps:
        lines.append(f"- {a.get('title','?')} @ {a.get('company','?')}：{a.get('status','?')}（{a.get('detail','')[:60]}）")
    ctx.append("【最近申请】\n" + ("\n".join(lines) if lines else "暂无"))
    try:
        queued = _queue_jobs(uid, 15)
        ql = [f"- {j.get('title','?')} @ {j.get('company','?')}：{j.get('score',0)}分（{j.get('location','')}）"
              for j in queued]
        ctx.append("【待投递队列（按分数排序）】\n" + ("\n".join(ql) if ql else "队列为空"))
    except Exception:
        queued = []
        ctx.append("【待投递队列】暂无")

    # JD 上下文：待人工 + 高分待投递，截断拼接到预算内
    jd_parts, used = [], sum(len(c) for c in ctx)
    manual = [a for a in apps if a.get("status") == "needs_manual"][:5]
    queued_ids = {j["id"] for j in queued[:5]}
    want = {a.get("job_id") for a in manual} | queued_ids
    for jid in want:
        job = db.get_job(jid)
        if not job or used >= MAX_CTX:
            continue
        desc = (db.job_description(job) or "")[:1500]
        if desc:
            jd_parts.append(f"【JD】{job.get('title')} @ {job.get('company')}：\n{desc}")
            used += len(jd_parts[-1])
    if jd_parts:
        ctx.append("\n\n".join(jd_parts))

    cid = body.conversation_id or 0
    history = []
    if cid and db.get_conversation(uid, cid):
        for m in db.get_chat_messages(uid, cid, 200)[-8:]:
            history.append({"role": m["role"], "content": str(m["content"])[:1500]})
        try:
            db.add_chat_message(uid, cid, "user", msg)
        except Exception:
            import traceback; traceback.print_exc()
        # 首条消息自动取标题
        if len(db.get_chat_messages(uid, cid, 2)) <= 1:
            db.rename_conversation(uid, cid, msg[:24])
    else:
        for h in (body.history or [])[-8:]:
            if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content"):
                history.append({"role": h["role"], "content": str(h["content"])[:1500]})
    history.append({"role": "user", "content": msg})

    # Chatbot actions：先检测操作意图，执行后再让 LLM 回复
    action_result = ""
    try:
        act = llm_mod.llm_detect_action(msg)
    except Exception:
        act = None
    if act and act.get("action") not in (None, "none"):
        try:
            _q = queued
        except NameError:
            _q = []
        action_result = _execute_chat_action(uid, act, _q)
        if action_result:
            ctx.append("【刚才执行的操作】\n" + action_result)

    reply = llm_mod.llm_chat(CHAT_SYSTEM + "\n\n实时数据：\n" + "\n".join(ctx)[:MAX_CTX], history)
    if not reply:
        raise HTTPException(502, "助手暂不可用，请稍后重试")
    if cid and db.get_conversation(uid, cid):
        try:
            db.add_chat_message(uid, cid, "assistant", reply)
        except Exception:
            import traceback; traceback.print_exc()
    return {"reply": reply, "conversation_id": cid}


def _execute_chat_action(uid: str, act: dict, queued: list) -> str:
    """Replacement for app.py:1529 _run_chat_action.

    The action is executed by Rachel (agents.rachel.run_chat_action),
    imported lazily at call time: no import cycle, no hard dependency at
    import time. Logs the coarse milestone here (action name only, never
    message content) when an action actually produced a result.
    Transitional guard: if the Rachel lane has not landed yet
    (ImportError), return "" so chat() still replies instead of 500ing.
    """
    try:
        from agents.rachel import run_chat_action
    except ImportError:
        return ""
    result = run_chat_action(uid, act, queued)
    if result:
        try:
            db.log_agent_event(uid, "gloria", "chat_action",
                               f"执行聊天操作：{act.get('action')}")
        except Exception:
            pass
    return result


# ---------- Agent interface (GET /api/v1/agents) ----------
# status() / recent_events() per docs/multi-agent-design.md section 7.
# "active" ONLY from real evidence (a gloria agent_events row within 1h);
# otherwise idle. Never raises.

def _newest_gloria_event(uid: str):
    try:
        events = db.list_agent_events(uid, agent="gloria", limit=1)
        return events[0] if events else None
    except Exception:
        return None


def status(uid: str) -> dict:
    """Team-page status. today_stats only from verified real DB counts
    (email_events rows); an entry appears only when its value > 0,
    otherwise [] (a fresh user with no email activity reports no stats)."""
    out = {"status": "idle", "current_task": None, "today_stats": [],
           "last_activity": None}
    try:
        newest = _newest_gloria_event(uid)
        if newest:
            created = newest.get("created_at")
            out["last_activity"] = created
            if created:
                try:
                    dt = _dt.datetime.fromisoformat(str(created))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=_dt.timezone.utc)
                    delta = _dt.datetime.now(_dt.timezone.utc) - dt
                    if _dt.timedelta(0) <= delta <= _dt.timedelta(hours=1):
                        out["status"] = "active"
                except Exception:
                    pass
        try:
            now = time.time()
            day_start = now - (now % 86400)
            week_start = now - 7 * 86400
            evs = db.list_email_events(uid, limit=200) or []
            today_new = 0
            week_interviews = 0
            for e in evs:
                try:
                    ts = float(e.get("created_at") or 0)
                except Exception:
                    continue
                if ts >= day_start:
                    today_new += 1
                if ts >= week_start and e.get("kind") == "interview":
                    week_interviews += 1
            stats = []
            if today_new:
                stats.append({"label_zh": "今日新回复",
                              "label_en": "New replies today",
                              "value": today_new})
            if week_interviews:
                stats.append({"label_zh": "本周面试",
                              "label_en": "Interviews this week",
                              "value": week_interviews})
            out["today_stats"] = stats
        except Exception:
            pass
    except Exception:
        pass
    return out


def recent_events(uid: str, limit: int = 5) -> list:
    """Newest-first gloria agent_events. Never raises."""
    try:
        return [{"event": e.get("event", ""), "summary": e.get("summary", ""),
                 "created_at": e.get("created_at"), "ref_id": e.get("ref_id")}
                for e in db.list_agent_events(uid, agent="gloria", limit=limit)]
    except Exception:
        return []
