"""app.py — Applai 后端：画像 / 职位 / 匹配队列 / 投递记录 / 网页 Dashboard

运行：cd backend && pip install -r requirements.txt && uvicorn app:app --port 8000
"""
import os
import time
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

import db
from agents import amy, caroline, gloria, jennifer, rachel

SESSION_COOKIE = "aap_session"


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0] if fwd else (request.client.host if request.client else "?")).strip()


# 登录/注册限流：每 IP 每分钟最多 10 次
_attempts: dict = {}


def _rate_limit(ip: str, limit: int = 10, window: int = 60):
    now = time.time()
    arr = [t for t in _attempts.get(ip, []) if now - t < window]
    if len(arr) >= limit:
        raise HTTPException(429, "尝试过于频繁，请稍后再试")
    arr.append(now)
    _attempts[ip] = arr


def get_current_user(request: Request) -> dict:
    """鉴权：优先 Bearer Token（插件/API），其次 session cookie（网页）。"""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        u = db.get_api_token_user(auth[7:].strip())
        if u:
            return u
    token = request.cookies.get(SESSION_COOKIE)
    u = db.get_session_user(token) if token else None
    if not u:
        raise HTTPException(401, "login required")
    return u


def _set_session_cookie(resp: Response, token: str):
    # 生产默认 Secure；测试环境可设 APPLAI_COOKIE_SECURE=0（httpx 不发 Secure cookie）
    secure = os.environ.get("APPLAI_COOKIE_SECURE", "1") == "1"
    resp.set_cookie(SESSION_COOKIE, token, max_age=db.SESSION_DAYS * 86400,
                    httponly=True, secure=secure, samesite="lax", path="/")

app = FastAPI(title="Applai API", version="0.2.0")


@app.on_event("startup")
def _startup_migrate():
    """启动时跑一次迁移，首个请求不再背迁移开销；同时启动后台打分线程。"""
    try:
        db.ensure_migrated()
    except Exception:
        pass
    jennifer.start_background_scorer()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://applai-backend.fly.dev"], allow_methods=["*"], allow_headers=["*"],
)


@app.get("/health")
def health():
    import llm as llm_mod
    return {"ok": True, "version": "0.2.0", "llm": llm_mod.is_configured()}


@app.put("/api/v1/profile")
def put_profile(body: jennifer.ProfileIn, user: dict = Depends(get_current_user)):
    # 2026-11-11 Victoria's call: profile saves reset scores + keywords cache.
    return jennifer.put_profile_with_reset(user["app_user_id"], body)


@app.get("/api/v1/profile")
def get_profile(user: dict = Depends(get_current_user)):
    return jennifer.get_profile(user["app_user_id"])


@app.get("/api/v1/profiles")
def list_profiles(kind: str = "job", user: dict = Depends(get_current_user)):
    return jennifer.list_profiles(user["app_user_id"], kind)


@app.post("/api/v1/profiles")
def create_profile(body: jennifer.ProfileCreateIn, user: dict = Depends(get_current_user)):
    return jennifer.create_profile(user["app_user_id"], body)


@app.put("/api/v1/profiles/{pid}")
def update_profile(pid: int, body: dict, user: dict = Depends(get_current_user)):
    return jennifer.update_profile(user["app_user_id"], pid, body)


@app.delete("/api/v1/profiles/{pid}")
def delete_profile(pid: int, user: dict = Depends(get_current_user)):
    return jennifer.delete_profile(user["app_user_id"], pid)


@app.post("/api/v1/profiles/{pid}/activate")
def activate_profile(pid: int, user: dict = Depends(get_current_user)):
    return jennifer.activate_profile(user["app_user_id"], pid)


@app.get("/api/v1/jobs/lookup")
def lookup_job(url: str, user: dict = Depends(get_current_user)):
    """按 URL 查职位（插件 Job Bank 面板用）。"""
    return amy.lookup_job(user["app_user_id"], url)


@app.post("/api/v1/jobs")
def add_job(body: amy.JobIn, user: dict = Depends(get_current_user)):
    return amy.add_job(user["app_user_id"], body)


@app.get("/api/v1/sources")
def get_sources(user: dict = Depends(get_current_user)):
    return amy.get_sources(user["app_user_id"])


@app.post("/api/v1/sources")
def add_source(body: amy.SourceIn, user: dict = Depends(get_current_user)):
    return amy.add_source(user["app_user_id"], body)


@app.delete("/api/v1/sources/{sid}")
def delete_source(sid: int, user: dict = Depends(get_current_user)):
    return amy.delete_source(user["app_user_id"], sid)


@app.post("/api/v1/jobs/discover/auto")
def discover_auto(user: dict = Depends(get_current_user)):
    """智能抓取：立即返回任务 ID，后台执行，前端轮询进度。"""
    return amy.discover_auto(user["app_user_id"])


@app.get("/api/v1/jobs/discover/status/{task_id}")
def discover_status(task_id: str, user: dict = Depends(get_current_user)):
    return amy.discover_status(user["app_user_id"], task_id)


@app.post("/api/v1/jobs/discover/stop")
def discover_stop(user: dict = Depends(get_current_user)):
    """停止当前正在跑的抓取（来源页停止按钮）：只翻任务状态，后台线程在下个阶段边界退出。"""
    return amy.discover_stop(user["app_user_id"])


@app.get("/api/v1/jobs/discover/latest")
def latest_fetch(user: dict = Depends(get_current_user)):
    """最新一次已完成抓取的职位预览（来源页用）：只读，无副作用。"""
    return amy.get_latest_fetch(user["app_user_id"])


@app.get("/api/v1/settings")
def get_user_settings(user: dict = Depends(get_current_user)):
    return rachel.get_user_settings(user["app_user_id"])


@app.put("/api/v1/settings")
def put_user_settings(body: rachel.SettingsIn, user: dict = Depends(get_current_user)):
    return rachel.put_user_settings(user["app_user_id"], body)


@app.get("/api/v1/settings/keywords")
def get_keywords(user: dict = Depends(get_current_user)):
    return jennifer.get_keywords(user["app_user_id"])


@app.post("/api/v1/settings/keywords/refresh")
def refresh_keywords(user: dict = Depends(get_current_user)):
    """强制重新生成关键词（画像大改后调用）。"""
    return jennifer.refresh_keywords(user["app_user_id"])


@app.post("/api/v1/jobs/discover")
def discover(body: amy.DiscoverIn, user: dict = Depends(get_current_user)):
    """从 ATS 公开 API 批量抓取职位，去重入库并打分。"""
    return amy.discover(user["app_user_id"], body)


@app.get("/api/v1/queue")
def get_queue(user: dict = Depends(get_current_user), top_n: int = 10):
    """今日投递队列：只读，无副作用。"""
    return caroline.get_queue(user["app_user_id"], top_n)


@app.post("/api/v1/applications/snapshot")
def post_snapshot(body: caroline.SnapshotIn, user: dict = Depends(get_current_user)):
    """投递留痕：保存某次投递的快照（表单填写、薪资、求职信、简历）。"""
    return caroline.post_snapshot(user["app_user_id"], body)


@app.post("/api/v1/applications/retry")
def retry_application(body: caroline.RetryIn, user: dict = Depends(get_current_user)):
    """把一条「失败」的投递记录删掉，职位随即回到队列等下次投递（只动 failed 状态）。"""
    return caroline.retry_application(user["app_user_id"], body)


@app.post("/api/v1/run-progress")
def post_run_progress(body: caroline.RunProgressIn, user: dict = Depends(get_current_user)):
    """插件投递进度上报（每步一次，失败不影响投递本身）。"""
    return caroline.post_run_progress(user["app_user_id"], body)


@app.get("/api/v1/run-progress")
def read_run_progress(user: dict = Depends(get_current_user)):
    return caroline.read_run_progress(user["app_user_id"])


@app.get("/api/v1/applications/{job_id}/snapshot")
def get_snapshot(job_id: int, user: dict = Depends(get_current_user)):
    return caroline.get_snapshot(user["app_user_id"], job_id)


@app.get("/api/v1/resume-blob/{file_hash}")
def get_resume_blob(file_hash: str, user: dict = Depends(get_current_user)):
    return caroline.get_resume_blob(user["app_user_id"], file_hash)


@app.post("/api/v1/applications")
def post_application(body: caroline.ApplicationIn, user: dict = Depends(get_current_user)):
    return caroline.post_application(user["app_user_id"], body)


@app.get("/api/v1/applications")
def get_applications(user: dict = Depends(get_current_user), limit: int = 200):
    return caroline.get_applications(user["app_user_id"], limit)


@app.get("/api/v1/stats")
def get_stats(user: dict = Depends(get_current_user)):
    return caroline.get_stats(user["app_user_id"])


# ---------- 多智能体团队状态 / Multi-agent team status ----------
_AGENT_ROSTER = [
    {"id": "jennifer", "name": "Jennifer", "role_zh": "简历与市场分析", "role_en": "Resume & Market Analysis"},
    {"id": "amy", "name": "Amy", "role_zh": "职位搜罗", "role_en": "Job Hunter"},
    {"id": "caroline", "name": "Caroline", "role_zh": "填表投递", "role_en": "Application Specialist"},
    {"id": "gloria", "name": "Gloria", "role_zh": "复盘", "role_en": "Review Coach"},
    {"id": "rachel", "name": "Rachel", "role_zh": "总管·排班", "role_en": "Supervisor & Scheduler"},
]


def _agent_recent_events(uid: str, agent_id: str, limit: int = 5) -> list:
    """db.agent_events → endpoint shape. Never raises."""
    try:
        return [{"event": e.get("event", ""), "summary": e.get("summary", ""),
                 "created_at": e.get("created_at"), "ref_id": e.get("ref_id")}
                for e in db.list_agent_events(uid, agent=agent_id, limit=limit)]
    except Exception:
        return []


def _agent_active_evidence(uid: str, agent_id: str) -> tuple[bool, str | None]:
    """Real evidence only — 'active' 必须有真实依据，不可臆造：
    Amy: 有 running 且 20 分钟内更新过的 discover 任务；
    Caroline: run_progress state=running 且 20 分钟内更新过；
    任何 agent: 1 小时内有 agent_events 记录。
    Returns (active, current_task)."""
    import datetime as _dt
    if agent_id == "amy":
        try:
            conn = db.get_db()
            row = conn.execute(
                "SELECT stage, updated_at FROM discover_tasks WHERE user_id=? AND status='running' "
                "ORDER BY updated_at DESC LIMIT 1", (uid,)).fetchone()
            conn.close()
            if row:
                d = dict(row)
                if d.get("updated_at") and time.time() - float(d["updated_at"]) <= 20 * 60:
                    stage = (d.get("stage") or "").strip()
                    return True, stage or "正在抓取职位…"
        except Exception:
            pass
    if agent_id == "caroline":
        try:
            prog = db.get_run_progress(uid) or {}
            if prog.get("state") == "running":
                upd = prog.get("updated_at") or 0
                if not upd or time.time() - float(upd) <= 20 * 60:
                    title = (prog.get("current_title") or "").strip()
                    return True, f"正在投递：{title}" if title else "正在跑批投递…"
        except Exception:
            pass
    # 1 小时内的 agent_events（created_at 是 UTC TIMESTAMP）
    try:
        events = db.list_agent_events(uid, agent=agent_id, limit=1)
        if events:
            created = events[0].get("created_at")
            if created:
                dt = _dt.datetime.fromisoformat(str(created))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_dt.timezone.utc)
                delta = _dt.datetime.now(_dt.timezone.utc) - dt
                if _dt.timedelta(0) <= delta <= _dt.timedelta(hours=1):
                    return True, None
    except Exception:
        pass
    return False, None


def _agent_entry(uid: str, roster: dict) -> dict:
    """One agent's team-page entry. Lane module missing (not landed yet) is
    normal — that agent reports idle with events from db.agent_events.
    Never raises: the endpoint must never 500 because a lane module is missing."""
    agent_id = roster["id"]
    entry = {"id": agent_id, "name": roster["name"], "role_zh": roster["role_zh"],
             "role_en": roster["role_en"], "status": "idle", "current_task": None,
             "today_stats": [], "last_activity": None, "recent_events": []}
    mod = None
    try:
        import importlib as _il
        mod = _il.import_module(f"agents.{agent_id}")
    except Exception:
        mod = None
    status_fn = getattr(mod, "status", None) if mod is not None else None
    if callable(status_fn):
        try:
            st = status_fn(uid) or {}
            if st.get("status") in ("active", "idle"):
                entry["status"] = st["status"]
            if isinstance(st.get("current_task"), str):
                entry["current_task"] = st["current_task"] or None
            if isinstance(st.get("today_stats"), list):
                entry["today_stats"] = st["today_stats"]
            if isinstance(st.get("last_activity"), str):
                entry["last_activity"] = st["last_activity"] or None
        except Exception:
            pass
    events_fn = getattr(mod, "recent_events", None) if mod is not None else None
    if callable(events_fn):
        try:
            evs = events_fn(uid, 5) or []
            entry["recent_events"] = [
                {"event": e.get("event", ""), "summary": e.get("summary", ""),
                 "created_at": e.get("created_at"), "ref_id": e.get("ref_id")}
                for e in evs if isinstance(e, dict)]
        except Exception:
            entry["recent_events"] = []
    if not entry["recent_events"]:
        entry["recent_events"] = _agent_recent_events(uid, agent_id, 5)
    if entry["last_activity"] is None and entry["recent_events"]:
        entry["last_activity"] = entry["recent_events"][0].get("created_at")
    # Lane 模块还没落地（或没有 status）：只按真实证据判断 active。
    if not callable(status_fn):
        active, task = _agent_active_evidence(uid, agent_id)
        entry["status"] = "active" if active else "idle"
        entry["current_task"] = task
    return entry


@app.get("/api/v1/agents")
def list_agents(user: dict = Depends(get_current_user)):
    """团队状态：5 位 agent 的状态 / 今日统计 / 最近事件（团队页用）。
    Lane 模块未落地时该 agent 显示 idle，不 500。"""
    uid = user["app_user_id"]
    return {"agents": [_agent_entry(uid, a) for a in _AGENT_ROSTER]}


# ---------- 账号体系：注册 / 登录 / 会话 / API Token ----------
class AuthIn(BaseModel):
    username: str
    password: str
    invite_code: str = ""


class TokenIn(BaseModel):
    name: str = ""


class ClaimIn(BaseModel):
    old_user_id: str


@app.post("/api/v1/auth/register")
def auth_register(body: AuthIn, request: Request):
    _rate_limit(_client_ip(request))
    try:
        u = db.create_auth_user(body.username, body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if body.invite_code:
        db.redeem_invite(body.invite_code, u["app_user_id"])
    # 新用户默认开启定时投递：每天 10 点，10 份。用户之后可改，不用 upfront 决策。
    try:
        db.update_settings(u["app_user_id"], schedule_enabled=1,
                           schedule_time="10:00", daily_count=10)
    except Exception:
        pass
    token = db.create_session(u["id"])
    resp = Response('{"ok":true}', media_type="application/json")
    _set_session_cookie(resp, token)
    return resp


@app.get("/api/v1/invites")
def list_my_invites(user: dict = Depends(get_current_user)):
    return {"invites": db.list_invites(user["app_user_id"])}


class InviteIn(BaseModel):
    note: str = ""


@app.post("/api/v1/invites")
def create_my_invite(body: InviteIn, user: dict = Depends(get_current_user)):
    inv = db.create_invite(user["app_user_id"], body.note)
    return {**inv, "link": f"https://applai-backend.fly.dev/?invite={inv['code']}"}


@app.post("/api/v1/profile/suggest-job")
def suggest_job_profile(user: dict = Depends(get_current_user)):
    """根据人物简历画像，AI 生成目标岗位画像初稿。"""
    return jennifer.suggest_job_profile(user["app_user_id"])


def admin_chat_debug(body: dict):
    """一次性：完整复现 chat pipeline（含 JD/action/llm）。用完即删。"""
    import traceback
    import llm as llm_mod
    msg = body.get("message", "你给我投什么方向简历")
    target = body.get("uid", "")
    conn = db.get_db()
    users = [dict(r)["app_user_id"] for r in
             conn.execute("SELECT app_user_id FROM auth_users").fetchall()]
    conn.close()
    if target:
        users = [u for u in users if u.startswith(target)]
    out = {}
    for uid in users:
        steps = {}
        try:
            steps["s1_profile"] = "ok"
            profile = db.get_profile(uid) or {}
            ctx = []
            ctx.append("【用户画像】" + llm_mod._profile_text(profile)[:800])
            if profile.get("summary"):
                ctx.append("【简历摘要】" + str(profile["summary"])[:600])
            steps["s2_stats"] = "ok"
            stats = db.stats(uid)
            ctx.append("【统计】" + str(stats))
            steps["s3_apps"] = "ok"
            apps = db.list_applications(uid, 30)
            lines = []
            for a in apps:
                lines.append(f"- {a.get('title','?')} @ {a.get('company','?')}：{a.get('status','?')}（{a.get('detail','')[:60]}）")
            ctx.append("【最近申请】\n" + ("\n".join(lines) if lines else "暂无"))
            steps["s4_queue"] = "ok"
            try:
                queued = _queue_jobs(uid, 15)
                ql = [f"- {j.get('title','?')} @ {j.get('company','?')}：{j.get('score',0)}分（{j.get('location','')}）" for j in queued]
                ctx.append("【待投递队列】\n" + ("\n".join(ql) if ql else "队列为空"))
            except Exception:
                queued = []
                ctx.append("【待投递队列】暂无")
            steps["s5_jd"] = "ok"
            jd_parts, used = [], sum(len(c) for c in ctx)
            manual = [a for a in apps if a.get("status") == "needs_manual"][:5]
            queued_ids = {j["id"] for j in queued[:5]}
            want = {a.get("job_id") for a in manual} | queued_ids
            for jid in want:
                job = db.get_job(jid)
                if not job or used >= 12000:
                    continue
                desc = (db.job_description(job) or "")[:1500]
                if desc:
                    jd_parts.append(f"【JD】{job.get('title')} @ {job.get('company')}：\n{desc}")
                    used += len(jd_parts[-1])
            if jd_parts:
                ctx.append("\n\n".join(jd_parts))
            steps["s6_action"] = "ok"
            act = llm_mod.llm_detect_action(msg)
            steps["action"] = str(act)[:150]
            if act and act.get("action") not in (None, "none"):
                action_result = _run_chat_action(uid, act, queued)
                if action_result:
                    ctx.append("【刚才执行的操作】\n" + action_result)
            steps["s7_llm"] = "ok"
            reply = llm_mod.llm_chat("你是求职助手。\n\n实时数据：\n" + "\n".join(ctx)[:12000],
                                     [{"role": "user", "content": msg}])
            steps["reply"] = f"ok len={len(reply or '')}"
        except Exception:
            steps["FAILED"] = traceback.format_exc()[-2000:]
        out[uid[:12]] = steps
    return out


def admin_chat_debug(body: dict):
    """一次性：对所有用户跑 chat 上下文组装，找 500。用完即删。"""
    import traceback
    import llm as llm_mod
    conn = db.get_db()
    users = [dict(r)["app_user_id"] for r in
             conn.execute("SELECT app_user_id FROM auth_users").fetchall()]
    conn.close()
    out = {}
    for uid in users:
        steps = {}
        try:
            profile = db.get_profile(uid) or {}
            ctx = []
            ctx.append("【用户画像】" + llm_mod._profile_text(profile)[:800])
            if profile.get("summary"):
                ctx.append("【简历摘要】" + str(profile["summary"])[:600])
            stats = db.stats(uid)
            ctx.append("【统计】" + str(stats))
            apps = db.list_applications(uid, 30)
            lines = [f"- {a.get('title','?')} @ {a.get('company','?')}" for a in apps]
            ctx.append("【最近申请】\n" + ("\n".join(lines) if lines else "暂无"))
            queued = _queue_jobs(uid, 15)
            out[uid[:8]] = {"ctx_len": sum(len(c) for c in ctx), "status": "ok"}
        except Exception:
            out[uid[:8]] = {"FAILED": traceback.format_exc()[-1500:]}
    return out


def admin_chat_debug(body: dict):
    """一次性：复现 Victoria 账号的 chat 500。用完即删。"""
    import traceback
    msg = body.get("message", "你给我投什么方向简历")
    username = body.get("username", "")
    conn = db.get_db()
    r = conn.execute("SELECT app_user_id FROM auth_users WHERE username=?", (username,)).fetchone()
    conn.close()
    uid = dict(r)["app_user_id"] if r else ""
    steps = {}
    try:
        steps["profile"] = "ok"
        profile = db.get_profile(uid) or {}
        steps["stats"] = "ok"
        stats = db.stats(uid)
        steps["apps"] = "ok"
        apps = db.list_applications(uid, 30)
        steps["queue"] = "ok"
        queued = _queue_jobs(uid, 15)
        steps["action_detect"] = "ok"
        import llm as llm_mod
        act = llm_mod.llm_detect_action(msg)
        steps["action"] = str(act)[:200]
        steps["chat"] = "ok"
        reply = llm_mod.llm_chat("test", [{"role": "user", "content": msg}])
        steps["reply_len"] = len(reply or "")
    except Exception as e:
        steps["FAILED_AT"] = traceback.format_exc()[-2000:]
    return steps


@app.get("/api/v1/gmail/status")
def gmail_status(user: dict = Depends(get_current_user)):
    return gloria.gmail_status(user["app_user_id"])


@app.get("/api/v1/gmail/auth-url")
def gmail_auth_url(user: dict = Depends(get_current_user)):
    return gloria.gmail_auth_url(user["app_user_id"])


@app.get("/api/v1/gmail/callback")
def gmail_callback(code: str = "", state: str = "", error: str = ""):
    return gloria.gmail_callback(code, state, error)


@app.post("/api/v1/gmail/disconnect")
def gmail_disconnect(user: dict = Depends(get_current_user)):
    return gloria.gmail_disconnect(user["app_user_id"])


@app.post("/api/v1/admin/email-sync")
def admin_email_sync(request: Request):
    """每 4 小时跑一次的邮件同步（cron）。用 CRON_TOKEN 保护。"""
    return gloria.admin_email_sync(request.headers.get("x-cron-token", ""))


@app.get("/api/v1/reports/digest")
def report_digest(period: str = "weekly", user: dict = Depends(get_current_user)):
    """投递复盘数据。period=daily|weekly。"""
    return gloria.report_digest(user["app_user_id"], period)


@app.post("/api/v1/reports/advice")
def report_advice(user: dict = Depends(get_current_user)):
    """AI 复盘建议。"""
    return gloria.report_advice(user["app_user_id"])


@app.post("/api/v1/resume/tailor")
def tailor_resume(body: dict, user: dict = Depends(get_current_user)):
    """按岗位改写简历。body: {job_id, profile_id?}。返回版本信息。"""
    return caroline.tailor_resume(user["app_user_id"], body)


@app.get("/api/v1/resume/for-job")
def resume_for_job(job_id: int, user: dict = Depends(get_current_user)):
    """投递时取该用的简历：画像开了改写 → 返回改写版 PDF；否则返回母版。
返回 {pdf_hash, filename, version_id(0=母版), tailored: bool}。
插件再用 pdf_hash 下载。"""
    return caroline.resume_for_job(user["app_user_id"], job_id)


@app.get("/api/v1/resume/versions")
def list_resume_versions(profile_id: int = 0, user: dict = Depends(get_current_user)):
    """列出某画像的简历版本。"""
    return caroline.list_resume_versions(user["app_user_id"], profile_id)


@app.get("/api/v1/email-events")
def email_events(user: dict = Depends(get_current_user)):
    return gloria.email_events(user["app_user_id"])


@app.get("/api/v1/account/export")
def account_export(user: dict = Depends(get_current_user)):
    """导出本账号全部数据（JSON 下载）。"""
    return db.export_user_data(user["app_user_id"])


@app.delete("/api/v1/account")
def account_delete(user: dict = Depends(get_current_user)):
    """删除本账号及全部数据，不可恢复。"""
    stats = db.delete_user_data(user["app_user_id"])
    resp = Response('{"ok":true}', media_type="application/json")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.post("/api/v1/auth/login")
def auth_login(body: AuthIn, request: Request):
    _rate_limit(_client_ip(request))
    u = db.verify_login(body.username, body.password)
    if not u:
        raise HTTPException(401, "用户名或密码错误")
    token = db.create_session(u["id"])
    resp = Response('{"ok":true}', media_type="application/json")
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/v1/auth/logout")
def auth_logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.delete_session(token)
    resp = Response('{"ok":true}', media_type="application/json")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/v1/auth/me")
def auth_me(user: dict = Depends(get_current_user)):
    return {"username": user["username"], "app_user_id": user["app_user_id"]}


@app.post("/api/v1/auth/tokens")
def create_token(body: TokenIn, user: dict = Depends(get_current_user)):
    tid, raw = db.create_api_token(user["id"], body.name)
    return {"id": tid, "token": raw,
            "hint": "token 只显示一次，请复制保存到插件设置中"}


@app.get("/api/v1/auth/tokens")
def list_tokens(user: dict = Depends(get_current_user)):
    return {"tokens": db.list_api_tokens(user["id"])}


@app.delete("/api/v1/auth/tokens/{tid}")
def revoke_token(tid: int, user: dict = Depends(get_current_user)):
    if not db.revoke_api_token(user["id"], tid):
        raise HTTPException(404, "token not found")
    return {"ok": True}


@app.post("/api/v1/resume/parse")
async def resume_parse(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    """上传简历 PDF → 提取文本 → LLM 结构化抽取 → 返回字段供前端预填（用户确认后保存）。"""
    return await jennifer.resume_parse(user["app_user_id"], file)


# ---------- 聊天助手 ----------


@app.get("/api/v1/chat/conversations")
def chat_list_conversations(user: dict = Depends(get_current_user)):
    return gloria.chat_list_conversations(user["app_user_id"])


@app.post("/api/v1/chat/conversations")
def chat_create_conversation(user: dict = Depends(get_current_user)):
    return gloria.chat_create_conversation(user["app_user_id"])


@app.get("/api/v1/chat/conversations/{cid}/messages")
def chat_get_messages(cid: int, user: dict = Depends(get_current_user)):
    return gloria.chat_get_messages(user["app_user_id"], cid)


@app.delete("/api/v1/chat/conversations/{cid}")
def chat_delete_conversation(cid: int, user: dict = Depends(get_current_user)):
    return gloria.chat_delete_conversation(user["app_user_id"], cid)


@app.post("/api/v1/chat")
def chat(body: gloria.ChatIn, user: dict = Depends(get_current_user)):
    return gloria.chat(user["app_user_id"], body)


# ---------- Web Push（手机浏览器通知，替代 ntfy App） ----------
class PushSubIn(BaseModel):
    endpoint: str
    p256dh: str
    auth: str


@app.get("/api/v1/push/vapid-public-key")
def vapid_public_key():
    pub = os.environ.get("APPLAI_VAPID_PUBLIC_KEY", "").strip()
    if not pub:
        raise HTTPException(503, "push not configured")
    return {"publicKey": pub}


@app.post("/api/v1/push/subscribe")
def push_subscribe(body: PushSubIn, user: dict = Depends(get_current_user)):
    from urllib.parse import urlparse as _up
    _host = (_up(body.endpoint).hostname or "").lower()
    _push_ok = _host in ("fcm.googleapis.com", "web.push.apple.com",
                         "updates.push.services.mozilla.com") \
        or _host.endswith(".notify.windows.com") or _host.endswith(".push.apple.com")
    if not body.endpoint.startswith("https://") or not _push_ok:
        raise HTTPException(400, "endpoint 无效")
    db.add_push_subscription(user["app_user_id"], body.endpoint, body.p256dh, body.auth)
    return {"ok": True}


@app.post("/api/v1/push/unsubscribe")
def push_unsubscribe(body: dict, user: dict = Depends(get_current_user)):
    db.remove_push_subscription(user["app_user_id"], (body or {}).get("endpoint", ""))
    return {"ok": True}


@app.post("/api/v1/auth/claim")
def claim_old(user: dict = Depends(get_current_user), body: ClaimIn = None):
    """把插件时代旧 user_id 下的数据迁移到当前账号。"""
    try:
        return db.claim_old_data(user["app_user_id"], (body.old_user_id if body else "").strip())
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------- 网页 Dashboard（会话登录保护） ----------
def _dashboard_user(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    return db.get_session_user(token) if token else None


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page():
    html = Path(__file__).parent / "login.html"
    return HTMLResponse(html.read_text(encoding="utf-8"))


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard_page(request: Request):
    if not _dashboard_user(request):
        return RedirectResponse("/login")
    html = Path(__file__).parent / "dashboard.html"
    return HTMLResponse(html.read_text(encoding="utf-8"))


@app.get("/dashboard/sw.js", include_in_schema=False)
def dashboard_sw():
    js = Path(__file__).parent / "dashboard-sw.js"
    return Response(js.read_text(encoding="utf-8"), media_type="application/javascript")


@app.get("/dashboard/manifest.json", include_in_schema=False)
def dashboard_manifest():
    return {
        "name": "Applai Dashboard",
        "short_name": "Applai",
        "start_url": "/dashboard",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#111827",
        "icons": [
            {"src": "/dashboard/icon.svg", "sizes": "any", "type": "image/svg+xml"}
        ],
    }


@app.get("/dashboard/icon.svg", include_in_schema=False)
def dashboard_icon():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="14" fill="#111827"/>'
        '<circle cx="32" cy="32" r="14" fill="none" stroke="#fff" stroke-width="5"/>'
        '<circle cx="32" cy="32" r="4" fill="#fff"/></svg>'
    )
    return Response(svg, media_type="image/svg+xml")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/dashboard")
