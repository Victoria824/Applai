"""app.py — Applai 后端：画像 / 职位 / 匹配队列 / 投递记录

运行：cd backend && pip install -r requirements.txt && uvicorn app:app --port 8000
"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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
    return {"ok": True, "version": "0.2.0"}


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
    # 单条手动添加也打分
    profile = db.get_profile(body.user_id)
    full = db.get_job(job["id"])
    s, reasons = matcher.score_job(profile, full)
    db.update_job_score(job["id"], s, reasons)
    return {"ok": True, **job, "score": s, "score_reasons": reasons}


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
                             p["location"], source=p["source"], raw=p["raw"])
            if job["is_new"]:
                total_new += 1
                s, reasons = matcher.score_job(profile, db.get_job(job["id"]))
                db.update_job_score(job["id"], s, reasons)
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
