"""agents/caroline.py — Caroline · 填表投递 / Application Specialist.

Moved as-is from backend/app.py (Step 4, Caroline lane, branch multi-agent).
Her "hands" are the extension (untouched, 0.2.14); her records are backend.
app.py still holds its own copies until the coordinator wires the routes to
this module — this file must therefore be self-contained and never
``import app`` (circular).

Source map (backend/app.py line numbers as of the move, 2026-11-05):
    _queue_jobs            ~471    get_queue              ~484
    SnapshotIn             ~525    post_snapshot          ~538
    RetryIn                ~574    retry_application      ~578
    RunProgressIn          ~594    post_run_progress      ~605
    read_run_progress      ~612    get_snapshot           ~617
    get_resume_blob        ~628    ApplicationIn          ~127
    post_application       ~641    get_applications       ~658
    get_stats              ~668    tailor_resume          ~1147
    _generate_tailored     ~1168   _maybe_auto_tailor     ~1199
    resume_for_job         ~1224   list_resume_versions   ~1263
    _resume_display_name   ~1115   _resume_text_from_profile ~1130
    _tailor_inflight       ~1196

Move rules applied:
- No FastAPI decorators, no Depends, no Request. Route handlers became plain
  functions taking ``uid`` explicitly as the FIRST argument
  (e.g. ``post_snapshot(uid, body)``); pure helpers kept identical signatures.
- Pydantic models moved verbatim (SnapshotIn, RetryIn, RunProgressIn,
  ApplicationIn). HTTPException raised exactly as in app.py.
- Shared services are imported, not rewritten: db, llm, resume_pdf, and
  send_web_push from agents.shared.push (already moved there by Foundation).
- Helpers _resume_display_name / _resume_text_from_profile live in
  agents.jennifer (resume domain) — the tailor paths delegate to them
  via lazy import (single implementation, no copy to drift).
- _queue_jobs filters on the STORED job score (>= 40) exactly as app.py does
  today; it does NOT call matcher.build_queue (which recomputes rule scores
  from the profile and is not what the queue endpoint uses).

Deliberate addition (the only behaviour change vs app.py): coarse milestone
events via db.log_agent_event, best-effort (never breaks the apply flow):
run finished ("run_finished", on run-progress state="done"), application
recorded ("application_recorded"), tailored resume generated
("tailored_generated", new versions only, not cached hits), retry executed
("retry_executed"). Never per-field fill details.
"""
import datetime as _dt
import re
import threading
import time

from fastapi import HTTPException
from pydantic import BaseModel, Field

import db
import llm
import resume_pdf
from agents.shared.push import send_web_push

__all__ = [
    "SnapshotIn", "RetryIn", "RunProgressIn", "ApplicationIn",
    "_queue_jobs", "get_queue",
    "post_snapshot", "retry_application",
    "post_run_progress", "read_run_progress",
    "get_snapshot", "get_resume_blob",
    "post_application", "get_applications", "get_stats",
    "tailor_resume", "_generate_tailored", "_maybe_auto_tailor",
    "resume_for_job", "list_resume_versions",
    "_resume_display_name", "_resume_text_from_profile",
    "status", "recent_events",
]


def _log_event(uid: str, event: str, summary: str, ref_id=None) -> None:
    """Coarse milestone only. Best effort — logging must never break apply."""
    try:
        db.log_agent_event(uid, "caroline", event, summary, ref_id)
    except Exception:
        pass


# ---------- 队列 / Queue ----------

def _queue_jobs(uid: str, top_n: int = 10) -> list:
    """待投递队列：chatbot 手动加入的（status='queued'）最优先，其余按混合
    分数排序并排除已处理过的职位。"""
    jobs = db.list_jobs(uid, limit=500)
    by_id = {j["id"]: j for j in jobs}
    done = db.applied_job_ids(uid)
    manual = [by_id[i] for i in db.queued_job_ids(uid) if i in by_id]
    fresh = [j for j in jobs
             if j["id"] not in done and (j.get("score") or 0) >= 40]
    fresh.sort(key=lambda j: (-(j.get("score") or 0), j.get("created_at", 0)))
    return (manual + fresh)[:top_n]


def get_queue(uid: str, top_n: int = 10):
    """今日投递队列：只读，无副作用。"""
    if not db.get_profile(uid):
        raise HTTPException(400, "profile not found, sync profile first")
    queue = _queue_jobs(uid, top_n)
    return {"user_id": uid, "count": len(queue), "jobs": queue}


# ---------- 投递留痕 / Snapshots ----------

class SnapshotIn(BaseModel):
    user_id: str
    job_id: int
    job_url: str = ""
    site: str = ""
    filled_fields: list = Field(default_factory=list)  # [{intent, label, value}]
    salary_info: str = ""
    cover_letter: str = ""
    resume_name: str = ""
    resume_hash: str = ""
    resume_data_url: str = ""


def post_snapshot(uid: str, body: SnapshotIn):
    """投递留痕：保存某次投递的快照（表单填写、薪资、求职信、简历）。"""
    # 简历内容寻址存储（去重）
    if body.resume_hash and body.resume_data_url:
        db.save_resume_blob(body.resume_hash, body.resume_name, body.resume_data_url)
    # JD 从职位库冻结一份
    job = db.get_job(body.job_id) or {}
    jd_text = ""
    try:
        jd_text = db.job_description({**job, "raw_json": job.get("raw_json")}) or ""
    except Exception:
        pass
    if not jd_text:
        jd_text = (job.get("description") or "")
    site = body.site
    if not site and body.job_url:
        try:
            from urllib.parse import urlparse
            site = urlparse(body.job_url).hostname or ""
        except Exception:
            pass
    db.save_snapshot(uid, body.job_id, {
        "job_url": body.job_url or job.get("url", ""),
        "site": site or job.get("source", ""),
        "jd_text": jd_text,
        "filled_fields": body.filled_fields,
        "salary_info": body.salary_info,
        "cover_letter": body.cover_letter,
        "resume_hash": body.resume_hash,
        "resume_name": body.resume_name,
    })
    return {"ok": True}


# ---------- 重试 / Retry ----------

class RetryIn(BaseModel):
    job_id: int


def retry_application(uid: str, body: RetryIn):
    """把一条「失败」的投递记录删掉，职位随即回到队列等下次投递（只动 failed 状态）。"""
    conn = db.get_db()
    try:
        cur = conn.execute(
            "DELETE FROM applications WHERE user_id=? AND job_id=? AND status='failed'",
            (uid, body.job_id))
        removed = cur.rowcount or 0
        conn.commit()
    finally:
        conn.close()
    _log_event(uid, "retry_executed",
               f"重试：删除 {removed} 条失败投递记录", ref_id=str(body.job_id))
    return {"ok": True, "removed": removed}


# ---------- 跑批进度 / Run progress ----------

class RunProgressIn(BaseModel):
    state: str = "running"
    total: int = 0
    processed: int = 0
    submitted: int = 0
    manual: int = 0
    failed: int = 0
    current_job_id: int = 0
    current_title: str = ""


def post_run_progress(uid: str, body: RunProgressIn):
    """插件投递进度上报（每步一次，失败不影响投递本身）。"""
    db.upsert_run_progress(uid, body.model_dump())
    if body.state == "done":
        _log_event(uid, "run_finished",
                   f"跑批完成：已投递 {body.submitted}，待人工 {body.manual}，"
                   f"失败 {body.failed}")
    return {"ok": True}


def read_run_progress(uid: str):
    return {"progress": db.get_run_progress(uid)}


# ---------- 快照 / 简历读取 ----------

def get_snapshot(uid: str, job_id: int):
    snap = db.get_snapshot(uid, job_id)
    if not snap:
        raise HTTPException(404, "snapshot not found")
    # 简历正文不直接返回，前端按需下载
    snap["has_resume"] = bool(snap.get("resume_hash"))
    return {"snapshot": snap}


def get_resume_blob(uid: str, file_hash: str):
    # 简单校验 hash 格式，避免路径遍历
    if not re.fullmatch(r"[0-9a-f]{16,128}", file_hash or ""):
        raise HTTPException(400, "bad hash")
    if not db.user_owns_resume_hash(uid, file_hash):
        raise HTTPException(404, "resume not found")
    blob = db.get_resume_blob_data(file_hash)
    if not blob:
        raise HTTPException(404, "resume not found")
    return blob


# ---------- 投递记录 / Applications ----------

class ApplicationIn(BaseModel):
    user_id: str
    job_id: int
    status: str  # queued|filled|submitted|needs_manual|failed|skipped
    detail: str = ""


def post_application(uid: str, body: ApplicationIn):
    if body.status not in ("queued", "filled", "submitted", "needs_manual", "failed", "skipped"):
        raise HTTPException(400, "invalid status")
    db.record_application(uid, body.job_id, body.status, body.detail)
    try:
        job = db.get_job(body.job_id) or {}
        title = (job.get("title") or "").strip()
        company = (job.get("company") or "").strip()
        label = f"{title} @ {company}" if (title or company) else f"岗位 {body.job_id}"
        _log_event(uid, "application_recorded",
                   f"投递记录：{label} — {body.status}", ref_id=str(body.job_id))
    except Exception:
        pass
    if body.status == "needs_manual":
        _maybe_auto_tailor(uid, body.job_id, body.detail)
        try:
            job = db.get_job(body.job_id) or {}
            send_web_push(uid, "Applai：有职位需要人工处理",
                          f"{job.get('title', '')} @ {job.get('company', '')} — {body.detail[:80]}")
        except Exception:
            pass
    return {"ok": True}


def get_applications(uid: str, limit: int = 200):
    apps = db.list_applications(uid, limit)
    for a in apps:
        if a.get("status") == "needs_manual":
            _maybe_auto_tailor(uid, a.get("job_id"), a.get("detail") or "")
    return {"user_id": uid, "applications": apps}


def get_stats(uid: str):
    return {"user_id": uid, "stats": db.stats(uid)}


# ---------- 按岗定制简历 / Tailored resume ----------

def _resume_display_name(uid: str) -> str:
    """投递时用的简历文件名 — canonical in agents.jennifer; delegate."""
    from agents.jennifer import _resume_display_name as _j_display_name
    return _j_display_name(uid)


def _resume_text_from_profile(profile: dict) -> str:
    """从画像的简历 PDF 提取文本 — canonical in agents.jennifer; delegate."""
    from agents.jennifer import _resume_text_from_profile as _j_resume_text
    return _j_resume_text(profile)


def tailor_resume(uid: str, body: dict):
    """按岗位改写简历。body: {job_id, profile_id?}。返回版本信息。"""
    job_id = body.get("job_id")
    if not job_id:
        raise HTTPException(400, "job_id 必填")
    pid = body.get("profile_id")
    if pid:
        profs = [p for p in db.list_profiles(uid) if p["id"] == pid]
        if not profs:
            raise HTTPException(404, "画像不存在")
        ap = profs[0]
        profile = ap["profile"]
    else:
        ap = db.get_active_profile(uid)
        profile = ap.get("profile", {})
        pid = ap.get("id", 0)
    return _generate_tailored(uid, job_id, pid, profile)


def _generate_tailored(uid, job_id, pid, profile):
    """生成（或取已有）按岗定制简历版本。手动端点与后台自动生成共用。"""
    # 已有改写版本直接返回（幂等）
    existing = db.get_resume_version(uid, pid, job_id)
    if existing:
        return {"version_id": existing["id"], "pdf_hash": existing["pdf_hash"], "cached": True}
    # 取 JD 和简历文本
    job = db.get_job(job_id) or {}
    jd = db.job_description(job) or ""
    resume_text = _resume_text_from_profile(profile)
    if not resume_text:
        raise HTTPException(400, "画像里没有可解析的简历 PDF")
    if not jd:
        raise HTTPException(400, "岗位没有描述，无法改写")
    tailored = llm.llm_tailor_resume(resume_text, jd)
    if not tailored:
        raise HTTPException(502, "AI 改写失败，请稍后重试")
    # 生成 PDF 并存储
    import base64 as _b64
    import hashlib as _hl
    pdf_bytes = resume_pdf.generate_pdf(tailored, profile)
    pdf_b64 = "data:application/pdf;base64," + _b64.b64encode(pdf_bytes).decode()
    pdf_hash = _hl.sha256(pdf_b64.encode()).hexdigest()
    db.save_resume_blob(pdf_hash, _resume_display_name(uid), pdf_b64)
    vid = db.save_resume_version(uid, pid, job_id, "tailored", tailored, pdf_hash)
    _log_event(uid, "tailored_generated",
               f"定制简历已生成（岗位 {job_id}）", ref_id=str(vid))
    return {"version_id": vid, "pdf_hash": pdf_hash, "cached": False}


_tailor_inflight = set()


def _maybe_auto_tailor(uid, job_id, detail):
    """待人工且待确认包含简历时，后台自动生成 1 页定制简历（Victoria 要求：
    投递环节自动完成，用户不需要再单独点生成）。打开职位页自动重填时，
    for-job 会优先取这份定制版附上。失败静默——重填会退回母版简历。"""
    if not job_id or "resume" not in (detail or "").lower():
        return
    key = (uid, job_id)
    if key in _tailor_inflight:
        return
    _tailor_inflight.add(key)

    def _run():
        try:
            ap = db.get_active_profile(uid)
            pid = ap.get("id", 0)
            if not db.get_resume_version(uid, pid, job_id):
                _generate_tailored(uid, job_id, pid, ap.get("profile", {}))
        except Exception:
            pass
        finally:
            _tailor_inflight.discard(key)

    threading.Thread(target=_run, daemon=True).start()


def resume_for_job(uid: str, job_id: int):
    """投递时取该用的简历：画像开了改写 → 返回改写版 PDF；否则返回母版。
    返回 {pdf_hash, filename, version_id(0=母版), tailored: bool}。
    插件再用 pdf_hash 下载。"""
    ap = db.get_active_profile(uid)
    pid = ap.get("id", 0)
    profile = ap.get("profile", {})
    # 已有定制版（无论自动开关）优先用——用户手动生成过就是明确意图
    v = db.get_resume_version(uid, pid, job_id)
    if not v and ap.get("tailor_enabled"):
            # 现场生成（约 20-40 秒）
            try:
                job = db.get_job(job_id) or {}
                jd = db.job_description(job) or ""
                resume_text = _resume_text_from_profile(profile)
                tailored = llm.llm_tailor_resume(resume_text, jd) if (resume_text and jd) else None
                if tailored:
                    import base64 as _b64
                    import hashlib as _hl
                    pdf_bytes = resume_pdf.generate_pdf(tailored, profile)
                    pdf_b64 = "data:application/pdf;base64," + _b64.b64encode(pdf_bytes).decode()
                    pdf_hash = _hl.sha256(pdf_b64.encode()).hexdigest()
                    db.save_resume_blob(pdf_hash, _resume_display_name(uid), pdf_b64)
                    vid = db.save_resume_version(uid, pid, job_id, "tailored", tailored, pdf_hash)
                    v = {"id": vid, "pdf_hash": pdf_hash}
                    _log_event(uid, "tailored_generated",
                               f"定制简历已生成（岗位 {job_id}）", ref_id=str(vid))
            except Exception:
                v = None
    if v:
        return {"pdf_hash": v["pdf_hash"], "filename": _resume_display_name(uid),
                "version_id": v["id"], "tailored": True}
    # 母版
    rf = profile.get("resumeFile") or {}
    return {"pdf_hash": "", "filename": rf.get("name", ""),
            "version_id": 0, "tailored": False,
            "data_url": rf.get("dataUrl", "")}


def list_resume_versions(uid: str, profile_id: int = 0):
    """列出某画像的简历版本。"""
    conn = db.get_db()
    if profile_id:
        rows = conn.execute(
            "SELECT id, job_id, kind, created_at FROM resume_versions "
            "WHERE user_id=? AND profile_id=? ORDER BY id DESC LIMIT 50",
            (uid, profile_id)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, job_id, kind, created_at FROM resume_versions "
            "WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,)).fetchall()
    conn.close()
    return {"versions": [dict(r) for r in rows]}


# ---------- Agent 接口 / Agent interface ----------

def status(uid: str) -> dict:
    """Team-page status. "active" only from real evidence (mirrors
    app._agent_active_evidence for caroline): run_progress state=running
    updated within 20 min, or a caroline agent_event within 1h.
    today_stats come only from real DB counts (submitted today by UTC day,
    current needs_manual total); entries are included only when their value
    is > 0, so a user with no activity reports [] (per the agents-endpoint
    contract: 无活动时统计为空). Never raises."""
    entry = {"status": "idle", "current_task": None, "today_stats": [],
             "last_activity": None}
    try:
        prog = db.get_run_progress(uid) or {}
        if prog.get("state") == "running":
            upd = prog.get("updated_at") or 0
            if not upd or time.time() - float(upd) <= 20 * 60:
                title = (prog.get("current_title") or "").strip()
                entry["status"] = "active"
                entry["current_task"] = f"正在投递：{title}" if title else "正在跑批投递…"
    except Exception:
        pass
    if entry["status"] != "active":
        try:
            events = db.list_agent_events(uid, agent="caroline", limit=1)
            if events:
                created = events[0].get("created_at")
                if created:
                    dt = _dt.datetime.fromisoformat(str(created))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=_dt.timezone.utc)
                    delta = _dt.datetime.now(_dt.timezone.utc) - dt
                    if _dt.timedelta(0) <= delta <= _dt.timedelta(hours=1):
                        entry["status"] = "active"
        except Exception:
            pass
    try:
        day_start = _dt.datetime.now(_dt.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        apps = db.list_applications(uid, 200)
        submitted_today = sum(
            1 for a in apps
            if a.get("status") == "submitted"
            and (a.get("updated_at") or 0) >= day_start)
        needs_manual = (db.stats(uid) or {}).get("needs_manual", 0) or 0
        stats = []
        if submitted_today > 0:
            stats.append({"label_zh": "今日已投递", "label_en": "Submitted today",
                          "value": int(submitted_today)})
        if needs_manual > 0:
            stats.append({"label_zh": "待人工", "label_en": "Needs manual review",
                          "value": int(needs_manual)})
        entry["today_stats"] = stats
    except Exception:
        entry["today_stats"] = []
    try:
        events = db.list_agent_events(uid, agent="caroline", limit=1)
        if events and events[0].get("created_at"):
            entry["last_activity"] = events[0]["created_at"]
    except Exception:
        pass
    return entry


def recent_events(uid: str, limit: int = 5) -> list:
    """Newest first. Never raises."""
    try:
        return [{"event": e.get("event", ""), "summary": e.get("summary", ""),
                 "created_at": e.get("created_at"), "ref_id": e.get("ref_id")}
                for e in db.list_agent_events(uid, agent="caroline", limit=limit)]
    except Exception:
        return []
