"""一次性：SQLite(/data/applai.db) → Postgres(DATABASE_URL) 数据迁移。
在 Fly 机器上运行：APPLAI_DB=/data/applai.db python3 migrate_sqlite_to_pg.py
"""
import os
import sqlite3
import sys

SQLITE_PATH = os.environ.get("APPLAI_DB", "/data/applai.db")
PG_URL = os.environ.get("DATABASE_URL", "")

if not PG_URL.startswith("postgres"):
    sys.exit("DATABASE_URL 未设置或不是 postgres")

# 表 → 列（按 SQLite schema 顺序，显式列出避免 * 展开顺序问题）
TABLES = {
    "users": ["user_id", "profile_json", "created_at", "updated_at"],
    "jobs": ["id", "user_id", "url", "title", "company", "location", "source",
             "raw_json", "score", "score_reasons", "created_at", "score_via"],
    "applications": ["id", "user_id", "job_id", "status", "detail", "created_at", "updated_at"],
    "user_settings": ["user_id", "job_keywords", "keywords_note", "keywords_updated",
                      "schedule_enabled", "schedule_time", "daily_count", "updated_at"],
    "push_subscriptions": ["id", "user_id", "endpoint", "p256dh", "auth", "created_at"],
    "job_sources": ["id", "user_id", "type", "key", "created_at"],
    "auth_users": ["id", "app_user_id", "username", "pw_hash", "created_at"],
    "auth_sessions": ["token", "user_id", "created_at", "expires_at"],
    "api_tokens": ["id", "token_hash", "user_id", "name", "created_at", "last_used"],
    "chat_conversations": ["id", "user_id", "title", "created_at", "updated_at"],
    "chat_messages": ["id", "conv_id", "user_id", "role", "content", "created_at"],
    "resume_blobs": ["hash", "filename", "data_url", "size", "created_at"],
    "application_snapshots": ["id", "user_id", "job_id", "job_url", "site", "jd_text",
                              "filled_fields", "salary_info", "cover_letter",
                              "resume_hash", "resume_name", "created_at"],
    "invites": ["code", "created_by", "note", "created_at", "used_by", "used_at"],
}

# 有 SERIAL id 需要重置 sequence 的表
SEQ_TABLES = ["jobs", "applications", "push_subscriptions", "job_sources",
              "auth_users", "api_tokens", "chat_conversations", "chat_messages",
              "application_snapshots"]


def main():
    import psycopg
    lite = sqlite3.connect(SQLITE_PATH)
    lite.row_factory = sqlite3.Row
    pg = psycopg.connect(PG_URL, autocommit=True)
    cur = pg.cursor()

    # 先建表（复用 db.py 的 schema 翻译）
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import db as dbmod
    # 强制走 PG 建表
    os.environ["APPLAI_DB_URL"] = PG_URL
    dbmod.PG_URL = PG_URL
    dbmod.USE_PG = True
    dbmod.get_db().close()

    total = 0
    for table, cols in TABLES.items():
        try:
            rows = lite.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        except Exception as e:
            print(f"SKIP {table}: {e}")
            continue
        if not rows:
            print(f"EMPTY {table}")
            continue
        placeholders = ", ".join(["%s"] * len(cols))
        col_list = ", ".join([f'"{c}"' for c in cols])
        # ON CONFLICT DO NOTHING 保证幂等（重复跑不报错）
        pk = cols[0]
        sql = (f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) '
               f'ON CONFLICT ("{pk}") DO NOTHING')
        data = [tuple(r[c] for c in cols) for r in rows]
        cur.executemany(sql, data)
        print(f"OK {table}: {len(data)} rows")
        total += len(data)

    for table in SEQ_TABLES:
        cur.execute(
            f"SELECT setval(pg_get_serial_sequence('\"{table}\"', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM \"{table}\"), 0) + 1, false)")
    print(f"DONE total={total}")


if __name__ == "__main__":
    main()
