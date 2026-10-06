"""db.py — SQLite 持久层（MVP 先用 SQLite，产品化时换 Postgres，SQL 零改动）"""
import json
import os
import sqlite3
import time
from pathlib import Path

DB_PATH = os.environ.get("APPLAI_DB") or str(Path(__file__).parent / "applai.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    profile_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT DEFAULT '',
    company TEXT DEFAULT '',
    location TEXT DEFAULT '',
    source TEXT DEFAULT 'manual',
    raw_json TEXT DEFAULT '{}',
    score REAL DEFAULT 0,
    score_reasons TEXT DEFAULT '[]',
    created_at REAL NOT NULL,
    UNIQUE(user_id, url)
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    job_id INTEGER NOT NULL,
    status TEXT NOT NULL,              -- queued|filled|submitted|needs_manual|failed|skipped
    detail TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(user_id, job_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id);
CREATE INDEX IF NOT EXISTS idx_apps_user ON applications(user_id);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # 轻量迁移：jobs 表加 score_via 列
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "score_via" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN score_via TEXT DEFAULT 'rules'")
        conn.commit()
    return conn


def upsert_profile(user_id: str, profile: dict):
    conn = get_db()
    now = time.time()
    conn.execute(
        """INSERT INTO users(user_id, profile_json, created_at, updated_at)
           VALUES(?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET profile_json=excluded.profile_json, updated_at=excluded.updated_at""",
        (user_id, json.dumps(profile, ensure_ascii=False), now, now),
    )
    conn.commit()
    conn.close()


def get_profile(user_id: str) -> dict:
    conn = get_db()
    row = conn.execute("SELECT profile_json FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return json.loads(row["profile_json"]) if row else {}


def add_job(user_id: str, url: str, title="", company="", location="", source="manual",
            raw=None, description="") -> dict:
    conn = get_db()
    now = time.time()
    raw = dict(raw or {})
    if description:
        raw["description"] = description[:4000]
    try:
        cur = conn.execute(
            """INSERT INTO jobs(user_id, url, title, company, location, source, raw_json, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (user_id, url, title, company, location, source, json.dumps(raw, ensure_ascii=False), now),
        )
        job_id = cur.lastrowid
        is_new = True
    except sqlite3.IntegrityError:
        row = conn.execute("SELECT id FROM jobs WHERE user_id=? AND url=?", (user_id, url)).fetchone()
        job_id = row["id"]
        is_new = False
    conn.commit()
    conn.close()
    return {"id": job_id, "is_new": is_new}


def job_description(job: dict) -> str:
    try:
        raw = json.loads(job.get("raw_json") or "{}")
    except json.JSONDecodeError:
        raw = {}
    return raw.get("description", "")


def update_job_score(job_id: int, score: float, reasons: list, via: str = "rules"):
    conn = get_db()
    conn.execute("UPDATE jobs SET score=?, score_reasons=?, score_via=? WHERE id=?",
                 (score, json.dumps(reasons, ensure_ascii=False), via, job_id))
    conn.commit()
    conn.close()


def get_job(job_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_jobs(user_id: str, limit=200) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE user_id=? ORDER BY score DESC, created_at DESC LIMIT ?", (user_id, limit))
    out = [dict(r) for r in rows]
    conn.close()
    return out


def applied_job_ids(user_id: str) -> set:
    conn = get_db()
    rows = conn.execute(
        "SELECT job_id FROM applications WHERE user_id=? AND status IN ('queued','filled','submitted','needs_manual')",
        (user_id,))
    out = {r["job_id"] for r in rows}
    conn.close()
    return out


def record_application(user_id: str, job_id: int, status: str, detail=""):
    conn = get_db()
    now = time.time()
    conn.execute(
        """INSERT INTO applications(user_id, job_id, status, detail, created_at, updated_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(user_id, job_id) DO UPDATE SET status=excluded.status, detail=excluded.detail, updated_at=excluded.updated_at""",
        (user_id, job_id, status, detail, now, now),
    )
    conn.commit()
    conn.close()


def list_applications(user_id: str, limit=200) -> list:
    conn = get_db()
    rows = conn.execute(
        """SELECT a.*, j.title, j.company, j.url FROM applications a
           JOIN jobs j ON j.id = a.job_id
           WHERE a.user_id=? ORDER BY a.updated_at DESC LIMIT ?""", (user_id, limit))
    out = [dict(r) for r in rows]
    conn.close()
    return out


def stats(user_id: str) -> dict:
    conn = get_db()
    rows = conn.execute(
        "SELECT status, COUNT(*) c FROM applications WHERE user_id=? GROUP BY status", (user_id,))
    out = {r["status"]: r["c"] for r in rows}
    conn.close()
    return out


def list_users() -> list:
    """Dashboard 用户列表：user_id + 画像标签（目标职位），按最近活跃排序。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT user_id, profile_json FROM users ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            p = json.loads(r["profile_json"] or "{}")
        except Exception:
            p = {}
        titles = ", ".join(p.get("targetTitles") or []) or "未填目标职位"
        out.append({"user_id": r["user_id"], "label": titles})
    return out
