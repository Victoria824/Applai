"""agents/jennifer.py — Jennifer：简历与市场分析 / Resume & Market Analysis.

Moved as-is from backend/app.py (multi-agent branch, Step 2 — Jennifer lane).
app.py is NOT edited by this lane; the coordinator wires routes to this module later.

Moved from app.py (line numbers verified by grep/ast on 2026-11-05):
    _background_scorer        app.py:68    | score_and_store         app.py:140
    put_profile (#1)          app.py:151   | get_profile             app.py:157
    ProfileCreateIn           app.py:165   | list_profiles           app.py:172
    create_profile            app.py:179   | update_profile          app.py:188
    delete_profile            app.py:197   | activate_profile        app.py:206
    _keywords_for             app.py:264   | get_keywords            app.py:444
    refresh_keywords          app.py:451   | _rescore_pending        app.py:494
    put_profile (#2)          app.py:514   | suggest_job_profile     app.py:852
    _resume_display_name      app.py:1115  | _resume_text_from_profile app.py:1130
    resume_parse              app.py:1348  | ProfileIn               app.py:108
    (scorer thread start       app.py:100-101, inside the startup handler)

put_profile — two variants (see the notes above each definition).
`put_profile` (#1, plain upsert) used to serve PUT /api/v1/profile;
`put_profile_with_reset` (#2, upsert + keywords invalidation + score
reset + background rescore) was unwired. DECISION 2026-11-11 (Victoria):
profile saves MUST reset scores and the keywords cache — the live route
now wires to `put_profile_with_reset`. #1 is kept as an unwired helper
(plain upsert, no reset) and must not be re-wired without her call.

Transformations applied to route handlers ONLY (pure helpers are verbatim):
  - No FastAPI decorators, no Depends, no Request. Handlers take `uid`
    explicitly, e.g. put_profile(uid, body) instead of
    (body, user=Depends(get_current_user)); `user["app_user_id"]` -> `uid`.
  - Pydantic models used by the handlers (ProfileIn, ProfileCreateIn) moved here.
  - HTTPException is still raised exactly as in app.py; resume_parse stays
    async with its UploadFile parameter.
  - Never `import app` (circular). Shared services are plain top-level
    imports (db, llm, matcher, resume_pdf); backend/ is on sys.path.

Agent interface (GET /api/v1/agents lazy-imports this module):
  - status(uid) / recent_events(uid, limit=5) — see below.
  - start_background_scorer() spawns the daemon scorer thread exactly as
    app.py's startup handler does; nothing starts on import.

Event logging (coarse milestones ONLY, via db.log_agent_event, best-effort):
  profile_updated (put_profile_with_reset), keywords_refreshed (refresh_keywords),
  rescore_finished (_rescore_pending), suggest_job_generated,
  resume_parsed. NEVER per-job scoring events — the scorer runs ~1/sec.
  get_keywords is a read path and does NOT log (refresh_keywords is the
  milestone); profiles CRUD does not log either.
"""

import datetime as _datetime
import time

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel, Field

import db
import llm
import matcher
import resume_pdf


def _log(uid: str, event: str, summary: str, ref_id=None) -> None:
    """Coarse milestone event for the team page. Never raises: logging must
    never change the behaviour of the function that calls it."""
    try:
        db.log_agent_event(uid, "jennifer", event, summary, ref_id)
    except Exception:
        pass


def _background_scorer():
    """后台打分守护线程：独立于抓取，持续给未打分职位打分。

    根因修复：之前抓取时同步补打分，导致抓取卡死；现在抓取只入库，打分由这个线程慢慢补。
    """
    import time as _time
    while True:
        try:
            jobs = db.list_unscored_jobs(limit=20)
            if not jobs:
                _time.sleep(30)
                continue
            for job in jobs:
                try:
                    uid = job.get("user_id")
                    profile = db.get_active_profile(uid) or {}
                    if profile:
                        score_and_store(uid, job["id"], profile)
                except Exception:
                    pass
                _time.sleep(1)  # 限流，避免 LLM 调用过密
        except Exception:
            _time.sleep(30)


class ProfileIn(BaseModel):
    user_id: str
    profile: dict


def score_and_store(user_id: str, job_id: int, profile: dict):
    """规则预筛 + LLM 精排，结果写回 DB。"""
    job = db.get_job(job_id)
    rs, rreasons = matcher.score_job(profile, job)
    fs, freasons, via = matcher.hybrid_score(
        profile, {**job, "description": db.job_description(job)}, rs, rreasons)
    db.update_job_score(job_id, fs, freasons, via)
    return fs, freasons, via


# NOTE — put_profile (#1, plain upsert, no reset). Kept as an unwired
# helper; the live PUT /api/v1/profile route wires to
# put_profile_with_reset per Victoria's 2026-11-11 decision. Do not
# re-wire the route to this without her call.
def put_profile(uid: str, body: ProfileIn):
    db.upsert_profile(uid, body.profile)
    _log(uid, "profile_updated", "画像已保存")
    return {"ok": True}


def get_profile(uid: str):
    ap = db.get_active_profile(uid)
    return {"user_id": uid, "profile": ap["profile"],
            "person": db.get_active_person(uid), "job": db.get_active_job(uid),
            "tailor_enabled": ap.get("tailor_enabled", False)}


class ProfileCreateIn(BaseModel):
    name: str
    profile: dict = Field(default_factory=dict)
    kind: str = "job"


def list_profiles(uid: str, kind: str = "job"):
    if kind not in ("person", "job"):
        kind = "job"
    return {"profiles": db.list_profiles(uid, kind)}


def create_profile(uid: str, body: ProfileCreateIn):
    try:
        kind = body.kind if body.kind in ("person", "job") else "job"
        return db.create_profile(uid, body.name, body.profile, kind)
    except ValueError as e:
        raise HTTPException(400, str(e))


def update_profile(uid: str, pid: int, body: dict):
    db.update_profile(pid, uid,
                      profile=body.get("profile"),
                      name=body.get("name"),
                      tailor_enabled=body.get("tailor_enabled"))
    return {"ok": True}


def delete_profile(uid: str, pid: int):
    try:
        db.delete_profile(pid, uid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


def activate_profile(uid: str, pid: int):
    db.activate_profile(pid, uid)
    # 激活后关键词缓存失效（画像变了）
    try:
        db.update_settings(uid, keywords_updated=0)
    except Exception:
        pass
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


def get_keywords(uid: str):
    kws, note = _keywords_for(uid, db.get_profile(uid) or {})
    return {"keywords": kws, "note": note}


def refresh_keywords(uid: str):
    """强制重新生成关键词（画像大改后调用）。"""
    import llm as llm_mod
    profile = db.get_profile(uid) or {}
    gen = llm_mod.llm_job_keywords(profile)
    if gen and gen.get("keywords"):
        db.update_settings(uid, job_keywords=gen["keywords"],
                           keywords_note=gen.get("note", ""), keywords_updated=time.time())
        _log(uid, "keywords_refreshed", "关键词已重新生成")
        return {"keywords": gen["keywords"], "note": gen.get("note", "")}
    raise HTTPException(502, "关键词生成失败，请稍后重试")


def _rescore_pending(uid: str):
    """后台：画像变更后重打所有未处理职位。"""
    try:
        profile = db.get_profile(uid)
        if not profile:
            return
        jobs = db.list_jobs(uid, limit=500)
        done = db.applied_job_ids(uid)
        scored = 0
        for j in jobs:
            if j["id"] in done or j.get("score") is not None:
                continue
            try:
                score_and_store(uid, j["id"], profile)
                scored += 1
            except Exception:
                continue
        _log(uid, "rescore_finished", f"画像变更后重打分完成：{scored} 个职位")
    except Exception:
        pass


# NOTE — put_profile_with_reset (was app.py:514, second of two put_profile
# definitions). Upserts the profile, invalidates the keywords cache
# (keywords_updated=0, so _keywords_for regenerates on next read), resets
# scores of unprocessed jobs (db.reset_scores) and spawns _rescore_pending
# to re-score them in the background.
# DECISION 2026-11-11 (Victoria): profile saves MUST reset scores and
# keywords — this is the LIVE handler for PUT /api/v1/profile.
# The plain-upsert variant put_profile (#1) is kept unwired as a helper.
def put_profile_with_reset(uid: str, body: ProfileIn):
    db.upsert_profile(uid, body.profile)
    # 画像变了：关键词缓存失效 + 未处理职位重打分（后台）
    db.update_settings(uid, keywords_updated=0)
    db.reset_scores(uid)
    import threading
    threading.Thread(target=_rescore_pending, args=(uid,), daemon=True).start()
    _log(uid, "profile_updated", "画像已保存")
    return {"ok": True}


def suggest_job_profile(uid: str):
    """根据人物简历画像，AI 生成目标岗位画像初稿。"""
    person = db.get_active_person(uid).get("profile") or {}
    resume_text = _resume_text_from_profile(person)
    if not resume_text:
        raise HTTPException(400, "请先上传简历")
    sug = llm.llm_suggest_job_profile(resume_text)
    if not sug:
        raise HTTPException(502, "AI 生成暂时失败（已自动重试多次），请点重试或稍后手动填写")
    _log(uid, "suggest_job_generated", "目标岗位画像初稿已生成")
    return {"suggestion": sug}


def _resume_display_name(uid: str) -> str:
    """投递时用的简历文件名：招聘方会看到，必须专业 —— First_Last_Resume.pdf。"""
    import re as _re
    person = (db.get_active_person(uid) or {}).get("profile") or {}

    def clean(v):
        v = _re.sub(r"[^\w\s-]", "", str(v or ""), flags=_re.UNICODE).strip()
        if len(v) > 1 and v.upper() == v and any(c.isalpha() for c in v):
            v = v.title()  # 全大写的名字（EMMA）转成正常大小写（Emma）
        return _re.sub(r"\s+", "_", v)
    first, last = clean(person.get("firstName")), clean(person.get("lastName"))
    name = "_".join(x for x in (first, last) if x)
    return f"{name}_Resume.pdf" if name else "Resume.pdf"


def _resume_text_from_profile(profile: dict) -> str:
    """从画像的简历 PDF 提取文本。"""
    rf = profile.get("resumeFile") or {}
    data_url = rf.get("dataUrl") or ""
    if not data_url or "," not in data_url:
        return ""
    try:
        import base64 as _b64
        from pypdf import PdfReader
        import io as _io
        raw = _b64.b64decode(data_url.split(",", 1)[1])
        reader = PdfReader(_io.BytesIO(raw))
        return "\n".join((p.extract_text() or "") for p in reader.pages)[:8000]
    except Exception:
        return ""


async def resume_parse(uid: str, file: UploadFile):
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
        raise HTTPException(502, "简历识别失败，请手动填写关键信息")
    # 只保留白名单字段
    allowed = ("firstName", "lastName", "email", "phone", "location", "linkedin",
               "github", "website", "targetTitles", "industries", "yearsExperience",
               "preferredLocations", "workAuth", "summary")
    out = {k: fields.get(k) for k in allowed if fields.get(k) not in (None, "", [])}
    _log(uid, "resume_parsed", f"简历已解析：{len(text)} 字符")
    return {"fields": out, "chars": len(text)}


def start_background_scorer():
    """Spawn the background scorer thread EXACTLY as app.py's startup handler
    does today (app.py:100-101): same target, same daemon setting.
    Call explicitly from the coordinator wiring — nothing starts on import."""
    import threading as _threading
    _threading.Thread(target=_background_scorer, daemon=True).start()


def status(uid: str) -> dict:
    """Team-page status. 'active' ONLY from real evidence: a jennifer
    agent_events row within the last hour. The scorer thread runs
    continuously, so its mere existence is NOT evidence of activity.
    today_stats is [] — the jobs table carries no scored-at timestamp and
    db.update_job_score stamps no time, so 'jobs scored today' cannot be
    counted from real data. Never raises."""
    last_activity = None
    try:
        events = db.list_agent_events(uid, agent="jennifer", limit=1)
        if events:
            last_activity = events[0].get("created_at")
    except Exception:
        last_activity = None
    active = False
    if last_activity:
        try:
            dt = _datetime.datetime.fromisoformat(str(last_activity))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_datetime.timezone.utc)
            delta = _datetime.datetime.now(_datetime.timezone.utc) - dt
            active = _datetime.timedelta(0) <= delta <= _datetime.timedelta(hours=1)
        except Exception:
            active = False
    out = {"status": "active" if active else "idle", "today_stats": []}
    if last_activity:
        out["last_activity"] = last_activity
    return out


def recent_events(uid: str, limit: int = 5) -> list:
    """db.agent_events -> endpoint shape. Never raises."""
    try:
        return [{"event": e.get("event", ""), "summary": e.get("summary", ""),
                 "created_at": e.get("created_at"), "ref_id": e.get("ref_id")}
                for e in db.list_agent_events(uid, agent="jennifer", limit=limit)]
    except Exception:
        return []
