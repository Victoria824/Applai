"""app.py — Applai 后端：画像 / 职位 / 匹配队列 / 投递记录 / 网页 Dashboard

运行：cd backend && pip install -r requirements.txt && uvicorn app:app --port 8000
"""
import os
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


@app.post("/api/v1/jobs/discover/auto")
def discover_auto(user: dict = Depends(get_current_user)):
    """用已保存的职位来源自动抓取（供每天定时任务调用）。"""
    uid = user["app_user_id"]
    sources = [{"type": s["type"], "key": s["key"]} for s in db.list_sources(uid)]
    if not sources:
        return {"ok": True, "seen": 0, "new": 0, "note": "no sources configured"}
    return {"ok": True, **_run_discover(uid, sources)}


@app.post("/api/v1/jobs/discover")
def discover(body: DiscoverIn, user: dict = Depends(get_current_user)):
    """从 ATS 公开 API 批量抓取职位，去重入库并打分。"""
    uid = user["app_user_id"]
    return {"ok": True, **_run_discover(uid, body.sources, body.limit)}


@app.get("/api/v1/queue")
def get_queue(user: dict = Depends(get_current_user), top_n: int = 10):
    """今日投递队列：画像匹配 TopN，排除已处理过的职位。"""
    uid = user["app_user_id"]
    profile = db.get_profile(uid)
    if not profile:
        raise HTTPException(400, "profile not found, sync profile first")
    jobs = db.list_jobs(uid, limit=500)
    done = db.applied_job_ids(uid)
    fresh = [j for j in jobs if j["id"] not in done]
    queue = matcher.build_queue(profile, fresh, top_n=top_n)
    # 标记为 queued
    for j in queue:
        db.record_application(uid, j["id"], "queued", f"score {j['score']}")
    return {"user_id": uid, "count": len(queue), "jobs": queue}


@app.post("/api/v1/applications")
def post_application(body: ApplicationIn, user: dict = Depends(get_current_user)):
    if body.status not in ("queued", "filled", "submitted", "needs_manual", "failed", "skipped"):
        raise HTTPException(400, "invalid status")
    db.record_application(user["app_user_id"], body.job_id, body.status, body.detail)
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
