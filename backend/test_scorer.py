"""打分线程回归测试（2026-11-11 根因修复）：
- 后台线程必须用画像本身（db.get_profile）打分，不能传 get_active_profile 的包装 dict
- 打成 0 分的职位不再回炉（list_unscored_jobs 语义）
- 坏职位不杀死线程、不阻塞其他职位，且错误进心跳
- 心跳真实更新并通过 status() 透出；监督线程会重启死掉的打分线程

独立测试库：只有在环境还没指定 APPLAI_DB 时才自建（与 test_backend.py 同进程
跑时共用它的库——pytest 按字母顺序先导 test_backend.py，它会先建好库）。
"""
import os
import tempfile
import threading
import time
import uuid

if not os.environ.get("APPLAI_DB"):
    _tmp = tempfile.mkdtemp()
    os.environ["APPLAI_DB"] = os.path.join(_tmp, "scorer_test.db")
    os.environ["APPLAI_COOKIE_SECURE"] = "0"

import db  # noqa: E402
from agents import jennifer  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402

PROFILE = {
    "firstName": "Scorer", "lastName": "Test", "email": "s@test.com",
    "location": "Toronto, ON, Canada",
    "targetTitles": ["AI Engineer", "Machine Learning Engineer"],
    "preferredLocations": ["Toronto", "Remote"],
    "remotePreference": "any",
    "yearsExperience": 5,
}


def _client_with_profile():
    """新小号 + 画像，返回 (client, uid)。"""
    c = TestClient(app.app)
    uname = "scorer_" + uuid.uuid4().hex[:10]
    r = c.post("/api/v1/auth/register", json={"username": uname, "password": "password123"})
    assert r.status_code == 200, r.text
    uid = c.get("/api/v1/profile").json()["user_id"]
    r = c.put("/api/v1/profile", json={"user_id": uid, "profile": dict(PROFILE)})
    assert r.status_code == 200, r.text
    return c, uid


def _seed(uid, title, company, location, idx):
    job = db.add_job(uid, f"https://example.com/scorer/{uid}/{idx}",
                     title, company, location, source="manual")
    assert job, f"seed failed for {title}"
    return job["id"]


def _job_score(job_id):
    """返回 (score, score_via, score_reasons 原始值)。
    注意：新入库的行天生就是 score=0 / via='rules' / reasons='[]'（列默认值），
    所以“从未打分”不能看 score 是否为 None，要看 reasons 是否仍为 '[]'。"""
    conn = db.get_db()
    try:
        row = conn.execute("SELECT score, score_via, score_reasons FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        if not row:
            return (None, None, None)
        d = dict(row)
        return (d["score"], d["score_via"], d["score_reasons"])
    finally:
        conn.close()


def test_score_batch_uses_real_profile(monkeypatch):
    """核心回归：后台打分必须用画像本身。匹配职位应得高分（≥40，进队列），
    不是旧 bug 的 0/25 分。"""
    monkeypatch.setattr(jennifer, "_SCORE_SLEEP", 0)
    c, uid = _client_with_profile()
    good = _seed(uid, "Machine Learning Engineer", "Acme", "Toronto, ON", 1)
    good2 = _seed(uid, "Senior AI Engineer", "Beta", "Remote", 2)
    bad = _seed(uid, "Product Manager", "Epsilon", "New York, NY", 3)
    res = jennifer._score_batch(limit=20)
    assert res["listed"] >= 3
    assert _job_score(good)[0] is not None and _job_score(good)[0] >= 40
    assert _job_score(good2)[0] is not None and _job_score(good2)[0] >= 40
    # 不匹配的职位打 0 分，但已写真实理由，不再回炉
    s, via, reasons = _job_score(bad)
    assert s == 0.0 and via in ("rules", "hybrid")
    assert reasons and reasons != "[]"
    assert bad not in [j["id"] for j in db.list_unscored_jobs(limit=500)]
    assert good not in [j["id"] for j in db.list_unscored_jobs(limit=500)]
    q = c.get("/api/v1/queue")
    assert q.status_code == 200
    qids = [j["id"] for j in q.json().get("jobs", [])]
    assert good in qids and good2 in qids


def test_scored_zero_not_requeued():
    """打成 0 分的职位不再回炉；但 score=0 且从未写过理由的历史遗留行仍要打。
    另：刚入库的新行（score=0 / via='rules' / reasons='[]'，列默认值）
    必须被当作未打分——这是 2026-11-11 复现出的列默认值陷阱。"""
    c, uid = _client_with_profile()
    fresh = _seed(uid, "Product Manager", "Epsilon", "New York, NY", 10)
    assert fresh in [j["id"] for j in db.list_unscored_jobs(limit=500)]
    jid = _seed(uid, "Product Manager", "Epsilon", "New York, NY", 11)
    db.update_job_score(jid, 0.0, ["标题无匹配", "地点不匹配"], "rules")
    assert jid not in [j["id"] for j in db.list_unscored_jobs(limit=500)]
    conn = db.get_db()
    conn.execute("UPDATE jobs SET score=0, score_reasons=NULL, score_via=NULL WHERE id=?",
                 (jid,))
    conn.commit()
    conn.close()
    assert jid in [j["id"] for j in db.list_unscored_jobs(limit=500)]
    assert db.count_unscored_jobs() >= 1


def test_bad_job_does_not_block_others(monkeypatch):
    """坏职位（打分抛错）只记日志 + 进心跳，不阻塞其他职位、不杀死线程。"""
    monkeypatch.setattr(jennifer, "_SCORE_SLEEP", 0)
    c, uid = _client_with_profile()
    good1 = _seed(uid, "Machine Learning Engineer", "Acme", "Toronto, ON", 21)
    bad = _seed(uid, "AI Engineer", "Beta", "Remote", 22)
    good2 = _seed(uid, "AI Engineer", "Gamma", "Toronto, ON", 23)
    orig = jennifer.score_and_store

    def flaky(user_id, job_id, profile):
        if job_id == bad:
            raise RuntimeError("boom: bad job")
        return orig(user_id, job_id, profile)

    monkeypatch.setattr(jennifer, "score_and_store", flaky)
    jennifer._score_batch(limit=20)
    assert _job_score(good1)[0] is not None and _job_score(good1)[0] >= 40
    assert _job_score(good2)[0] is not None and _job_score(good2)[0] >= 40
    # 坏职位没打上分：仍在待打分池里，理由仍是入库默认值 '[]'
    s, via, reasons = _job_score(bad)
    assert reasons in (None, "[]", "")
    assert bad in [j["id"] for j in db.list_unscored_jobs(limit=500)]
    hb = jennifer._hb_snapshot()
    assert hb.get("last_error") and "boom" in hb["last_error"]


def test_heartbeat_and_status(monkeypatch):
    """心跳真实更新：打分后 jobs_scored 涨、last_loop_at 变新；status() 透出打分行。"""
    monkeypatch.setattr(jennifer, "_SCORE_SLEEP", 0)
    c, uid = _client_with_profile()
    before = jennifer._hb_snapshot()["jobs_scored"]
    _seed(uid, "Machine Learning Engineer", "Acme", "Toronto, ON", 31)
    _seed(uid, "AI Engineer", "Beta", "Remote", 32)
    jennifer._score_batch(limit=20)
    hb = jennifer._hb_snapshot()
    assert hb["jobs_scored"] > before
    assert hb.get("last_loop_at")
    st = jennifer.status(uid)
    labels = [s["label_zh"] for s in st["today_stats"]]
    assert "打分线程" in labels
    assert "已打分（本进程累计）" in labels
    assert "待打分" in labels
    assert "上次打分循环" in labels
    pending = [s for s in st["today_stats"] if s["label_zh"] == "待打分"][0]
    assert pending["value"] == str(db.count_unscored_jobs())


def test_thread_scores_jobs_end_to_end():
    """端到端：监督线程 + 打分线程真实跑起来，种子职位进队列。"""
    c, uid = _client_with_profile()
    good = _seed(uid, "Machine Learning Engineer", "Acme", "Toronto, ON", 41)
    good2 = _seed(uid, "AI Engineer", "Beta", "Remote", 42)
    jennifer.start_background_scorer()
    deadline = time.time() + 90
    seen = []
    while time.time() < deadline:
        q = c.get("/api/v1/queue")
        if q.status_code == 200:
            seen = [j["id"] for j in q.json().get("jobs", [])]
            if good in seen and good2 in seen:
                break
        time.sleep(2)
    assert good in seen and good2 in seen, f"jobs never reached queue: {seen}"
    assert jennifer._scorer_alive()


def test_supervisor_restarts_dead_thread(monkeypatch):
    """监督线程会重启死掉的打分线程；start_background_scorer 幂等。"""
    monkeypatch.setattr(jennifer, "_SUPERVISOR_INTERVAL", 1)
    jennifer.start_background_scorer()
    sup_before = jennifer._SUPERVISOR_THREAD
    jennifer.start_background_scorer()
    assert jennifer._SUPERVISOR_THREAD is sup_before
    # 伪造一个已死的打分线程
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    assert not dead.is_alive()
    before_restarts = jennifer._hb_snapshot()["restarts"]
    jennifer._SCORER_THREAD = dead
    deadline = time.time() + 45
    ok = False
    while time.time() < deadline:
        t = jennifer._SCORER_THREAD
        if t is not dead and t is not None and t.is_alive():
            ok = True
            break
        time.sleep(1)
    assert ok, "supervisor did not restart the dead scorer thread"
    assert jennifer._hb_snapshot()["restarts"] > before_restarts
