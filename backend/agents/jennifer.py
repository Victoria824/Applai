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
import logging as _logging
import threading as _threading
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


# ---------- 后台打分线程 / background scorer (Jennifer) ----------
# 根因 2026-11-11（本地复现确认），两处叠加：
# (1) 旧线程把 db.get_active_profile(uid) 返回的包装 dict（{"id", "profile":
# {...}, ...}）直接传给 score_and_store，而 matcher 要的是画像本身
# （db.get_profile(uid) 的合并 dict）。包装里没有 targetTitles，标题分恒为 0，
# 规则分最高 25，永远进不了 ≥40 的队列；同步路径（amy.add_job）一直用的是
# get_profile，所以同步打分正常（85 分），只有后台线程是错的。
# (2) 列默认值陷阱：jobs 表 score DEFAULT 0、score_via DEFAULT 'rules'、
# score_reasons DEFAULT '[]'——新入库的行天生就长得像“已打成 0 分”。按
# score_via 判“是否打过分”的谓词会把所有新职位都排除在外，线程根本捞不到
# 待打分职位（本地复现：_score_batch listed==0，心跳 loops==0）；而旧的
# score=0 谓词又会把已打 0 分的职位反复捞回、空转。唯一可靠的信号是
# score_reasons 的内容（只有打分成功才会写入非空理由列表）。
# 修复：线程改用 db.get_profile(uid)；list_unscored_jobs 按 score_reasons
# 内容判定未打分；单步抽成 _score_batch 方便测试；加心跳 + 监督线程，
# 静默失败不再可能。
_logger = _logging.getLogger("applai.scorer")

_SCORE_SLEEP = 1.0          # 每打一个职位后的限流等待（秒），测试可 monkeypatch 为 0
_SUPERVISOR_INTERVAL = 30   # 监督线程检查打分线程存活的间隔（秒），测试可调小
_LOOP_IDLE_SLEEP = 30       # 无待打分职位时循环等待（秒）

_HEARTBEAT_LOCK = _threading.Lock()
_HEARTBEAT = {
    "thread_started_at": None,  # ISO UTC：打分线程最近一次启动时间
    "last_loop_at": None,       # ISO UTC：最近一次打分循环完成时间
    "last_error": None,         # 最近一次打分错误（字符串），干净循环后清掉
    "last_error_at": None,      # ISO UTC
    "jobs_scored": 0,           # 本进程累计打分成功数
    "loops": 0,                 # 本进程累计打分循环数
    "restarts": 0,              # 监督线程重启打分线程的次数
}
_SCORER_THREAD = None
_SUPERVISOR_THREAD = None
_SUPERVISOR_LOCK = _threading.Lock()
_SCORER_EVER_STARTED = False


def _now_iso() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def _hb_update(**kw) -> None:
    with _HEARTBEAT_LOCK:
        _HEARTBEAT.update(kw)


def _hb_snapshot() -> dict:
    with _HEARTBEAT_LOCK:
        return dict(_HEARTBEAT)


def _scorer_alive() -> bool:
    t = _SCORER_THREAD
    return t is not None and t.is_alive()


def _score_batch(limit: int = 20) -> dict:
    """单次打分：取最新一批未打分职位逐个打分。返回
    {"listed": n, "scored": n, "skipped": n, "errors": [str]}。
    listed==0 表示无事可做（调用方等 30 秒再试）。单步可独立测试；
    线程循环只是反复调用它。失败的职位记日志、记心跳，不阻塞其他职位。
    心跳由本函数维护——无论调用方是线程循环还是直接调用，每次打分都留下
    真实证据（loops / jobs_scored / last_loop_at / last_error）。"""
    try:
        jobs = db.list_unscored_jobs(limit=limit)
    except Exception as e:
        msg = f"list_unscored_jobs: {e}"
        _logger.warning("scorer: %s", msg)
        _hb_update(last_error=msg[:200], last_error_at=_now_iso(),
                   last_loop_at=_now_iso(), loops=_HEARTBEAT["loops"] + 1)
        return {"listed": 0, "scored": 0, "skipped": 0, "errors": [msg]}
    scored = skipped = 0
    errors = []
    for job in jobs:
        try:
            uid = job.get("user_id")
            # 关键：画像本身（合并 dict），不是 get_active_profile 的包装 dict。
            profile = db.get_profile(uid) or {}
            if not profile:
                skipped += 1
                continue
            score_and_store(uid, job["id"], profile)
            scored += 1
        except Exception as e:
            msg = f"job {job.get('id')}: {e}"
            errors.append(msg)
            _logger.warning("scorer: %s", msg)
        time.sleep(_SCORE_SLEEP)
    with _HEARTBEAT_LOCK:
        _HEARTBEAT["loops"] += 1
        _HEARTBEAT["jobs_scored"] += scored
        _HEARTBEAT["last_loop_at"] = _now_iso()
        if errors:
            _HEARTBEAT["last_error"] = errors[-1][:200]
            _HEARTBEAT["last_error_at"] = _now_iso()
        else:
            _HEARTBEAT["last_error"] = None
            _HEARTBEAT["last_error_at"] = None
    return {"listed": len(jobs), "scored": scored, "skipped": skipped, "errors": errors}


def _background_scorer():
    """后台打分守护线程：独立于抓取，持续给未打分职位打分。
    自己永远不静默死去：任何异常都记日志 + 写心跳，循环继续。
    心跳由 _score_batch 维护；这里只做最后一道防线。"""
    _hb_update(thread_started_at=_now_iso())
    while True:
        try:
            res = _score_batch(limit=20)
            if res["listed"] == 0:
                time.sleep(_LOOP_IDLE_SLEEP)
        except Exception as e:
            msg = f"loop: {e}"
            _logger.warning("scorer: %s", msg)
            _hb_update(last_error=msg[:200], last_error_at=_now_iso())
            time.sleep(_LOOP_IDLE_SLEEP)


def _start_scorer_thread(restart: bool = False) -> None:
    """启动（或重启）打分线程。只由监督线程调用。"""
    global _SCORER_THREAD, _SCORER_EVER_STARTED
    with _SUPERVISOR_LOCK:
        t = _threading.Thread(target=_background_scorer, daemon=True)
        t.start()
        _SCORER_THREAD = t
        _SCORER_EVER_STARTED = True
        if restart:
            with _HEARTBEAT_LOCK:
                _HEARTBEAT["restarts"] += 1


def _supervisor_loop():
    """监督线程：定期确认打分线程还活着，死了就重启并记日志。
    打分线程本身有外层 try/except 几乎不会死；监督是第二道防线。"""
    while True:
        try:
            if not _scorer_alive():
                restart = _SCORER_EVER_STARTED
                _start_scorer_thread(restart=restart)
                if restart:
                    _logger.warning("scorer: thread was dead, restarted")
        except Exception as e:
            _logger.warning("scorer supervisor error: %s", e)
        time.sleep(_SUPERVISOR_INTERVAL)


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
    """启动后台打分（监督线程；监督线程立即启动打分线程）。
    幂等：重复调用不会启动第二个监督线程。应用启动时由 app.py 调用；
    import 时不启动任何线程。"""
    global _SUPERVISOR_THREAD
    with _SUPERVISOR_LOCK:
        if _SUPERVISOR_THREAD is not None and _SUPERVISOR_THREAD.is_alive():
            return
        t = _threading.Thread(target=_supervisor_loop, daemon=True)
        t.start()
        _SUPERVISOR_THREAD = t


def _rel_time(iso) -> str:
    """ISO 时间 → '刚刚 / N秒前 / N分钟前 / N小时前 / N天前'；解析失败返回原文。"""
    if not iso:
        return "从未运行"
    try:
        dt = _datetime.datetime.fromisoformat(str(iso))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_datetime.timezone.utc)
        secs = (_datetime.datetime.now(_datetime.timezone.utc) - dt).total_seconds()
    except Exception:
        return str(iso)
    if secs < 10:
        return "刚刚"
    if secs < 60:
        return f"{int(secs)}秒前"
    if secs < 3600:
        return f"{int(secs // 60)}分钟前"
    if secs < 86400:
        return f"{int(secs // 3600)}小时前"
    return f"{int(secs // 86400)}天前"


def status(uid: str) -> dict:
    """Team-page status. 'active' ONLY from real evidence: a jennifer
    agent_events row within the last hour, OR the scorer thread having
    looped within the last 10 minutes (heartbeat — the thread loops at
    least every 30s when idle, so a stale loop means it is stuck/dead).
    today_stats 包含打分心跳（线程存活、上次循环、累计打分数、待打分数、
    最近错误）——全部来自真实数据。Never raises."""
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
    hb = _hb_snapshot()
    alive = _scorer_alive()
    loop_fresh = False
    last_loop = hb.get("last_loop_at")
    if last_loop:
        try:
            dt = _datetime.datetime.fromisoformat(str(last_loop))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_datetime.timezone.utc)
            delta = _datetime.datetime.now(_datetime.timezone.utc) - dt
            loop_fresh = _datetime.timedelta(0) <= delta <= _datetime.timedelta(minutes=10)
        except Exception:
            loop_fresh = False
    if alive and loop_fresh:
        active = True
    out = {"status": "active" if active else "idle", "today_stats": []}
    if last_activity:
        out["last_activity"] = last_activity
    elif last_loop:
        out["last_activity"] = last_loop
    # 打分心跳（全局，不分用户；全部来自真实数据，失败也不影响本函数）。
    # 只在有真实证据时才报：线程从未启动、一次循环都没跑过时，today_stats
    # 保持为空——与“无活动时统计为空”的 Team 页约定一致；线上进程启动时
    # 线程即随之启动，所以生产环境这几行恒在。
    has_hb = (hb.get("thread_started_at") is not None
              or hb.get("last_loop_at") is not None
              or hb.get("jobs_scored", 0) > 0
              or hb.get("last_error") is not None)
    pending = None
    if has_hb:
        stats = [
            {"label_zh": "打分线程", "label_en": "Scorer thread",
             "value": "运行中" if alive else "未运行"},
            {"label_zh": "已打分（本进程累计）", "label_en": "Jobs scored (this process)",
             "value": str(hb.get("jobs_scored", 0))},
        ]
        pending = None
        try:
            pending = db.count_unscored_jobs()
            stats.append({"label_zh": "待打分", "label_en": "Pending scoring",
                          "value": str(pending)})
        except Exception:
            pending = None
        stats.append({"label_zh": "上次打分循环", "label_en": "Last scorer loop",
                      "value": _rel_time(last_loop)})
        if hb.get("last_error"):
            stats.append({"label_zh": "最近打分错误", "label_en": "Last scorer error",
                          "value": str(hb["last_error"])[:60]})
        out["today_stats"] = stats
    # current_task 只在真正“正在干活”时才报：线程存活、循环新鲜、且有待打分
    # 队列。线程未运行不是“当前任务”，是健康状态——today_stats 里的“打分线程”
    # 一行已经报了“未运行”，监督线程负责重启；空闲时 current_task 必须为 None。
    if alive and loop_fresh and pending:
        out["current_task"] = f"正在打分（待打分 {pending} 个）"
    return out


def recent_events(uid: str, limit: int = 5) -> list:
    """db.agent_events -> endpoint shape. Never raises."""
    try:
        return [{"event": e.get("event", ""), "summary": e.get("summary", ""),
                 "created_at": e.get("created_at"), "ref_id": e.get("ref_id")}
                for e in db.list_agent_events(uid, agent="jennifer", limit=limit)]
    except Exception:
        return []
