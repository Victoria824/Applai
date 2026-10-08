"""app.py — Applai 后端：画像 / 职位 / 匹配队列 / 投递记录 / 网页 Dashboard

运行：cd backend && pip install -r requirements.txt && uvicorn app:app --port 8000
"""
import os
import re
import time
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

import db
import ingest
import matcher

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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


class ProfileIn(BaseModel):
    user_id: str
    profile: dict


class JobIn(BaseModel):
    user_id: str
    url: str
    title: str = ""
    company: str = ""
    location: str = ""


class DiscoverIn(BaseModel):
    user_id: str
    sources: list = Field(default_factory=list)  # [{type:'greenhouse'|'lever', key:'board-or-site'}]
    limit: int = 100


class ApplicationIn(BaseModel):
    user_id: str
    job_id: int
    status: str  # queued|filled|submitted|needs_manual|failed|skipped
    detail: str = ""


@app.get("/health")
def health():
    import llm as llm_mod
    return {"ok": True, "version": "0.2.0", "llm": llm_mod.is_configured()}


def score_and_store(user_id: str, job_id: int, profile: dict):
    """规则预筛 + LLM 精排，结果写回 DB。"""
    job = db.get_job(job_id)
    rs, rreasons = matcher.score_job(profile, job)
    fs, freasons, via = matcher.hybrid_score(
        profile, {**job, "description": db.job_description(job)}, rs, rreasons)
    db.update_job_score(job_id, fs, freasons, via)
    return fs, freasons, via


@app.put("/api/v1/profile")
def put_profile(body: ProfileIn, user: dict = Depends(get_current_user)):
    db.upsert_profile(user["app_user_id"], body.profile)
    return {"ok": True}


@app.get("/api/v1/profile")
def get_profile(user: dict = Depends(get_current_user)):
    uid = user["app_user_id"]
    return {"user_id": uid, "profile": db.get_profile(uid)}


@app.post("/api/v1/jobs")
def add_job(body: JobIn, user: dict = Depends(get_current_user)):
    uid = user["app_user_id"]
    src = ingest.parse_source(body.url)
    job = db.add_job(uid, body.url, body.title, body.company,
                     body.location, source=(f"{src['type']}:{src['key']}" if src else "manual"))
    profile = db.get_profile(uid)
    s, reasons, via = score_and_store(uid, job["id"], profile)
    return {"ok": True, **job, "score": s, "score_reasons": reasons, "score_via": via}


class SourceIn(BaseModel):
    type: str  # greenhouse | lever
    key: str   # board token / 公司名


@app.get("/api/v1/sources")
def get_sources(user: dict = Depends(get_current_user)):
    return {"sources": db.list_sources(user["app_user_id"])}


@app.post("/api/v1/sources")
def add_source(body: SourceIn, user: dict = Depends(get_current_user)):
    try:
        return db.add_source(user["app_user_id"], body.type, body.key)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/v1/sources/{sid}")
def delete_source(sid: int, user: dict = Depends(get_current_user)):
    if not db.remove_source(user["app_user_id"], sid):
        raise HTTPException(404, "source not found")
    return {"ok": True}


def _keywords_for(uid: str, profile: dict) -> tuple[list, str]:
    """获取搜索关键词：缓存有效直接用，否则 LLM 生成，失败用画像目标职位兜底。"""
    st = db.get_settings(uid)
    kws, note, updated = st.get("job_keywords") or [], st.get("keywords_note") or "", st.get("keywords_updated") or 0
    if kws and time.time() - updated < 7 * 86400:
        return kws, note
    import llm as llm_mod
    gen = llm_mod.llm_job_keywords(profile)
    if gen and gen.get("keywords"):
        db.update_settings(uid, job_keywords=gen["keywords"],
                           keywords_note=gen.get("note", ""), keywords_updated=time.time())
        return gen["keywords"], gen.get("note", "")
    fallback = [t for t in (profile.get("targetTitles") or []) if t]
    return fallback, ""


def _run_discover(uid: str, sources: list, limit: int = 100) -> dict:
    """从来源抓取职位入库并打分，返回 {seen, new}。"""
    profile = db.get_profile(uid)
    if not profile:
        raise HTTPException(400, "profile not found, sync profile first")
    total_new, total_seen = 0, 0
    for src in sources:
        try:
            postings = ingest.fetch_source(src, limit)
        except Exception:
            continue
        for p in postings:
            total_seen += 1
            job = db.add_job(uid, p["url"], p["title"], p["company"],
                             p["location"], source=p["source"], raw=p["raw"],
                             description=p.get("description", ""))
            if job["is_new"]:
                total_new += 1
                score_and_store(uid, job["id"], profile)
    return {"seen": total_seen, "new": total_new}


def _run_smart_discover(uid: str, limit: int = 60, progress=None) -> dict:
    """智能模式：LLM 按画像生成关键词 → 免费聚合源全网搜索 → 入库打分。手动来源照常抓取。"""
    def pg(stage, done, total=100):
        if progress:
            try:
                progress(stage, done, total)
            except Exception:
                pass
    profile = db.get_profile(uid)
    if not profile:
        raise HTTPException(400, "profile not found, sync profile first")
    manual = [{"type": s["type"], "key": s["key"]} for s in db.list_sources(uid)]
    seen_keys = {(s["type"], s["key"]) for s in manual}
    seeds = [{"type": t, "key": k} for t, k in ingest.CA_SEED_BOARDS
             if (t, k) not in seen_keys]
    pg("公司官网 / Company boards", 5)
    out = _run_discover(uid, manual + seeds, limit) if (manual or seeds) else {"seen": 0, "new": 0}
    kws, note = _keywords_for(uid, profile)
    if not kws:
        return {**out, "keywords": [], "mode": "seeds_only"}
    agg_new, agg_seen = 0, 0
    stages = [("jobbank", "加拿大 Job Bank / Job Bank", lambda: ingest.fetch_jobbank(kws, 40)),
              ("arbeitnow", "聚合源 Arbeitnow", lambda: ingest.fetch_arbeitnow(kws, limit)),
              ("remotive", "聚合源 Remotive", lambda: ingest.fetch_remotive(kws, limit))]
    for i, (_name, label, fetcher) in enumerate(stages):
        pg(f"{label}（{i+1}/3）", 10 + i * 15)
        try:
            postings = fetcher()
        except Exception:
            continue
        new_jobs = []
        for p in postings:
            agg_seen += 1
            job = db.add_job(uid, p["url"], p["title"], p["company"],
                             p["location"], source=p["source"], raw=p["raw"],
                             description=p.get("description", ""))
            if job["is_new"]:
                agg_new += 1
                new_jobs.append(job["id"])
        # AI 打分（只对新职位）
        for j, jid in enumerate(new_jobs):
            pg(f"AI 打分 / Scoring（{label} {j+1}/{len(new_jobs)}）", 10 + i * 15 + int(15 * j / max(1, len(new_jobs))))
            try:
                score_and_store(uid, jid, profile)
            except Exception:
                continue
    pg("完成 / Done", 100)
    return {"seen": out["seen"] + agg_seen, "new": out["new"] + agg_new,
            "keywords": kws, "note": note, "mode": "smart"}


import uuid as _uuid
import threading as _threading
DISCOVER_TASKS: dict = {}  # task_id -> {status, stage, done, total, new, seen, error}


def _discover_task_set(task_id: str, **kw):
    t = DISCOVER_TASKS.get(task_id)
    if t:
        t.update(kw)


def _run_smart_discover_bg(uid: str, task_id: str):
    try:
        _discover_task_set(task_id, status="running", stage="公司官网 / Company boards", done=0, total=100)
        res = _run_smart_discover(uid, progress=lambda s, d, t: _discover_task_set(task_id, stage=s, done=d, total=t))
        _discover_task_set(task_id, status="done", stage="完成 / Done",
                           done=100, total=100, new=res.get("new", 0), seen=res.get("seen", 0))
    except Exception as e:
        _discover_task_set(task_id, status="error", error=str(e)[:200])


@app.post("/api/v1/jobs/discover/auto")
def discover_auto(user: dict = Depends(get_current_user)):
    """智能抓取：立即返回任务 ID，后台执行，前端轮询进度。"""
    uid = user["app_user_id"]
    task_id = _uuid.uuid4().hex[:12]
    DISCOVER_TASKS[task_id] = {"status": "running", "stage": "启动中 / Starting…",
                               "done": 0, "total": 100, "new": 0, "seen": 0, "error": ""}
    _threading.Thread(target=_run_smart_discover_bg, args=(uid, task_id), daemon=True).start()
    return {"ok": True, "task_id": task_id, "status": "running"}


@app.get("/api/v1/jobs/discover/status/{task_id}")
def discover_status(task_id: str, user: dict = Depends(get_current_user)):
    t = DISCOVER_TASKS.get(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    return {"ok": True, "task_id": task_id, **t}


@app.get("/api/v1/settings")
def get_user_settings(user: dict = Depends(get_current_user)):
    st = db.get_settings(user["app_user_id"])
    st.pop("job_keywords", None)  # 关键词走专属接口
    return st


class SettingsIn(BaseModel):
    schedule_enabled: bool | None = None
    schedule_time: str | None = None
    daily_count: int | None = None


@app.put("/api/v1/settings")
def put_user_settings(body: SettingsIn, user: dict = Depends(get_current_user)):
    kw = {}
    if body.schedule_enabled is not None:
        kw["schedule_enabled"] = body.schedule_enabled
    if body.schedule_time is not None:
        if not re.match(r"^\d{2}:\d{2}$", body.schedule_time):
            raise HTTPException(400, "schedule_time 格式应为 HH:MM")
        kw["schedule_time"] = body.schedule_time
    if body.daily_count is not None:
        kw["daily_count"] = max(1, min(30, int(body.daily_count)))
    st = db.update_settings(user["app_user_id"], **kw)
    st.pop("job_keywords", None)
    return st


@app.get("/api/v1/settings/keywords")
def get_keywords(user: dict = Depends(get_current_user)):
    uid = user["app_user_id"]
    kws, note = _keywords_for(uid, db.get_profile(uid) or {})
    return {"keywords": kws, "note": note}


@app.post("/api/v1/settings/keywords/refresh")
def refresh_keywords(user: dict = Depends(get_current_user)):
    """强制重新生成关键词（画像大改后调用）。"""
    uid = user["app_user_id"]
    import llm as llm_mod
    profile = db.get_profile(uid) or {}
    gen = llm_mod.llm_job_keywords(profile)
    if gen and gen.get("keywords"):
        db.update_settings(uid, job_keywords=gen["keywords"],
                           keywords_note=gen.get("note", ""), keywords_updated=time.time())
        return {"keywords": gen["keywords"], "note": gen.get("note", "")}
    raise HTTPException(502, "关键词生成失败，请稍后重试")


@app.post("/api/v1/jobs/discover")
def discover(body: DiscoverIn, user: dict = Depends(get_current_user)):
    """从 ATS 公开 API 批量抓取职位，去重入库并打分。"""
    uid = user["app_user_id"]
    return {"ok": True, **_run_discover(uid, body.sources, body.limit)}


def _queue_jobs(uid: str, top_n: int = 10) -> list:
    """待投递队列：用入库时的混合分数排序，排除已处理过的职位。"""
    jobs = db.list_jobs(uid, limit=500)
    done = db.applied_job_ids(uid)
    fresh = [j for j in jobs
             if j["id"] not in done and (j.get("score") or 0) >= 40]
    fresh.sort(key=lambda j: (-j["score"], j.get("created_at", 0)))
    return fresh[:top_n]


@app.get("/api/v1/queue")
def get_queue(user: dict = Depends(get_current_user), top_n: int = 10):
    """今日投递队列：只读，无副作用。"""
    uid = user["app_user_id"]
    if not db.get_profile(uid):
        raise HTTPException(400, "profile not found, sync profile first")
    queue = _queue_jobs(uid, top_n)
    return {"user_id": uid, "count": len(queue), "jobs": queue}


def _rescore_pending(uid: str):
    """后台：画像变更后重打所有未处理职位。"""
    try:
        profile = db.get_profile(uid)
        if not profile:
            return
        jobs = db.list_jobs(uid, limit=500)
        done = db.applied_job_ids(uid)
        for j in jobs:
            if j["id"] in done or j.get("score") is not None:
                continue
            try:
                score_and_store(uid, j["id"], profile)
            except Exception:
                continue
    except Exception:
        pass


@app.put("/api/v1/profile")
def put_profile(body: ProfileIn, user: dict = Depends(get_current_user)):
    uid = user["app_user_id"]
    db.upsert_profile(uid, body.profile)
    # 画像变了：关键词缓存失效 + 未处理职位重打分（后台）
    db.update_settings(uid, keywords_updated=0)
    db.reset_scores(uid)
    import threading
    threading.Thread(target=_rescore_pending, args=(uid,), daemon=True).start()
    return {"ok": True}


@app.post("/api/v1/applications")
def post_application(body: ApplicationIn, user: dict = Depends(get_current_user)):
    if body.status not in ("queued", "filled", "submitted", "needs_manual", "failed", "skipped"):
        raise HTTPException(400, "invalid status")
    uid = user["app_user_id"]
    db.record_application(uid, body.job_id, body.status, body.detail)
    if body.status == "needs_manual":
        try:
            job = db.get_job(body.job_id) or {}
            send_web_push(uid, "Applai：有职位需要人工处理",
                          f"{job.get('title', '')} @ {job.get('company', '')} — {body.detail[:80]}")
        except Exception:
            pass
    return {"ok": True}


@app.get("/api/v1/applications")
def get_applications(user: dict = Depends(get_current_user), limit: int = 200):
    uid = user["app_user_id"]
    return {"user_id": uid, "applications": db.list_applications(uid, limit)}


@app.get("/api/v1/stats")
def get_stats(user: dict = Depends(get_current_user)):
    uid = user["app_user_id"]
    return {"user_id": uid, "stats": db.stats(uid)}


# ---------- 账号体系：注册 / 登录 / 会话 / API Token ----------
class AuthIn(BaseModel):
    username: str
    password: str


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
    token = db.create_session(u["id"])
    resp = Response('{"ok":true}', media_type="application/json")
    _set_session_cookie(resp, token)
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
    name = (file.filename or "").lower()
    if not name.endswith(".pdf"):
        raise HTTPException(400, "目前只支持 PDF 简历")
    data = await file.read()
    if len(data) > 4 * 1024 * 1024:
        raise HTTPException(400, "文件超过 4MB")
    if len(data) < 100:
        raise HTTPException(400, "文件内容为空")
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((p.extract_text() or "") for p in reader.pages[:5])[:8000]
    except Exception:
        raise HTTPException(400, "PDF 解析失败，请确认文件未损坏或加密")
    if len(text.strip()) < 50:
        raise HTTPException(400, "未能从 PDF 提取到文字（可能是扫描图片版），请手动填写")
    import llm as llm_mod
    fields = llm_mod.llm_parse_resume(text)
    if not fields:
        raise HTTPException(502, "简历识别服务暂不可用，请手动填写")
    # 只保留白名单字段
    allowed = ("firstName", "lastName", "email", "phone", "location", "linkedin",
               "github", "website", "targetTitles", "industries", "yearsExperience",
               "preferredLocations", "workAuth", "summary")
    out = {k: fields.get(k) for k in allowed if fields.get(k) not in (None, "", [])}
    return {"fields": out, "chars": len(text)}


# ---------- 聊天助手 ----------
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
5. 数据里没有的信息要承认不知道，不要 hallucinate。"""

MAX_CTX = 12000


@app.get("/api/v1/chat/conversations")
def chat_list_conversations(user: dict = Depends(get_current_user)):
    return {"conversations": db.list_conversations(user["app_user_id"])}


@app.post("/api/v1/chat/conversations")
def chat_create_conversation(user: dict = Depends(get_current_user)):
    return db.create_conversation(user["app_user_id"])


@app.get("/api/v1/chat/conversations/{cid}/messages")
def chat_get_messages(cid: int, user: dict = Depends(get_current_user)):
    uid = user["app_user_id"]
    if not db.get_conversation(uid, cid):
        raise HTTPException(404, "conversation not found")
    return {"messages": db.get_chat_messages(uid, cid)}


@app.delete("/api/v1/chat/conversations/{cid}")
def chat_delete_conversation(cid: int, user: dict = Depends(get_current_user)):
    db.delete_conversation(user["app_user_id"], cid)
    return {"ok": True}


@app.post("/api/v1/chat")
def chat(body: ChatIn, user: dict = Depends(get_current_user)):
    import llm as llm_mod
    uid = user["app_user_id"]
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
        db.add_chat_message(uid, cid, "user", msg)
        # 首条消息自动取标题
        if len(db.get_chat_messages(uid, cid, 2)) <= 1:
            db.rename_conversation(uid, cid, msg[:24])
    else:
        for h in (body.history or [])[-8:]:
            if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content"):
                history.append({"role": h["role"], "content": str(h["content"])[:1500]})
    history.append({"role": "user", "content": msg})

    reply = llm_mod.llm_chat(CHAT_SYSTEM + "\n\n实时数据：\n" + "\n".join(ctx)[:MAX_CTX], history)
    if not reply:
        raise HTTPException(502, "助手暂不可用，请稍后重试")
    if cid and db.get_conversation(uid, cid):
        db.add_chat_message(uid, cid, "assistant", reply)
    return {"reply": reply, "conversation_id": cid}


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
    if not body.endpoint.startswith("https://"):
        raise HTTPException(400, "endpoint 无效")
    db.add_push_subscription(user["app_user_id"], body.endpoint, body.p256dh, body.auth)
    return {"ok": True}


@app.post("/api/v1/push/unsubscribe")
def push_unsubscribe(body: dict, user: dict = Depends(get_current_user)):
    db.remove_push_subscription(user["app_user_id"], (body or {}).get("endpoint", ""))
    return {"ok": True}


def send_web_push(user_id: str, title: str, body: str):
    """验证码等事件发生时推送到用户手机浏览器。失败静默（best effort）。"""
    priv = os.environ.get("APPLAI_VAPID_PRIVATE_KEY", "").strip()
    if not priv:
        return
    try:
        from pywebpush import webpush, WebPushException
        import json as _json
        for sub in db.list_push_subscriptions(user_id):
            try:
                webpush(
                    subscription_info={"endpoint": sub["endpoint"],
                                       "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                    data=_json.dumps({"title": title, "body": body}),
                    vapid_private_key=priv,
                    vapid_claims={"sub": "mailto:applai@localhost"},
                )
            except Exception:
                continue
    except Exception:
        pass


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
