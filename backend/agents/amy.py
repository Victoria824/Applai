"""agents.amy — Amy · 职位搜罗 / Job Hunter.

Moved AS-IS from backend/app.py (branch multi-agent; app.py untouched).
This module owns: sources CRUD, single-job lookup/add, manual + smart
discover (fetch only — ingest as-is, db.add_job dedups by URL), the
background discover thread, and discover-task status.

Rules honoured here:
- No FastAPI decorators / Depends / Request. Route handlers are plain
  functions taking uid explicitly.
- No `import app` (circular). Shared services only: db, ingest, matcher.
- Fetch only ingests jobs as-is; scoring is NOT Amy's job — _run_discover
  and _run_smart_discover do not score (the background scorer does).
  Leaving the sources page never cancels a server-side fetch; a second
  discover click starts a parallel task (no dedup guard).
- Keywords: Jennifer owns generation. Amy's _keywords_for below delegates
  (lazy import) to Jennifer's full _keywords_for — cache read plus LLM
  regeneration when the cache is stale/empty — exactly as app.py did.
  The db settings job_keywords cache remains the handoff between them.
- Milestones are logged ONLY via db.log_agent_event (coarse only, never
  per-job / per-source rows): discover_started, discover_finished
  (with new/seen counts), discover_failed.

Agent interface (GET /api/v1/agents lazy-imports this module):
- status(uid) -> {"status", "current_task", "today_stats", "last_activity"}
- recent_events(uid, limit=5) -> [{"event", "summary", "created_at", "ref_id"}]
Neither ever raises.
"""

import time
import uuid as _uuid
import threading as _threading

from fastapi import HTTPException
from pydantic import BaseModel, Field

import db
import ingest
import matcher

# ---------------------------------------------------------------------------
# Pydantic models (moved as-is from app.py; field names kept identical,
# including the legacy user_id field which handlers ignore — uid comes
# from the authenticated user, exactly as in app.py).
# ---------------------------------------------------------------------------


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


class SourceIn(BaseModel):
    type: str  # greenhouse | lever
    key: str   # board token / 公司名


# ---------------------------------------------------------------------------
# Scoring helper — delegates to Jennifer's canonical score_and_store
# (agents.jennifer). app.py's add_job scores the single manually-added
# job synchronously; the lazy import below preserves that exactly with
# one implementation (no copy to drift). _run_discover /
# _run_smart_discover do NOT use it (fetch ingests only).
# ---------------------------------------------------------------------------


def _score_and_store(user_id: str, job_id: int, profile: dict):
    """规则预筛 + LLM 精排，结果写回 DB — canonical in agents.jennifer."""
    from agents.jennifer import score_and_store as _j_score_and_store
    return _j_score_and_store(user_id, job_id, profile)


# ---------------------------------------------------------------------------
# Job lookup / add (app.py:217 lookup_job, app.py:227 add_job)
# ---------------------------------------------------------------------------


def lookup_job(uid: str, url: str):
    """按 URL 查职位（插件 Job Bank 面板用）。"""
    job = db.get_job_by_url(uid, url)
    if not job:
        raise HTTPException(404, "job not found")
    return job


def add_job(uid: str, body: JobIn):
    src = ingest.parse_source(body.url)
    job = db.add_job(uid, body.url, body.title, body.company,
                     body.location, source=(f"{src['type']}:{src['key']}" if src else "manual"))
    if not job:
        raise HTTPException(409, "职位已存在或正在写入，请稍后重试")
    profile = db.get_profile(uid)
    s, reasons, via = _score_and_store(uid, job["id"], profile)
    return {"ok": True, **job, "score": s, "score_reasons": reasons, "score_via": via}


# ---------------------------------------------------------------------------
# Sources CRUD (app.py:245 get_sources, app.py:250 add_source,
# app.py:258 delete_source)
# ---------------------------------------------------------------------------


def get_sources(uid: str):
    return {"sources": db.list_sources(uid)}


def add_source(uid: str, body: SourceIn):
    try:
        return db.add_source(uid, body.type, body.key)
    except ValueError as e:
        raise HTTPException(400, str(e))


def delete_source(uid: str, sid: int):
    if not db.remove_source(uid, sid):
        raise HTTPException(404, "source not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Keywords — delegate to Jennifer (see module docstring). Her full
# _keywords_for reads the cache and regenerates via LLM when it is
# empty/stale, writing the result back — exactly as app.py did.
# ---------------------------------------------------------------------------


def _keywords_for(uid: str, profile: dict) -> tuple[list, str]:
    """Keywords are Jennifer's — delegate (lazy import) to her full
    _keywords_for: cache read plus LLM regeneration when the cache is
    stale/empty, exactly as app.py did. The cache remains the handoff."""
    from agents.jennifer import _keywords_for as _j_keywords_for
    return _j_keywords_for(uid, profile)


# ---------------------------------------------------------------------------
# Discover (app.py:280 _run_discover, app.py:313 _run_smart_discover)
# ---------------------------------------------------------------------------


def _run_discover(uid: str, sources: list, limit: int = 100) -> dict:
    """从来源抓取职位入库并打分，返回 {seen, new}。"""
    profile = db.get_profile(uid)
    if not profile:
        raise HTTPException(400, "profile not found, sync profile first")
    total_new, total_seen = 0, 0
    # 14 家公司官网并行抓取（4 线程），串行最慢 14×30s=7 分钟，并行 ~1 分钟
    from concurrent.futures import ThreadPoolExecutor as _TPE
    all_postings = []
    def _fetch_one(src):
        try:
            return ingest.fetch_source(src, limit) or []
        except Exception:
            return []
    if sources:
        with _TPE(max_workers=4) as _ex:
            for postings in _ex.map(_fetch_one, sources):
                all_postings.extend(postings or [])
    for p in all_postings:
        if not isinstance(p, dict) or not p.get("url"):
            continue
        total_seen += 1
        job = db.add_job(uid, p["url"], p["title"], p["company"],
                         p["location"], source=p["source"], raw=p["raw"],
                         description=p.get("description", ""))
        if not job:
            continue
        if job["is_new"]:
            total_new += 1
        # 不打分：打分由后台 scorer 统一处理，抓取只负责入库
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
        for p in (postings or []):
            if not isinstance(p, dict) or not p.get("url"):
                continue
            agg_seen += 1
            job = db.add_job(uid, p["url"], p["title"], p["company"],
                             p["location"], source=p["source"], raw=p["raw"],
                             description=p.get("description", ""))
            if not job:
                continue
            if job["is_new"]:
                agg_new += 1
                new_jobs.append(job["id"])
            # 老职位跳过打分：后台 scorer 统一处理
        # 不打分：新职位由后台 scorer 统一打分，抓取只负责入库
    pg("完成 / Done", 100)
    return {"seen": out["seen"] + agg_seen, "new": out["new"] + agg_new,
            "keywords": kws, "note": note, "mode": "smart"}


# ---------------------------------------------------------------------------
# Background discover task (app.py:367 _discover_task_set,
# app.py:375 _run_smart_discover_bg)
# ---------------------------------------------------------------------------


def _discover_task_set(task_id: str, **kw):
    # 任务状态存 DB（之前存内存，进程重启/多实例时轮询 404）
    try:
        db.update_discover_task(task_id, **kw)
    except Exception:
        pass


def _run_smart_discover_bg(uid: str, task_id: str):
    db.log_agent_event(uid, "amy", "discover_started", "智能抓取开始", task_id)
    try:
        _discover_task_set(task_id, status="running", stage="公司官网 / Company boards", done=0, total=100)
        res = _run_smart_discover(uid, progress=lambda s, d, t: _discover_task_set(task_id, stage=s, done=d, total=t))
        _discover_task_set(task_id, status="done", stage="完成 / Done",
                           done=100, total=100, new=res.get("new", 0), seen=res.get("seen", 0))
        try:
            n_sources = len(db.list_sources(uid))
        except Exception:
            n_sources = 0
        db.log_agent_event(
            uid, "amy", "discover_finished",
            f"抓取完成：新增 {res.get('new', 0)} 个职位，共看到 {res.get('seen', 0)} 个（来源 {n_sources} 个）",
            task_id)
    except Exception as e:
        import traceback as _tb
        err = f"{type(e).__name__}: {e}\n" + "".join(_tb.format_tb(e.__traceback__)[-4:])
        _discover_task_set(task_id, status="error", error=err[:800])
        db.log_agent_event(uid, "amy", "discover_failed", f"抓取失败：{type(e).__name__}: {e}"[:500], task_id)


def start_discover_bg(uid: str, task_id: str):
    """Spawn the background discover thread EXACTLY as app.py discover_auto
    does today: threading.Thread(target=_run_smart_discover_bg,
    args=(uid, task_id), daemon=True). Nothing is started on import."""
    _threading.Thread(target=_run_smart_discover_bg, args=(uid, task_id), daemon=True).start()


# ---------------------------------------------------------------------------
# Route-handler equivalents, plain functions taking uid explicitly
# (app.py:388 discover_auto, app.py:398 discover_status, app.py:465 discover)
# ---------------------------------------------------------------------------


def discover_auto(uid: str):
    """智能抓取：立即返回任务 ID，后台执行，前端轮询进度。"""
    task_id = _uuid.uuid4().hex[:12]
    db.create_discover_task(task_id, uid)
    start_discover_bg(uid, task_id)
    return {"ok": True, "task_id": task_id, "status": "running"}


def discover_status(uid: str, task_id: str):
    t = db.get_discover_task(task_id)
    if not t or t.get("user_id") != uid:
        raise HTTPException(404, "task not found")
    # 残留任务：后台线程随进程重启死亡后，任务会永久停在 running，
    # 前端将无限轮询。正常一次抓取约 2-3 分钟，20 分钟无更新即判中断，
    # 给前端一个终态而不是永远转圈。
    if t.get("status") == "running" and t.get("updated_at") and time.time() - t["updated_at"] > 20 * 60:
        db.update_discover_task(task_id, status="error", stage="任务中断 / Interrupted",
                                error="任务中断：服务器重启或执行超时，请重新点击抓取。")
        t = db.get_discover_task(task_id)
    t.pop("user_id", None)
    t.pop("updated_at", None)
    return {"ok": True, "task_id": task_id, **t}


def discover(uid: str, body: DiscoverIn):
    """从 ATS 公开 API 批量抓取职位，去重入库并打分。"""
    db.log_agent_event(uid, "amy", "discover_started", "手动抓取开始")
    try:
        res = _run_discover(uid, body.sources, body.limit)
    except Exception:
        db.log_agent_event(uid, "amy", "discover_failed", "手动抓取失败")
        raise
    db.log_agent_event(
        uid, "amy", "discover_finished",
        f"手动抓取完成：新增 {res.get('new', 0)} 个职位，共看到 {res.get('seen', 0)} 个")
    return {"ok": True, **res}


# ---------------------------------------------------------------------------
# Agent interface (GET /api/v1/agents)
# ---------------------------------------------------------------------------


def _running_discover_task(uid: str):
    """Latest running discover task, if updated within 20 min. Same query
    and window as app.py _agent_active_evidence (Amy branch)."""
    try:
        conn = db.get_db()
        row = conn.execute(
            "SELECT stage, updated_at FROM discover_tasks WHERE user_id=? AND status='running' "
            "ORDER BY updated_at DESC LIMIT 1", (uid,)).fetchone()
        conn.close()
        if row:
            d = dict(row)
            if d.get("updated_at") and time.time() - float(d["updated_at"]) <= 20 * 60:
                return d
    except Exception:
        pass
    return None


def _jobs_created_today(uid: str) -> int | None:
    """Count of jobs with created_at (REAL epoch seconds) since UTC midnight.
    Returns None if the count cannot be verified."""
    try:
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        conn = db.get_db()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE user_id=? AND created_at >= ?",
            (uid, start_of_day)).fetchone()
        conn.close()
        return int(dict(row)["n"])
    except Exception:
        return None


def status(uid: str) -> dict:
    """Never raises. 'active' ONLY from a running discover task updated
    within 20 min; current_task is its stage string when active."""
    out = {"status": "idle", "current_task": None, "today_stats": [], "last_activity": None}
    try:
        task = _running_discover_task(uid)
        if task:
            stage = (task.get("stage") or "").strip()
            out["status"] = "active"
            out["current_task"] = stage or "正在抓取职位…"
        n = _jobs_created_today(uid)
        # Only report the stat when there is real activity to report (n > 0);
        # a fresh/quiet user reports [] — matches the /api/v1/agents contract
        # (test_agents_endpoint_returns_five_and_requires_auth).
        if n is not None and n > 0:
            out["today_stats"] = [{"label_zh": "今日新增职位", "label_en": "New jobs today", "value": n}]
        events = recent_events(uid, 1)
        if events:
            out["last_activity"] = events[0].get("created_at")
    except Exception:
        pass
    return out


def recent_events(uid: str, limit: int = 5) -> list:
    """Newest first. Never raises."""
    try:
        rows = db.list_agent_events(uid, agent="amy", limit=limit)
        return [{"event": r.get("event", ""), "summary": r.get("summary", ""),
                 "created_at": r.get("created_at"), "ref_id": r.get("ref_id")}
                for r in rows if isinstance(r, dict)]
    except Exception:
        return []
