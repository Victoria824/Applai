"""Rachel / 瑞秋 — 总管 · 排班 / Supervisor & Scheduler.

Thin supervisor — NO domain work (no fetching, scoring, filling, or email
classification). She owns:
- the daily schedule state (schedule_enabled / schedule_time / daily_count),
- global pause/resume (via Gloria's chat commands, routed through her),
- handoff routing (Gloria's chat commands reach other agents only through
  her — see run_chat_action),
- failure retries (retry_stale_discover re-runs a discover task that went
  stale/error, e.g. killed by a deploy restart).

Topology: agents communicate via DB rows, never direct cross-agent calls.
Rachel is the ONE exception: she routes commands to the right agent's
db-level operations. She works through db.* only, plus ONE lazy import of
agents.amy inside retry_stale_discover (no import cycle — the import happens
at call time, after the module is loaded).

Rules:
- No FastAPI decorators, no Depends, no Request in this module. Route
  handlers become plain functions taking uid explicitly.
- Never `import app` (circular). Shared services are plain top-level
  imports (db) — backend/ is on sys.path.
- Logging: coarse milestones ONLY via db.log_agent_event(uid, "rachel", ...):
  schedule paused/resumed (run_chat_action), stale discover retried
  (retry_stale_discover). Nothing else is logged.

Moved from backend/app.py (branch multi-agent; app.py originals still
present until the coordinator wires the routes to this module):
- get_user_settings / put_user_settings + SettingsIn — app.py:415-441
- _run_chat_action -> run_chat_action — app.py:1529
- retry_stale_discover — NEW helper, no app.py origin (UNWIRED: no route
  or chat action calls it yet; pending the coordinator's decision).
"""

import datetime as _dt
import re

from fastapi import HTTPException
from pydantic import BaseModel

import db


# ---------- Settings（每日排班状态） ----------
# Moved from app.py:415-441. Route handlers become plain functions taking
# uid explicitly; logic identical. The daily schedule itself is driven by
# the extension (chrome.alarms) reading these settings — Rachel owns only
# the STATE, not the trigger.

def get_user_settings(uid: str) -> dict:
    st = db.get_settings(uid)
    st.pop("job_keywords", None)  # 关键词走专属接口
    return st


class SettingsIn(BaseModel):
    schedule_enabled: bool | None = None
    schedule_time: str | None = None
    daily_count: int | None = None


def put_user_settings(uid: str, body: SettingsIn) -> dict:
    kw = {}
    if body.schedule_enabled is not None:
        kw["schedule_enabled"] = body.schedule_enabled
    if body.schedule_time is not None:
        if not re.match(r"^\d{2}:\d{2}$", body.schedule_time):
            raise HTTPException(400, "schedule_time 格式应为 HH:MM")
        kw["schedule_time"] = body.schedule_time
    if body.daily_count is not None:
        kw["daily_count"] = max(1, min(30, int(body.daily_count)))
    st = db.update_settings(uid, **kw)
    st.pop("job_keywords", None)
    return st


# ---------- Chat action routing（Gloria 的聊天指令经 Rachel 路由） ----------
# Moved from app.py:1529 (_run_chat_action -> run_chat_action), verbatim
# except the two log_agent_event calls on pause/resume (the module's only
# logging points, per spec). The `queued` parameter is accepted but unused
# in the body — signature kept as-is.

def run_chat_action(uid: str, act: dict, queued: list) -> str:
    """执行 chatbot 操作，返回中文结果描述。"""
    action = act.get("action")
    if action == "pause_schedule":
        db.update_settings(uid, schedule_enabled=0)
        try:
            db.log_agent_event(uid, "rachel", "schedule_paused", "已暂停自动投递")
        except Exception:
            pass
        return "已暂停自动投递 ✅"
    if action == "resume_schedule":
        db.update_settings(uid, schedule_enabled=1)
        try:
            db.log_agent_event(uid, "rachel", "schedule_resumed", "已恢复自动投递")
        except Exception:
            pass
        return "已恢复自动投递 ✅"
    if action in ("add_to_queue", "mark_applied"):
        wants = act.get("jobs") or []
        if not wants:
            return "没识别出是哪个职位，请说具体一点（比如：把 XX 公司那个加进去）"
        # 在最近职位里模糊匹配
        jobs = db.list_jobs(uid, limit=200) if hasattr(db, "list_jobs") else []
        done, missed = [], []
        for w in wants:
            wl = str(w).lower()
            hit = None
            for j in jobs:
                jl = f"{j.get('title','')} @ {j.get('company','')}".lower()
                if wl in jl or jl in wl:
                    hit = j
                    break
            if not hit:
                missed.append(w)
                continue
            if action == "add_to_queue":
                # 加入手动队列：插到 applications 为 queued
                db.record_application(uid, hit["id"], "queued", "chatbot 手动加入")
            else:
                db.record_application(uid, hit["id"], "submitted", "chatbot 标记已手动投递")
            done.append(f"{hit.get('title')} @ {hit.get('company')}")
        parts = []
        if done:
            verb = "已加入投递队列 ✅" if action == "add_to_queue" else "已标记为已投递 ✅"
            parts.append(verb + "：" + "、".join(done))
        if missed:
            parts.append("没找到：" + "、".join(missed))
        return "\n".join(parts)
    return ""


# ---------- Failure retry（失败重试） ----------
# NEW helper — no app.py origin. UNWIRED: no route or chat action calls it
# yet; pending the coordinator's decision.

def retry_stale_discover(uid: str, task_id: str) -> dict:
    """Re-run a discover task that went stale/error (e.g. killed by a deploy
    restart). Verifies the task belongs to uid, resets it to running, and
    starts Amy's background runner via a lazy import (no import cycle).

    Returns {"ok": True, "task_id": ..., "status": "running"} on success,
    {"ok": False, "error": ...} on failure. Never raises.
    """
    try:
        t = db.get_discover_task(task_id)
    except Exception as e:
        return {"ok": False, "error": f"db error: {e}"}
    if not t or t.get("user_id") != uid:
        return {"ok": False, "error": "task not found"}
    # 先确认 runner 可用再重置：避免任务被重置为 running 后却没有 runner
    # 接手（那样它只会 20 分钟后再次被判 stale）。
    try:
        from agents.amy import start_discover_bg
    except Exception:
        return {"ok": False, "error": "discover runner not available"}
    try:
        db.update_discover_task(
            task_id, status="running", stage="启动中 / Starting…",
            done=0, total=100, new=0, seen=0, error="")
    except Exception as e:
        return {"ok": False, "error": f"db error: {e}"}
    try:
        start_discover_bg(uid, task_id)
    except Exception as e:
        return {"ok": False, "error": f"runner error: {e}"}
    try:
        db.log_agent_event(uid, "rachel", "discover_retried",
                           f"重新抓取 stale 任务 {task_id}", ref_id=task_id)
    except Exception:
        pass
    return {"ok": True, "task_id": task_id, "status": "running"}


# ---------- Agent interface（GET /api/v1/agents 用） ----------

def _newest_rachel_event(uid: str):
    """Newest rachel agent_events row, or None. Never raises."""
    try:
        events = db.list_agent_events(uid, agent="rachel", limit=1)
        return events[0] if events else None
    except Exception:
        return None


def status(uid: str) -> dict:
    """Team-page status. Never raises.

    "active" ONLY from real evidence: a rachel agent_events row within 1h
    (same convention as app.py _agent_active_evidence — created_at is a UTC
    TIMESTAMP). Otherwise "idle".
    today_stats: ONLY numbers verified from real data — the schedule setting
    (daily_count) when schedule_enabled, with schedule_time in the label.
    """
    out = {"status": "idle", "current_task": None, "today_stats": []}
    try:
        ev = _newest_rachel_event(uid)
        if ev:
            created = ev.get("created_at")
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
        st = db.get_settings(uid)
        if st.get("schedule_enabled"):
            label_zh = "今日计划"
            label_en = "Today's plan"
            sched_time = (st.get("schedule_time") or "").strip()
            if sched_time:
                label_zh = f"今日计划 · {sched_time}"
                label_en = f"Today's plan · {sched_time}"
            out["today_stats"] = [{
                "label_zh": label_zh,
                "label_en": label_en,
                "value": int(st.get("daily_count") or 0),
            }]
    except Exception:
        pass
    return out


def recent_events(uid: str, limit: int = 5) -> list:
    """Newest-first rachel events. Never raises."""
    try:
        rows = db.list_agent_events(uid, agent="rachel", limit=limit)
        return [{"event": r.get("event", ""), "summary": r.get("summary", ""),
                 "created_at": r.get("created_at"), "ref_id": r.get("ref_id")}
                for r in rows if isinstance(r, dict)]
    except Exception:
        return []
