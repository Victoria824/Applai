"""db.py — SQLite 持久层（MVP 先用 SQLite，产品化时换 Postgres，SQL 零改动）"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

DB_PATH = os.environ.get("APPLAI_DB") or str(Path(__file__).parent / "applai.db")
PG_URL = os.environ.get("APPLAI_DB_URL", "") or os.environ.get("DATABASE_URL", "")
USE_PG = PG_URL.startswith("postgres")

try:
    from psycopg import errors as _pg_errors
    _IntegrityError = (sqlite3.IntegrityError, _pg_errors.UniqueViolation)
except ImportError:
    _IntegrityError = (sqlite3.IntegrityError,)

# 有自增 id 列的表（PG 下 INSERT 自动加 RETURNING id）
_ID_TABLES = {"jobs", "applications", "push_subscriptions", "job_sources",
              "auth_users", "api_tokens", "chat_conversations", "chat_messages",
              "application_snapshots"}


def _pg_schema(sql: str) -> str:
    """SQLite DDL → Postgres DDL（小子集翻译）。"""
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql)
    return sql


class _PGCursor:
    """让 psycopg 游标长得像 sqlite3 游标：? 占位符 + lastrowid。"""

    def __init__(self, cur):
        self._cur = cur
        self._lastrowid = None

    def execute(self, sql, params=()):
        pg_sql = sql.replace("?", "%s")
        m = re.match(r"\s*INSERT\s+INTO\s+(\w+)", sql, re.I)
        if m and m.group(1).lower() in _ID_TABLES and "RETURNING" not in sql.upper():
            pg_sql += " RETURNING id"
            self._cur.execute(pg_sql, params)
            row = self._cur.fetchone()
            self._lastrowid = row["id"] if row else None
        else:
            self._cur.execute(pg_sql, params)
        return self

    @property
    def lastrowid(self):
        return self._lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)


class _PGConn:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        return _PGCursor(self._conn.cursor()).execute(sql, params)

    def executescript(self, sql):
        # PG 没有 executescript：按分号拆分执行
        with self._conn.cursor() as cur:
            for stmt in sql.split(";"):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(_pg_schema(stmt))

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

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
CREATE TABLE IF NOT EXISTS chat_conversations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  title TEXT DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conv_id INTEGER NOT NULL,
  user_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_conv_user ON chat_conversations(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_msg_conv ON chat_messages(conv_id, id);
CREATE TABLE IF NOT EXISTS resume_blobs (
  hash TEXT PRIMARY KEY,
  filename TEXT DEFAULT '',
  data_url TEXT NOT NULL,
  size INTEGER DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS application_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  job_id INTEGER NOT NULL,
  job_url TEXT DEFAULT '',
  site TEXT DEFAULT '',
  jd_text TEXT DEFAULT '',
  filled_fields TEXT DEFAULT '[]',
  salary_info TEXT DEFAULT '',
  cover_letter TEXT DEFAULT '',
  resume_hash TEXT DEFAULT '',
  resume_name TEXT DEFAULT '',
  created_at REAL NOT NULL,
  UNIQUE(user_id, job_id)
);
CREATE INDEX IF NOT EXISTS idx_snap_user ON application_snapshots(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS invites (
  code TEXT PRIMARY KEY,
  created_by TEXT NOT NULL,
  note TEXT DEFAULT '',
  created_at REAL NOT NULL,
  used_by TEXT DEFAULT '',
  used_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_invites_creator ON invites(created_by, created_at DESC);
CREATE TABLE IF NOT EXISTS gmail_tokens (
  user_id TEXT PRIMARY KEY,
  email TEXT DEFAULT '',
  refresh_token TEXT NOT NULL,
  connected_at REAL NOT NULL,
  last_sync REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS gmail_oauth_states (
  state TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS email_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  gmail_msg_id TEXT NOT NULL,
  kind TEXT NOT NULL,              -- interview | rejection | other
  company TEXT DEFAULT '',
  job_title TEXT DEFAULT '',
  subject TEXT DEFAULT '',
  summary TEXT DEFAULT '',
  job_id INTEGER DEFAULT 0,       -- 关联到的 application/job
  created_at REAL NOT NULL,
  UNIQUE(user_id, gmail_msg_id)
);
CREATE INDEX IF NOT EXISTS idx_email_events_user ON email_events(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  name TEXT NOT NULL,
  profile_json TEXT NOT NULL DEFAULT '{}',
  is_active INTEGER DEFAULT 0,
  tailor_enabled INTEGER DEFAULT 0,   -- 是否按岗位自动优化简历
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_profiles_user ON profiles(user_id);
CREATE TABLE IF NOT EXISTS resume_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  profile_id INTEGER NOT NULL,
  job_id INTEGER DEFAULT 0,
  kind TEXT DEFAULT 'master',          -- master | tailored
  content_json TEXT NOT NULL DEFAULT '{}',
  pdf_hash TEXT DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resume_ver ON resume_versions(user_id, profile_id, job_id);
"""

def _pg_fix_sequences(pg):
    """修复迁移后序列不同步：把每个 SERIAL id 序列拨到 MAX(id)+1。"""
    try:
        tbls = pg.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()
        for t in tbls:
            tbl = t["tablename"]
            try:
                seq = pg.execute(
                    "SELECT pg_get_serial_sequence(%s, 'id')", (tbl,)).fetchone()
                seqname = seq["pg_get_serial_sequence"] if seq else None
                if not seqname:
                    continue
                pg.execute(
                    f"SELECT setval(%s, COALESCE((SELECT MAX(id) FROM {tbl}), 0) + 1, false)",
                    (seqname,))
            except Exception:
                continue
        pg.commit()
    except Exception:
        pass


def get_db():
    if USE_PG:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(PG_URL, row_factory=dict_row, autocommit=False)
        pg = _PGConn(conn)
        pg.executescript(SCHEMA)
        pg.executescript(AUTH_SCHEMA)
        # 轻量迁移：jobs 表加 score_via 列
        cols = {r["column_name"] for r in pg.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='jobs'").fetchall()}
        if "score_via" not in cols:
            pg.execute("ALTER TABLE jobs ADD COLUMN score_via TEXT DEFAULT 'rules'")
            pg.commit()
        # Phase 4: 快照表加简历版本列
        scols = {r["column_name"] for r in pg.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='application_snapshots'").fetchall()}
        if "resume_version_id" not in scols:
            pg.execute("ALTER TABLE application_snapshots ADD COLUMN resume_version_id INTEGER DEFAULT 0")
            pg.commit()
        if "resume_tailored" not in scols:
            pg.execute("ALTER TABLE application_snapshots ADD COLUMN resume_tailored INTEGER DEFAULT 0")
            pg.commit()
        # 画像拆分：kind 列 + 老数据拆分
        pcols = {r["column_name"] for r in pg.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='profiles'").fetchall()}
        if "kind" not in pcols:
            pg.execute("ALTER TABLE profiles ADD COLUMN kind TEXT DEFAULT 'job'")
            pg.commit()
        try:
            _migrate_split_profiles(pg)
            pg.commit()
        except Exception:
            pg.rollback()
        _pg_fix_sequences(pg)
        return pg
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executescript(AUTH_SCHEMA)
    # 轻量迁移：jobs 表加 score_via 列
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "score_via" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN score_via TEXT DEFAULT 'rules'")
        conn.commit()
    # Phase 4: 快照表加简历版本列
    scols = {r[1] for r in conn.execute("PRAGMA table_info(application_snapshots)").fetchall()}
    if "resume_version_id" not in scols:
        conn.execute("ALTER TABLE application_snapshots ADD COLUMN resume_version_id INTEGER DEFAULT 0")
    if "resume_tailored" not in scols:
        conn.execute("ALTER TABLE application_snapshots ADD COLUMN resume_tailored INTEGER DEFAULT 0")
    conn.commit()
    # 画像拆分：person（简历） vs job（目标岗位）
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(profiles)").fetchall()}
    if "kind" not in pcols:
        conn.execute("ALTER TABLE profiles ADD COLUMN kind TEXT DEFAULT 'job'")
        conn.commit()
    try:
        _migrate_split_profiles(conn)
        conn.commit()
    except Exception:
        conn.rollback()
    return conn


PERSON_KEYS = {"firstName", "lastName", "email", "phone", "location", "linkedin",
               "github", "website", "resumeFile", "coverLetter"}


def _migrate_split_profiles(conn):
    """老画像（job 行里混着 person 字段）拆成 person + job 两条。
    按内容识别：kind='job' 但 profile_json 里含 person 专属字段的，就是没拆过的老数据。"""
    rows = conn.execute("SELECT id, user_id, name, profile_json, is_active, tailor_enabled "
                        "FROM profiles WHERE kind='job'").fetchall()
    # 只处理真正混着 person 字段的行
    todo = []
    for r in rows:
        try:
            pj = json.loads(dict(r)["profile_json"] or "{}")
        except Exception:
            continue
        if any(k in pj for k in PERSON_KEYS):
            todo.append(r)
    rows = todo
    now = time.time()
    for r in rows:
        d = dict(r)
        try:
            pj = json.loads(d["profile_json"] or "{}")
        except Exception:
            pj = {}
        person = {k: v for k, v in pj.items() if k in PERSON_KEYS}
        job = {k: v for k, v in pj.items() if k not in PERSON_KEYS}
        # 原行变为 job 画像
        conn.execute("UPDATE profiles SET kind='job', profile_json=? WHERE id=?",
                     (json.dumps(job, ensure_ascii=False), d["id"]))
        # person 数据：没有就建一条，有的就合并（首个非空值 wins）
        prow = conn.execute("SELECT id, profile_json FROM profiles WHERE user_id=? AND kind='person' LIMIT 1",
                            (d["user_id"],)).fetchone()
        if not prow:
            pname = "我的简历"
            taken = {r2["name"] for r2 in conn.execute(
                "SELECT name FROM profiles WHERE user_id=?", (d["user_id"],)).fetchall()}
            if pname in taken:
                pname = f"我的简历-{d['id']}"
            conn.execute(
                "INSERT INTO profiles(user_id, name, kind, profile_json, is_active, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (d["user_id"], pname, "person", json.dumps(person, ensure_ascii=False),
                 1, now, now))
        elif person:
            try:
                existing = json.loads(dict(prow)["profile_json"] or "{}")
            except Exception:
                existing = {}
            merged_p = dict(existing)
            for k, v in person.items():
                if k not in merged_p or not merged_p[k]:
                    merged_p[k] = v
            conn.execute("UPDATE profiles SET profile_json=?, updated_at=? WHERE id=?",
                         (json.dumps(merged_p, ensure_ascii=False), now, dict(prow)["id"]))
    if rows:
        conn.commit()


def upsert_profile(user_id: str, profile: dict):
    """向后兼容：按字段归属拆分，分别更新 person / job 画像。"""
    person = {k: v for k, v in (profile or {}).items() if k in PERSON_KEYS}
    job = {k: v for k, v in (profile or {}).items() if k not in PERSON_KEYS}
    ap = get_active_person(user_id)
    aj = get_active_job(user_id)
    if person:
        update_profile(ap["id"], user_id, profile={**(ap.get("profile") or {}), **person})
    if job or not person:
        update_profile(aj["id"], user_id, profile={**(aj.get("profile") or {}), **job})


def get_profile(user_id: str) -> dict:
    """向后兼容：返回当前激活画像的内容。"""
    return get_active_profile(user_id).get("profile", {})


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
    except _IntegrityError:
        try:
            conn.rollback()
        except Exception:
            pass
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


def reset_scores(uid: str) -> int:
    """画像变更后：未处理职位的分数清零，等待重打分。返回受影响行数。"""
    conn = get_db()
    cur = conn.execute(
        """UPDATE jobs SET score=NULL, score_reasons=NULL, score_via=NULL WHERE user_id=?
           AND id NOT IN (SELECT job_id FROM applications WHERE user_id=? AND status IN ('queued','filled','submitted','needs_manual'))""",
        (uid, uid))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def create_conversation(uid: str, title: str = "") -> dict:
    conn = get_db()
    now = time.time()
    cur = conn.execute(
        "INSERT INTO chat_conversations(user_id, title, created_at, updated_at) VALUES(?,?,?,?)",
        (uid, title or "新对话", now, now))
    cid = cur.lastrowid
    conn.commit()
    conn.close()
    return {"id": cid, "title": title or "新对话"}


def list_conversations(uid: str, limit: int = 50) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, updated_at FROM chat_conversations WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",
        (uid, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_conversation(uid: str, cid: int):
    conn = get_db()
    r = conn.execute(
        "SELECT id, title FROM chat_conversations WHERE id=? AND user_id=?", (cid, uid)).fetchone()
    conn.close()
    return dict(r) if r else None


def delete_conversation(uid: str, cid: int):
    conn = get_db()
    conn.execute("DELETE FROM chat_messages WHERE conv_id=? AND user_id=?", (cid, uid))
    conn.execute("DELETE FROM chat_conversations WHERE id=? AND user_id=?", (cid, uid))
    conn.commit()
    conn.close()


def add_chat_message(uid: str, cid: int, role: str, content: str):
    conn = get_db()
    now = time.time()
    conn.execute(
        "INSERT INTO chat_messages(conv_id, user_id, role, content, created_at) VALUES(?,?,?,?,?)",
        (cid, uid, role, content, now))
    conn.execute("UPDATE chat_conversations SET updated_at=? WHERE id=?", (now, cid))
    conn.commit()
    conn.close()


def get_chat_messages(uid: str, cid: int, limit: int = 200) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT role, content FROM chat_messages WHERE conv_id=? AND user_id=? ORDER BY id ASC LIMIT ?",
        (cid, uid, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def rename_conversation(uid: str, cid: int, title: str):
    conn = get_db()
    conn.execute("UPDATE chat_conversations SET title=? WHERE id=? AND user_id=?",
                 (title[:60], cid, uid))
    conn.commit()
    conn.close()


def save_resume_blob(file_hash: str, filename: str, data_url: str) -> bool:
    """内容寻址存简历，已存在则跳过（去重）。"""
    if not file_hash or not data_url:
        return False
    conn = get_db()
    exists = conn.execute("SELECT 1 FROM resume_blobs WHERE hash=?", (file_hash,)).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO resume_blobs(hash, filename, data_url, size, created_at) VALUES(?,?,?,?,?)",
            (file_hash, filename or "", data_url, len(data_url), time.time()))
        conn.commit()
    conn.close()
    return True


def get_resume_blob(file_hash: str):
    conn = get_db()
    r = conn.execute("SELECT hash, filename, size, created_at FROM resume_blobs WHERE hash=?",
                     (file_hash,)).fetchone()
    conn.close()
    return dict(r) if r else None


def get_resume_blob_data(file_hash: str):
    conn = get_db()
    r = conn.execute("SELECT data_url, filename FROM resume_blobs WHERE hash=?", (file_hash,)).fetchone()
    conn.close()
    return dict(r) if r else None


def save_snapshot(uid: str, job_id: int, snap: dict):
    conn = get_db()
    now = time.time()
    conn.execute(
        """INSERT INTO application_snapshots(user_id, job_id, job_url, site, jd_text, filled_fields,
           salary_info, cover_letter, resume_hash, resume_name,
           resume_version_id, resume_tailored, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(user_id, job_id) DO UPDATE SET job_url=excluded.job_url, site=excluded.site,
           jd_text=excluded.jd_text, filled_fields=excluded.filled_fields, salary_info=excluded.salary_info,
           cover_letter=excluded.cover_letter, resume_hash=excluded.resume_hash,
           resume_name=excluded.resume_name, resume_version_id=excluded.resume_version_id,
           resume_tailored=excluded.resume_tailored, created_at=excluded.created_at""",
        (uid, job_id, snap.get("job_url", ""), snap.get("site", ""), snap.get("jd_text", "")[:8000],
         json.dumps(snap.get("filled_fields", []), ensure_ascii=False)[:20000],
         snap.get("salary_info", "")[:500], snap.get("cover_letter", "")[:8000],
         snap.get("resume_hash", ""), snap.get("resume_name", ""),
         snap.get("resume_version_id", 0), 1 if snap.get("resume_tailored") else 0, now))
    conn.commit()
    conn.close()


def get_snapshot(uid: str, job_id: int):
    conn = get_db()
    r = conn.execute(
        "SELECT * FROM application_snapshots WHERE user_id=? AND job_id=?", (uid, job_id)).fetchone()
    conn.close()
    if not r:
        return None
    d = dict(r)
    try:
        d["filled_fields"] = json.loads(d.get("filled_fields") or "[]")
    except Exception:
        d["filled_fields"] = []
    d.pop("id", None)
    return d


def snapshot_job_ids(uid: str) -> set:
    conn = get_db()
    rows = conn.execute("SELECT job_id FROM application_snapshots WHERE user_id=?", (uid,)).fetchall()
    conn.close()
    return {r["job_id"] for r in rows}


def create_invite(created_by: str, note: str = "") -> dict:
    code = secrets.token_hex(4).upper()  # 8 位可读码
    conn = get_db()
    conn.execute("INSERT INTO invites(code, created_by, note, created_at) VALUES(?,?,?,?)",
                 (code, created_by, note or "", time.time()))
    conn.commit()
    conn.close()
    return {"code": code}


def list_invites(created_by: str) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT code, note, created_at, used_by, used_at FROM invites WHERE created_by=? ORDER BY created_at DESC",
        (created_by,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # 好友进度：画像 / 简历 / 插件 Token
        if d["used_by"]:
            prof = get_profile(d["used_by"]) or {}
            has_resume = bool((prof.get("resumeFile") or {}).get("name"))
            toks = conn.execute(
                "SELECT 1 FROM api_tokens t JOIN auth_users u ON u.id=t.user_id WHERE u.app_user_id=? LIMIT 1",
                (d["used_by"],)).fetchone()
            # 用户名
            un = conn.execute("SELECT username FROM auth_users WHERE app_user_id=?",
                              (d["used_by"],)).fetchone()
            d["friend"] = (un["username"] if un else "?")
            d["has_profile"] = bool(prof.get("targetTitles"))
            d["has_resume"] = has_resume
            d["has_token"] = bool(toks)
        out.append(d)
    conn.close()
    return out


def redeem_invite(code: str, used_by: str) -> bool:
    """使用邀请码。返回是否成功（码存在且未被用过）。"""
    conn = get_db()
    r = conn.execute("SELECT used_by FROM invites WHERE code=?", (code.upper(),)).fetchone()
    if not r or r["used_by"]:
        conn.close()
        return False
    conn.execute("UPDATE invites SET used_by=?, used_at=? WHERE code=?",
                 (used_by, time.time(), code.upper()))
    conn.commit()
    conn.close()
    return True


def export_user_data(uid: str) -> dict:
    """导出用户全部数据（JSON）。"""
    conn = get_db()
    data = {"app_user_id": uid, "exported_at": time.time()}
    data["profile"] = get_profile(uid)
    data["settings"] = get_settings(uid)
    for tbl in ("jobs", "applications", "application_snapshots", "chat_conversations", "chat_messages", "invites"):
        try:
            if tbl == "chat_conversations":
                rows = conn.execute("SELECT id, title, created_at, updated_at FROM chat_conversations WHERE user_id=?", (uid,)).fetchall()
            elif tbl == "chat_messages":
                rows = conn.execute(
                    "SELECT m.role, m.content, m.created_at FROM chat_messages m JOIN chat_conversations c ON c.id=m.conv_id WHERE c.user_id=? ORDER BY m.id", (uid,)).fetchall()
            elif tbl == "application_snapshots":
                rows = conn.execute(
                    "SELECT job_id, job_url, site, filled_fields, salary_info, cover_letter, resume_name, created_at FROM application_snapshots WHERE user_id=?", (uid,)).fetchall()
            elif tbl == "invites":
                rows = conn.execute("SELECT code, note, created_at, used_by, used_at FROM invites WHERE created_by=?", (uid,)).fetchall()
            else:
                rows = conn.execute(f"SELECT * FROM {tbl} WHERE user_id=?", (uid,)).fetchall()
            data[tbl] = [dict(r) for r in rows]
        except Exception:
            data[tbl] = []
    # 简历 blob（只导自己快照引用到的）
    hashes = {s.get("resume_hash") for s in data.get("application_snapshots", []) if s.get("resume_hash")}
    prof = data.get("profile") or {}
    rf = prof.get("resumeFile") or {}
    data["resume_blobs"] = []
    conn.close()
    return data


def delete_user_data(uid: str):
    """级联删除用户全部数据。返回删除的表行数统计。"""
    conn = get_db()
    stats = {}
    # 先找 auth id（删 token/session）
    au = conn.execute("SELECT id FROM auth_users WHERE app_user_id=?", (uid,)).fetchone()
    auth_id = au["id"] if au else None
    for tbl, col in [("application_snapshots", "user_id"), ("applications", "user_id"),
                     ("jobs", "user_id"), ("chat_messages", "user_id"),
                     ("chat_conversations", "user_id"), ("invites", "created_by")]:
        try:
            cur = conn.execute(f"DELETE FROM {tbl} WHERE {col}=?", (uid,))
            stats[tbl] = cur.rowcount
        except Exception:
            pass
    # 该用户创建的邀请码一起删
    try:
        conn.execute("DELETE FROM invites WHERE created_by=?", (uid,))
    except Exception:
        pass
    for tbl in ("users", "user_settings", "profiles", "resume_versions"):
        try:
            cur = conn.execute(f"DELETE FROM {tbl} WHERE user_id=?", (uid,))
            stats[tbl] = cur.rowcount
        except Exception:
            pass
    if auth_id:
        for tbl in ("api_tokens", "auth_sessions"):
            try:
                cur = conn.execute(f"DELETE FROM {tbl} WHERE user_id=?", (auth_id,))
                stats[tbl] = cur.rowcount
            except Exception:
                pass
        try:
            cur = conn.execute("DELETE FROM auth_users WHERE id=?", (auth_id,))
            stats["auth_users"] = cur.rowcount
        except Exception:
            pass
    conn.commit()
    conn.close()
    return stats


def get_job_by_url(user_id: str, url: str):
    conn = get_db()
    r = conn.execute("SELECT * FROM jobs WHERE user_id=? AND url=?", (user_id, url)).fetchone()
    conn.close()
    return dict(r) if r else None


def save_gmail_state(state: str, user_id: str):
    conn = get_db()
    conn.execute("INSERT INTO gmail_oauth_states(state, user_id, created_at) VALUES(?,?,?)",
                 (state, user_id, time.time()))
    # 清理过期（1小时前）
    conn.execute("DELETE FROM gmail_oauth_states WHERE created_at < ?", (time.time() - 3600,))
    conn.commit()
    conn.close()


def consume_gmail_state(state: str):
    conn = get_db()
    r = conn.execute("SELECT user_id FROM gmail_oauth_states WHERE state=?", (state,)).fetchone()
    if r:
        conn.execute("DELETE FROM gmail_oauth_states WHERE state=?", (state,))
        conn.commit()
    conn.close()
    return r["user_id"] if r else None


def save_gmail_token(user_id: str, email: str, refresh_token: str):
    conn = get_db()
    conn.execute(
        """INSERT INTO gmail_tokens(user_id, email, refresh_token, connected_at, last_sync)
           VALUES(?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET email=excluded.email,
           refresh_token=excluded.refresh_token, connected_at=excluded.connected_at""",
        (user_id, email, refresh_token, time.time(), 0))
    conn.commit()
    conn.close()


def get_gmail_token(user_id: str):
    conn = get_db()
    r = conn.execute("SELECT * FROM gmail_tokens WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return dict(r) if r else None


def delete_gmail_token(user_id: str):
    conn = get_db()
    conn.execute("DELETE FROM gmail_tokens WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def touch_gmail_sync(user_id: str):
    conn = get_db()
    conn.execute("UPDATE gmail_tokens SET last_sync=? WHERE user_id=?", (time.time(), user_id))
    conn.commit()
    conn.close()


def gmail_users() -> list:
    """所有连了 Gmail 的用户（cron 用）。"""
    conn = get_db()
    rows = conn.execute("SELECT user_id, email, refresh_token FROM gmail_tokens").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_email_event(user_id: str, msg_id: str, kind: str, company: str,
                    job_title: str, subject: str, summary: str, job_id: int = 0) -> bool:
    """返回 True=新事件，False=已存在。"""
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO email_events(user_id, gmail_msg_id, kind, company, job_title,
               subject, summary, job_id, created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (user_id, msg_id, kind, company or "", job_title or "", subject or "",
             summary or "", job_id or 0, time.time()))
        conn.commit()
        is_new = True
    except _IntegrityError:
        is_new = False
    conn.close()
    return is_new


def list_email_events(user_id: str, limit: int = 50) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM email_events WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _ensure_default_profile(user_id: str) -> dict:
    """确保 person 和 job 各有一个激活画像（老用户走 _migrate_split_profiles）。"""
    conn = get_db()
    now = time.time()
    for kind, name in (("person", "我的简历"), ("job", "默认画像")):
        r = conn.execute("SELECT id FROM profiles WHERE user_id=? AND kind=? LIMIT 1",
                         (user_id, kind)).fetchone()
        if not r:
            # 尝试从旧 users 表拿
            pj = {}
            if kind == "job":
                old = conn.execute("SELECT profile_json FROM users WHERE user_id=?",
                                   (user_id,)).fetchone()
                if old:
                    try:
                        full = json.loads(old["profile_json"] or "{}")
                    except Exception:
                        full = {}
                    pj = {k: v for k, v in full.items() if k not in PERSON_KEYS}
            conn.execute(
                "INSERT INTO profiles(user_id, name, kind, profile_json, is_active, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (user_id, name, kind, json.dumps(pj, ensure_ascii=False), 1, now, now))
    conn.commit()
    r = conn.execute("SELECT id FROM profiles WHERE user_id=? AND kind='job' AND is_active=1 LIMIT 1",
                     (user_id,)).fetchone()
    conn.close()
    return {"id": r["id"] if r else 0}


def list_profiles(user_id: str, kind: str = "job") -> list:
    _ensure_default_profile(user_id)
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, profile_json, is_active, tailor_enabled, created_at, updated_at "
        "FROM profiles WHERE user_id=? AND kind=? ORDER BY is_active DESC, id",
        (user_id, kind)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["profile"] = json.loads(d.pop("profile_json") or "{}")
        except Exception:
            d["profile"] = {}
        d["is_active"] = bool(d["is_active"])
        d["tailor_enabled"] = bool(d["tailor_enabled"])
        out.append(d)
    conn.close()
    return out


def _get_active_one(user_id: str, kind: str) -> dict:
    _ensure_default_profile(user_id)
    conn = get_db()
    r = conn.execute(
        "SELECT id, name, profile_json, tailor_enabled FROM profiles "
        "WHERE user_id=? AND kind=? AND is_active=1 LIMIT 1", (user_id, kind)).fetchone()
    conn.close()
    if not r:
        return {"id": 0, "name": "", "profile": {}, "tailor_enabled": False}
    d = dict(r)
    try:
        d["profile"] = json.loads(d.pop("profile_json") or "{}")
    except Exception:
        d["profile"] = {}
    d["tailor_enabled"] = bool(d["tailor_enabled"])
    return d


def get_active_person(user_id: str) -> dict:
    return _get_active_one(user_id, "person")


def get_active_job(user_id: str) -> dict:
    return _get_active_one(user_id, "job")


def get_active_profile(user_id: str) -> dict:
    """向后兼容：返回 person+job 合并后的画像。LLM/匹配/插件都读这个。"""
    person = get_active_person(user_id)
    job = get_active_job(user_id)
    merged = dict(person.get("profile") or {})
    merged.update(job.get("profile") or {})
    return {"id": job.get("id", 0), "person_id": person.get("id", 0),
            "name": job.get("name", ""), "person_name": person.get("name", ""),
            "profile": merged, "tailor_enabled": job.get("tailor_enabled", False)}


def create_profile(user_id: str, name: str, profile: dict = None, kind: str = "job") -> dict:
    _ensure_default_profile(user_id)
    # 同 kind 下同名不允许
    conn = get_db()
    dup = conn.execute("SELECT 1 FROM profiles WHERE user_id=? AND kind=? AND name=? LIMIT 1",
                       (user_id, kind, name[:50])).fetchone()
    if dup:
        conn.close()
        raise ValueError("同名画像已存在")
    now = time.time()
    cur = conn.execute(
        "INSERT INTO profiles(user_id, name, kind, profile_json, is_active, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (user_id, name[:50], kind, json.dumps(profile or {}, ensure_ascii=False), 0, now, now))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return {"id": pid}


def update_profile(profile_id: int, user_id: str, profile: dict = None,
                   name: str = None, tailor_enabled: bool = None):
    conn = get_db()
    sets, vals = [], []
    if profile is not None:
        sets.append("profile_json=?")
        vals.append(json.dumps(profile, ensure_ascii=False))
    if name:
        sets.append("name=?")
        vals.append(name[:50])
    if tailor_enabled is not None:
        sets.append("tailor_enabled=?")
        vals.append(1 if tailor_enabled else 0)
    if sets:
        sets.append("updated_at=?")
        vals.append(time.time())
        vals.extend([profile_id, user_id])
        conn.execute(f"UPDATE profiles SET {', '.join(sets)} WHERE id=? AND user_id=?", vals)
        conn.commit()
    conn.close()


def activate_profile(profile_id: int, user_id: str):
    conn = get_db()
    r = conn.execute("SELECT kind FROM profiles WHERE id=? AND user_id=?",
                     (profile_id, user_id)).fetchone()
    kind = r["kind"] if r else "job"
    conn.execute("UPDATE profiles SET is_active=0 WHERE user_id=? AND kind=?", (user_id, kind))
    conn.execute("UPDATE profiles SET is_active=1 WHERE id=? AND user_id=?", (profile_id, user_id))
    conn.commit()
    conn.close()


def delete_profile(profile_id: int, user_id: str):
    conn = get_db()
    r = conn.execute("SELECT is_active FROM profiles WHERE id=? AND user_id=?",
                     (profile_id, user_id)).fetchone()
    if r and r["is_active"]:
        conn.close()
        raise ValueError("不能删除正在使用的画像")
    conn.execute("DELETE FROM profiles WHERE id=? AND user_id=?", (profile_id, user_id))
    conn.commit()
    conn.close()


def save_resume_version(user_id: str, profile_id: int, job_id: int,
                        kind: str, content: dict, pdf_hash: str = "") -> int:
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO resume_versions(user_id, profile_id, job_id, kind, content_json, pdf_hash, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (user_id, profile_id, job_id, kind, json.dumps(content, ensure_ascii=False),
         pdf_hash, time.time()))
    vid = cur.lastrowid
    conn.commit()
    conn.close()
    return vid


def get_resume_version(user_id: str, profile_id: int, job_id: int):
    """取某画像针对某岗位的改写版本（没有返回 None）。"""
    conn = get_db()
    r = conn.execute(
        "SELECT * FROM resume_versions WHERE user_id=? AND profile_id=? AND job_id=? "
        "AND kind='tailored' ORDER BY id DESC LIMIT 1",
        (user_id, profile_id, job_id)).fetchone()
    conn.close()
    if not r:
        return None
    d = dict(r)
    try:
        d["content"] = json.loads(d.get("content_json") or "{}")
    except Exception:
        d["content"] = {}
    return d


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
    snaps = {r["job_id"] for r in conn.execute(
        "SELECT job_id FROM application_snapshots WHERE user_id=?", (user_id,)).fetchall()}
    conn.close()
    for o in out:
        o["has_snapshot"] = o["job_id"] in snaps
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
    except _IntegrityError:
        # 到底是用户名重复，还是 id 序列冲突？查清楚再报错
        try:
            conn.rollback()
        except Exception:
            pass
        r = conn.execute("SELECT 1 FROM auth_users WHERE username=?", (username,)).fetchone()
        conn.close()
        if r:
            raise ValueError("用户名已被注册")
        raise ValueError("注册失败请重试")
    finally:
        try:
            conn.close()
        except Exception:
            pass

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
        has_p = conn.execute("SELECT 1 FROM profiles WHERE user_id=?", (old_app_user_id,)).fetchone()
        if not has and not has_p:
            raise ValueError("找不到该旧用户 ID 的数据")
        # 职位 URL 冲突时保留新账号的（新账号刚注册通常为空）
        conn.execute("DELETE FROM jobs WHERE user_id=? AND url IN "
                     "(SELECT url FROM jobs WHERE user_id=?)",
                     (old_app_user_id, new_app_user_id))
        conn.execute("UPDATE users SET user_id=? WHERE user_id=?", (new_app_user_id, old_app_user_id))
        # 新账号的自动默认画像是空的，删掉再接管旧画像（避免同名冲突）
        if has_p:
            conn.execute("DELETE FROM profiles WHERE user_id=?", (new_app_user_id,))
        conn.execute("UPDATE profiles SET user_id=? WHERE user_id=?", (new_app_user_id, old_app_user_id))
        conn.execute("UPDATE jobs SET user_id=? WHERE user_id=?", (new_app_user_id, old_app_user_id))
        conn.execute("UPDATE applications SET user_id=? WHERE user_id=?",
                     (new_app_user_id, old_app_user_id))
        conn.execute("UPDATE resume_versions SET user_id=? WHERE user_id=?",
                     (new_app_user_id, old_app_user_id))
        conn.execute("UPDATE application_snapshots SET user_id=? WHERE user_id=?",
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
    except _IntegrityError:
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
