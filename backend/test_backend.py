"""后端测试：pytest test_backend.py"""
import os
import tempfile

# 测试用独立数据库
tmp = tempfile.mkdtemp()
os.environ["APPLAI_DB"] = os.path.join(tmp, "test.db")

import db
db.DB_PATH = os.path.join(tmp, "test.db")

from fastapi.testclient import TestClient
import app

client = TestClient(app.app)
UID = "test_user_1"
PROFILE = {
    "firstName": "Victoria", "lastName": "Liu", "email": "v@test.com",
    "location": "Toronto, ON, Canada",
    "targetTitles": ["AI Engineer", "Machine Learning Engineer"],
    "preferredLocations": ["Toronto", "Remote"],
    "remotePreference": "any",
    "workAuth": "I am authorized to work in Canada",
    "needsSponsorship": False,
}


def test_profile_roundtrip():
    r = client.put("/api/v1/profile", json={"user_id": UID, "profile": PROFILE})
    assert r.status_code == 200
    r = client.get("/api/v1/profile", params={"user_id": UID})
    assert r.json()["profile"]["email"] == "v@test.com"


def test_add_job_scores():
    r = client.post("/api/v1/jobs", json={
        "user_id": UID, "url": "https://boards.greenhouse.io/acme/jobs/1",
        "title": "Senior AI Engineer", "company": "Acme", "location": "Toronto, ON"})
    assert r.status_code == 200
    d = r.json()
    assert d["score"] >= 40, d  # 标题 AI Engineer 全匹配 60 + 地点 Toronto 25


def test_add_job_dedup():
    url = "https://boards.greenhouse.io/acme/jobs/1"
    r1 = client.post("/api/v1/jobs", json={"user_id": UID, "url": url, "title": "Senior AI Engineer"})
    r2 = client.post("/api/v1/jobs", json={"user_id": UID, "url": url, "title": "Senior AI Engineer"})
    assert r1.json()["id"] == r2.json()["id"]
    assert r2.json()["is_new"] is False


def test_queue_excludes_applied():
    # 再加一个低分职位
    client.post("/api/v1/jobs", json={
        "user_id": UID, "url": "https://boards.greenhouse.io/acme/jobs/2",
        "title": "Accountant", "company": "Acme", "location": "Vancouver"})
    r = client.get("/api/v1/queue", params={"user_id": UID, "top_n": 10})
    jobs = r.json()["jobs"]
    titles = [j["title"] for j in jobs]
    assert "Senior AI Engineer" in titles
    assert "Accountant" not in titles  # 会计 0 分被过滤
    # 标记已处理后不再出现
    job_id = [j["id"] for j in jobs if j["title"] == "Senior AI Engineer"][0]
    client.post("/api/v1/applications", json={
        "user_id": UID, "job_id": job_id, "status": "submitted", "detail": "ok"})
    r2 = client.get("/api/v1/queue", params={"user_id": UID, "top_n": 10})
    assert all(j["id"] != job_id for j in r2.json()["jobs"])


def test_applications_and_stats():
    r = client.get("/api/v1/applications", params={"user_id": UID})
    assert any(a["status"] == "submitted" for a in r.json()["applications"])
    r = client.get("/api/v1/stats", params={"user_id": UID})
    assert r.json()["stats"].get("submitted", 0) >= 1


def test_matcher_unit():
    import matcher
    s, reasons = matcher.score_job(PROFILE, {"title": "Machine Learning Engineer", "location": "Remote"})
    assert s >= 80, (s, reasons)
    s2, _ = matcher.score_job(PROFILE, {"title": "Accountant", "location": "Vancouver"})
    assert s2 < 40, s2
