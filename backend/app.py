"""app.py — Applai 后端：画像 / 职位 / 匹配队列 / 投递记录 / 网页 Dashboard

运行：cd backend && pip install -r requirements.txt && uvicorn app:app --port 8000
"""
import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

import db
import ingest
import matcher

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
def put_profile(body: ProfileIn):
    db.upsert_profile(body.user_id, body.profile)
    return {"ok": True}


@app.get("/api/v1/profile")
def get_profile(user_id: str):
    return {"user_id": user_id, "profile": db.get_profile(user_id)}


@app.post("/api/v1/jobs")
def add_job(body: JobIn):
    src = ingest.parse_source(body.url)
    job = db.add_job(body.user_id, body.url, body.title, body.company,
                     body.location, source=(f"{src['type']}:{src['key']}" if src else "manual"))
    profile = db.get_profile(body.user_id)
    s, reasons, via = score_and_store(body.user_id, job["id"], profile)
    return {"ok": True, **job, "score": s, "score_reasons": reasons, "score_via": via}


@app.post("/api/v1/jobs/discover")
def discover(body: DiscoverIn):
    """从 ATS 公开 API 批量抓取职位，去重入库并打分。"""
    profile = db.get_profile(body.user_id)
    if not profile:
        raise HTTPException(400, "profile not found, sync profile first")
    total_new, total_seen = 0, 0
    for src in body.sources:
        try:
            postings = ingest.fetch_source(src, body.limit)
        except Exception as e:
            continue
        for p in postings:
            total_seen += 1
            job = db.add_job(body.user_id, p["url"], p["title"], p["company"],
                             p["location"], source=p["source"], raw=p["raw"],
                             description=p.get("description", ""))
            if job["is_new"]:
                total_new += 1
                score_and_store(body.user_id, job["id"], profile)
    return {"ok": True, "seen": total_seen, "new": total_new}


@app.get("/api/v1/queue")
def get_queue(user_id: str, top_n: int = 10):
    """今日投递队列：画像匹配 TopN，排除已处理过的职位。"""
    profile = db.get_profile(user_id)
    if not profile:
        raise HTTPException(400, "profile not found, sync profile first")
    jobs = db.list_jobs(user_id, limit=500)
    done = db.applied_job_ids(user_id)
    fresh = [j for j in jobs if j["id"] not in done]
    queue = matcher.build_queue(profile, fresh, top_n=top_n)
    # 标记为 queued
    for j in queue:
        db.record_application(user_id, j["id"], "queued", f"score {j['score']}")
    return {"user_id": user_id, "count": len(queue), "jobs": queue}


@app.post("/api/v1/applications")
def post_application(body: ApplicationIn):
    if body.status not in ("queued", "filled", "submitted", "needs_manual", "failed", "skipped"):
        raise HTTPException(400, "invalid status")
    db.record_application(body.user_id, body.job_id, body.status, body.detail)
    return {"ok": True}


@app.get("/api/v1/applications")
def get_applications(user_id: str, limit: int = 200):
    return {"user_id": user_id, "applications": db.list_applications(user_id, limit)}


@app.get("/api/v1/stats")
def get_stats(user_id: str):
    return {"user_id": user_id, "stats": db.stats(user_id)}


# ---------- 网页 Dashboard（密码保护） ----------
# 密码通过环境变量 APPLAI_DASHBOARD_PASSWORD 设置（Fly 上用 secret）。
# 未设置时 /dashboard 返回 503，避免无密码裸奔。

_basic = HTTPBasic(auto_error=False)


def dashboard_auth(creds: HTTPBasicCredentials = Depends(_basic)):
    pwd = os.environ.get("APPLAI_DASHBOARD_PASSWORD", "")
    if not pwd:
        raise HTTPException(503, "dashboard not configured: set APPLAI_DASHBOARD_PASSWORD")
    if not creds or not secrets.compare_digest(creds.password or "", pwd):
        raise HTTPException(
            401, "login required",
            headers={"WWW-Authenticate": 'Basic realm="Applai Dashboard"'},
        )
    return True


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard_page(_ok: bool = Depends(dashboard_auth)):
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


@app.get("/api/v1/users")
def list_users(_ok: bool = Depends(dashboard_auth)):
    """Dashboard 用户列表（带画像标签，方便选择）。"""
    return {"users": db.list_users()}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/dashboard")
