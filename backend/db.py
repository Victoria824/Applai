"""db.py — SQLite 持久层（MVP 先用 SQLite，产品化时换 Postgres，SQL 零改动）"""
import hashlib
import hmac
import json
import os
import secrets
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
CREATE TABLE IF NOT EXISTS user_settings (
  user_id TEXT PRIMARY KEY,
  job_keywords TEXT DEFAULT '[]',
  keywords_note TEXT DEFAULT '',
  keywords_updated REAL DEFAULT 0,
  schedule_enabled INTEGER DEFAULT 0,
  schedule_time TEXT DEFAULT '08:00',
  daily_count INTEGER DEFAULT 10,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  endpoint TEXT UNIQUE NOT NULL,
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS job_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  type TEXT NOT NULL,              -- greenhouse | lever
  key TEXT NOT NULL,               -- board token / 公司名
  created_at REAL NOT NULL,
  UNIQUE(user_id, type, key)
);
"""


AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  app_user_id TEXT UNIQUE NOT NULL,
  username TEXT UNIQUE NOT NULL,
  pw_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS api_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT UNIQUE NOT NULL,
  user_id INTEGER NOT NULL,
  name TEXT DEFAULT '',
  created_at INTEGER NOT NULL,
  last_used INTEGER
);
"""

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executescript(AUTH_SCHEMA)
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


# ================= 账号体系（多用户隔离） =================
_PBKDF2_ITERS = 600_000

def _pw_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERS)
    return f"{salt.hex()}${_PBKDF2_ITERS}${dk.hex()}"

def _pw_verify(stored: str, password: str) -> bool:
    try:
        salt_hex, iters, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False

def _valid_username(username: str) -> bool:
    return bool(username) and 3 <= len(username) <= 32 and \
        all(c.isalnum() or c in "_-" for c in username)

def create_auth_user(username: str, password: str) -> dict:
    """注册。返回 {id, app_user_id, username}；用户名重复/不合法或密码太短抛 ValueError。"""
    username = (username or "").strip().lower()
    if not _valid_username(username):
        raise ValueError("用户名需为 3–32 位字母/数字/_/-")
    if not password or len(password) < 8:
        raise ValueError("密码至少 8 位")
    conn = get_db()
    try:
        app_user_id = "u_" + secrets.token_hex(8)
        now = int(time.time())
        cur = conn.execute(
            "INSERT INTO auth_users (app_user_id, username, pw_hash, created_at) VALUES (?,?,?,?)",
            (app_user_id, username, _pw_hash(password), now))
        conn.commit()
        return {"id": cur.lastrowid, "app_user_id": app_user_id, "username": username}
    except sqlite3.IntegrityError:
        raise ValueError("用户名已被注册")
    finally:
        conn.close()

def get_auth_user(auth_id: int) -> dict | None:
    conn = get_db()
    r = conn.execute("SELECT id, app_user_id, username, created_at FROM auth_users WHERE id=?",
                     (auth_id,)).fetchone()
    conn.close()
    return dict(r) if r else None

def verify_login(username: str, password: str) -> dict | None:
    """登录验证，成功返回 {id, app_user_id, username}，失败返回 None。"""
    username = (username or "").strip().lower()
    conn = get_db()
    r = conn.execute("SELECT id, app_user_id, username, pw_hash FROM auth_users WHERE username=?",
                     (username,)).fetchone()
    conn.close()
    if not r or not _pw_verify(r["pw_hash"], password or ""):
        return None
    return {"id": r["id"], "app_user_id": r["app_user_id"], "username": r["username"]}

SESSION_DAYS = 30

def create_session(auth_id: int) -> str:
    token = secrets.token_hex(32)
    now = int(time.time())
    conn = get_db()
    conn.execute("INSERT INTO auth_sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                 (token, auth_id, now, now + SESSION_DAYS * 86400))
    # 顺手清理过期会话
    conn.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (now,))
    conn.commit()
    conn.close()
    return token

def get_session_user(token: str) -> dict | None:
    if not token:
        return None
    conn = get_db()
    r = conn.execute(
        "SELECT u.id, u.app_user_id, u.username FROM auth_sessions s "
        "JOIN auth_users u ON u.id = s.user_id "
        "WHERE s.token=? AND s.expires_at > ?", (token, int(time.time()))).fetchone()
    conn.close()
    return dict(r) if r else None

def delete_session(token: str):
    conn = get_db()
    conn.execute("DELETE FROM auth_sessions WHERE token=?", (token,))
    conn.commit()
    conn.close()

def create_api_token(auth_id: int, name: str = "") -> tuple[int, str]:
    """创建插件用 API Token。返回 (id, 明文token) ——明文只返回这一次。"""
    raw = "aap_" + secrets.token_hex(32)
    th = hashlib.sha256(raw.encode()).hexdigest()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO api_tokens (token_hash, user_id, name, created_at) VALUES (?,?,?,?)",
        (th, auth_id, (name or "")[:64], int(time.time())))
    conn.commit()
    conn.close()
    return cur.lastrowid, raw

def get_api_token_user(raw_token: str) -> dict | None:
    if not raw_token or not raw_token.startswith("aap_"):
        return None
    th = hashlib.sha256(raw_token.encode()).hexdigest()
    conn = get_db()
    r = conn.execute(
        "SELECT u.id, u.app_user_id, u.username, t.id AS tid FROM api_tokens t "
        "JOIN auth_users u ON u.id = t.user_id WHERE t.token_hash=?", (th,)).fetchone()
    if r:
        conn.execute("UPDATE api_tokens SET last_used=? WHERE id=?", (int(time.time()), r["tid"]))
        conn.commit()
    conn.close()
    return {"id": r["id"], "app_user_id": r["app_user_id"], "username": r["username"]} if r else None

def list_api_tokens(auth_id: int) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, created_at, last_used FROM api_tokens WHERE user_id=? ORDER BY id DESC",
        (auth_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def revoke_api_token(auth_id: int, token_id: int) -> bool:
    conn = get_db()
    cur = conn.execute("DELETE FROM api_tokens WHERE id=? AND user_id=?", (token_id, auth_id))
    conn.commit()
    conn.close()
    return cur.rowcount > 0

def claim_old_data(new_app_user_id: str, old_app_user_id: str) -> dict:
    """把旧 user_id 下的画像/职位/申请记录迁移到新账号。旧 ID 需存在且新账号无数据冲突。"""
    if not old_app_user_id or old_app_user_id == new_app_user_id:
        raise ValueError("旧用户 ID 无效")
    conn = get_db()
    try:
        has = conn.execute("SELECT 1 FROM users WHERE user_id=?", (old_app_user_id,)).fetchone()
        if not has:
            raise ValueError("找不到该旧用户 ID 的数据")
        # 职位 URL 冲突时保留新账号的（新账号刚注册通常为空）
        conn.execute("DELETE FROM jobs WHERE user_id=? AND url IN "
                     "(SELECT url FROM jobs WHERE user_id=?)",
                     (old_app_user_id, new_app_user_id))
        conn.execute("UPDATE users SET user_id=? WHERE user_id=?", (new_app_user_id, old_app_user_id))
        conn.execute("UPDATE jobs SET user_id=? WHERE user_id=?", (new_app_user_id, old_app_user_id))
        conn.execute("UPDATE applications SET user_id=? WHERE user_id=?",
                     (new_app_user_id, old_app_user_id))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ================= 职位来源 =================
def add_source(user_id: str, type_: str, key: str) -> dict:
    type_ = (type_ or "").strip().lower()
    key = (key or "").strip()
    if type_ not in ("greenhouse", "lever", "ashby"):
        raise ValueError("type 仅支持 greenhouse / lever / ashby")
    if not key or len(key) > 128:
        raise ValueError("key 无效")
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO job_sources (user_id, type, key, created_at) VALUES (?,?,?,?)",
            (user_id, type_, key, time.time()))
        conn.commit()
        return {"id": cur.lastrowid, "type": type_, "key": key}
    except sqlite3.IntegrityError:
        raise ValueError("该来源已添加")
    finally:
        conn.close()


def list_sources(user_id: str) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT id, type, key, created_at FROM job_sources WHERE user_id=? ORDER BY id",
        (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def remove_source(user_id: str, source_id: int) -> bool:
    conn = get_db()
    cur = conn.execute("DELETE FROM job_sources WHERE id=? AND user_id=?", (source_id, user_id))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


# ================= 用户设置 =================
def get_settings(user_id: str) -> dict:
    conn = get_db()
    r = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
    if not r:
        conn.execute("INSERT INTO user_settings (user_id, updated_at) VALUES (?,?)",
                     (user_id, time.time()))
        conn.commit()
        r = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    d = dict(r)
    try:
        d["job_keywords"] = json.loads(d.get("job_keywords") or "[]")
    except Exception:
        d["job_keywords"] = []
    return d


def update_settings(user_id: str, **kw) -> dict:
    allowed = {"schedule_enabled", "schedule_time", "daily_count",
               "job_keywords", "keywords_note", "keywords_updated"}
    sets, vals = [], []
    for k, v in kw.items():
        if k not in allowed:
            continue
        if k == "job_keywords":
            v = json.dumps(v or [])
        if k == "schedule_enabled":
            v = 1 if v else 0
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return get_settings(user_id)
    get_settings(user_id)  # 确保行存在
    conn = get_db()
    conn.execute(f"UPDATE user_settings SET {', '.join(sets)}, updated_at=? WHERE user_id=?",
                 (*vals, time.time(), user_id))
    conn.commit()
    conn.close()
    return get_settings(user_id)


# ================= Web Push 订阅 =================
def add_push_subscription(user_id: str, endpoint: str, p256dh: str, auth: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, created_at)"
        " VALUES (?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET"
        " user_id=excluded.user_id, p256dh=excluded.p256dh, auth=excluded.auth",
        (user_id, endpoint, p256dh, auth, time.time()))
    conn.commit()
    conn.close()


def list_push_subscriptions(user_id: str) -> list:
    conn = get_db()
    rows = conn.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id=?",
                        (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def remove_push_subscription(user_id: str, endpoint: str) -> bool:
    conn = get_db()
    cur = conn.execute("DELETE FROM push_subscriptions WHERE user_id=? AND endpoint=?",
                       (user_id, endpoint))
    conn.commit()
    conn.close()
    return cur.rowcount > 0
