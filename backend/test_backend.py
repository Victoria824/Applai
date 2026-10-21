"""后端测试：pytest test_backend.py"""
import os
import tempfile

# 测试用独立数据库
tmp = tempfile.mkdtemp()
os.environ["APPLAI_DB"] = os.path.join(tmp, "test.db")
os.environ["APPLAI_COOKIE_SECURE"] = "0"  # httpx 不通过 http 发送 Secure cookie

import db
db.DB_PATH = os.path.join(tmp, "test.db")

from fastapi.testclient import TestClient
import app

import pytest as _pytest

@_pytest.fixture(autouse=True)
def _clear_rate_limit():
    import app as _appmod
    _appmod._attempts.clear()
    yield


def _authed_client(username):
    """注册一个新账号并返回已登录的独立 client（cookie 隔离）。"""
    c = TestClient(app.app)
    r = c.post("/api/v1/auth/register", json={"username": username, "password": "password123"})
    assert r.status_code == 200, r.text
    return c


client = _authed_client("legacy_tester")
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


# ---------- LLM 混合打分 ----------
def test_hybrid_blend(monkeypatch):
    import llm as llm_mod
    import matcher
    monkeypatch.setenv("APPLAI_LLM_API_KEY", "fake-key")
    monkeypatch.setattr(llm_mod, "llm_score", lambda p, j, timeout=60: (90.0, ["LLM 理由"]))
    fs, reasons, via = matcher.hybrid_score(PROFILE, {"title": "AI Engineer"}, 85.0, ["规则理由"])
    assert via == "hybrid"
    assert fs == round(85.0 * 0.35 + 90.0 * 0.65, 1) == 88.2, fs
    assert reasons == ["LLM 理由"]


def test_hybrid_prefilter_skips_llm(monkeypatch):
    import llm as llm_mod
    import matcher
    monkeypatch.setenv("APPLAI_LLM_API_KEY", "fake-key")
    called = []
    monkeypatch.setattr(llm_mod, "llm_score",
                        lambda p, j, timeout=60: (called.append(1), (99.0, []))[1])
    fs, reasons, via = matcher.hybrid_score(PROFILE, {"title": "Accountant"}, 10.0, ["规则理由"])
    assert via == "rules" and fs == 10.0 and called == []


def test_hybrid_llm_failure_fallback(monkeypatch):
    import llm as llm_mod
    import matcher
    monkeypatch.setenv("APPLAI_LLM_API_KEY", "fake-key")
    monkeypatch.setattr(llm_mod, "llm_score", lambda p, j, timeout=60: (None, []))
    fs, reasons, via = matcher.hybrid_score(PROFILE, {"title": "AI Engineer"}, 85.0, ["规则理由"])
    assert via == "rules" and fs == 85.0 and reasons == ["规则理由"]


def test_hybrid_no_key_fallback(monkeypatch):
    import matcher
    monkeypatch.delenv("APPLAI_LLM_API_KEY", raising=False)
    fs, reasons, via = matcher.hybrid_score(PROFILE, {"title": "AI Engineer"}, 85.0, ["规则理由"])
    assert via == "rules" and fs == 85.0


def test_llm_prompt_contains_profile_and_job():
    import llm as llm_mod
    prompt = llm_mod.PROMPT.format(
        profile=llm_mod._profile_text(PROFILE),
        job=llm_mod._job_text({"title": "AI Engineer", "company": "Acme",
                               "location": "Toronto", "description": "Build LLM systems."}))
    assert "AI Engineer" in prompt and "Toronto" in prompt and "Build LLM systems." in prompt
    assert "score" in prompt and "reasons" in prompt


def test_llm_extract_json_with_fences():
    import llm as llm_mod
    d = llm_mod._extract_json('```json\n{"score": 72, "reasons": ["a"]}\n```')
    assert d == {"score": 72, "reasons": ["a"]}
    assert llm_mod._extract_json("not json at all") is None


def test_dashboard_redirects_to_login():
    c = TestClient(app.app)
    r = c.get("/dashboard", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/login" in r.headers["location"]
    r = c.get("/login")
    assert r.status_code == 200 and "Applai" in r.text


def test_register_login_flow():
    c = TestClient(app.app)
    r = c.post("/api/v1/auth/register", json={"username": "alice", "password": "password123"})
    assert r.status_code == 200
    r = c.get("/api/v1/auth/me")
    assert r.json()["username"] == "alice"
    r = c.get("/dashboard")
    assert r.status_code == 200 and "Applai" in r.text
    # 重复注册
    r = c.post("/api/v1/auth/register", json={"username": "alice", "password": "password123"})
    assert r.status_code == 400
    # 用户名不合法 / 密码太短
    r = c.post("/api/v1/auth/register", json={"username": "ab", "password": "password123"})
    assert r.status_code == 400
    r = c.post("/api/v1/auth/register", json={"username": "alice2", "password": "short"})
    assert r.status_code == 400
    # 登出后失效
    c.post("/api/v1/auth/logout")
    assert c.get("/api/v1/auth/me").status_code == 401


def test_login_wrong_password():
    c = _authed_client("bob")
    c.post("/api/v1/auth/logout")
    r = c.post("/api/v1/auth/login", json={"username": "bob", "password": "wrongpass"})
    assert r.status_code == 401
    r = c.post("/api/v1/auth/login", json={"username": "bob", "password": "password123"})
    assert r.status_code == 200
    assert c.get("/api/v1/auth/me").status_code == 200


def test_user_isolation():
    ca = _authed_client("iso_a")
    cb = _authed_client("iso_b")
    ca.put("/api/v1/profile", json={"user_id": "ignored", "profile": {"email": "a@x.com"}})
    assert ca.get("/api/v1/profile").json()["profile"]["email"] == "a@x.com"
    assert cb.get("/api/v1/profile").json()["profile"] in (None, {})
    r = ca.post("/api/v1/jobs",
                json={"user_id": "ignored", "url": "https://example.com/iso1", "title": "T", "company": "C"})
    real_job_id = r.json()["id"]
    assert len(ca.get("/api/v1/applications").json()["applications"]) == 0
    ca.post("/api/v1/applications",
            json={"user_id": "ignored", "job_id": real_job_id, "status": "submitted"})
    apps_b = cb.get("/api/v1/applications").json()["applications"]
    assert apps_b == []
    assert len(ca.get("/api/v1/applications").json()["applications"]) == 1


def test_api_token_auth_and_revoke():
    c = _authed_client("tokuser")
    r = c.post("/api/v1/auth/tokens", json={"name": "ext"}).json()
    raw = r["token"]
    assert raw.startswith("aap_")
    tid = r["id"]
    # 无 cookie、仅 Bearer 可用
    c2 = TestClient(app.app)
    r = c2.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200 and r.json()["username"] == "tokuser"
    assert c2.get("/api/v1/auth/me", headers={"Authorization": "Bearer aap_bogus"}).status_code == 401
    # 撤销后失效
    assert c.delete(f"/api/v1/auth/tokens/{tid}").status_code == 200
    assert c2.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {raw}"}).status_code == 401


def test_claim_old_data():
    import db as dbmod
    dbmod.upsert_profile("old_u_123", {"email": "old@x.com"})
    c = _authed_client("claimer")
    assert c.get("/api/v1/profile").json()["profile"] in (None, {})
    r = c.post("/api/v1/auth/claim", json={"old_user_id": "old_u_123"})
    assert r.status_code == 200
    assert c.get("/api/v1/profile").json()["profile"]["email"] == "old@x.com"
    # 不存在的旧 ID
    r = c.post("/api/v1/auth/claim", json={"old_user_id": "no_such_user"})
    assert r.status_code == 400


def test_resume_parse(monkeypatch):
    import llm as llm_mod
    c = _authed_client("resumeqa")
    pdf = open("/home/hatch/workspace/job-apply-tool/test_fixtures/fake_ai_engineer_resume.pdf", "rb").read()
    # mock LLM 抽取
    monkeypatch.setattr(llm_mod, "llm_parse_resume",
                        lambda text, timeout=60: {"firstName": "Alex", "lastName": "Chen",
                                                 "email": "alex.chen.qa@example.com",
                                                 "targetTitles": ["AI Engineer"],
                                                 "yearsExperience": 5})
    r = c.post("/api/v1/resume/parse", files={"file": ("resume.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["fields"]["email"] == "alex.chen.qa@example.com"
    assert d["fields"]["yearsExperience"] == 5
    assert d["chars"] > 100
    # 非 PDF 拒绝
    r = c.post("/api/v1/resume/parse", files={"file": ("a.txt", b"hello", "text/plain")})
    assert r.status_code == 400
    # 未登录拒绝
    anon = TestClient(app.app)
    r = anon.post("/api/v1/resume/parse", files={"file": ("resume.pdf", pdf, "application/pdf")})
    assert r.status_code == 401
    # LLM 失败 → 502
    monkeypatch.setattr(llm_mod, "llm_parse_resume", lambda text, timeout=60: None)
    r = c.post("/api/v1/resume/parse", files={"file": ("resume.pdf", pdf, "application/pdf")})
    assert r.status_code == 502


def test_resume_prompt_renders():
    import llm as llm_mod
    out = llm_mod.RESUME_PROMPT.replace("{resume}", "John Smith\nAI Engineer")
    assert "John Smith" in out and "{resume}" not in out
    assert '"firstName"' in out  # JSON 示例完整保留


def test_llm_parse_resume_success(monkeypatch):
    import llm as llm_mod
    import httpx

    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": '```json\n{"firstName":"Alex","email":"a@b.com"}\n```'}}]}

    monkeypatch.setattr(llm_mod, "get_config",
                        lambda: {"api_key": "k", "base_url": "https://x", "model": "m"})
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResp())
    d = llm_mod.llm_parse_resume("some resume text here " * 10)
    assert d and d["firstName"] == "Alex" and d["email"] == "a@b.com"


def test_job_sources():
    c = _authed_client("srcqa")
    # 非法类型
    r = c.post("/api/v1/sources", json={"type": "weird", "key": "x"})
    assert r.status_code == 400
    # 添加
    r = c.post("/api/v1/sources", json={"type": "greenhouse", "key": "openai"})
    assert r.status_code == 200
    sid = r.json()["id"]
    # 重复
    r = c.post("/api/v1/sources", json={"type": "greenhouse", "key": "openai"})
    assert r.status_code == 400
    # 列表
    ss = c.get("/api/v1/sources").json()["sources"]
    assert len(ss) == 1 and ss[0]["key"] == "openai"
    # 隔离：别人看不到
    c2 = _authed_client("srcqa2")
    assert c2.get("/api/v1/sources").json()["sources"] == []
    # 删除
    assert c.delete(f"/api/v1/sources/{sid}").status_code == 200
    assert c.get("/api/v1/sources").json()["sources"] == []
    # 删别人的 → 404
    r = c.post("/api/v1/sources", json={"type": "lever", "key": "acme"})
    assert c2.delete(f"/api/v1/sources/{r.json()['id']}").status_code == 404


def test_discover_auto_async():
    """discover/auto 立即返回 task_id，后台执行，轮询可查到 done。"""
    import time as _time
    c = _authed_client("discauto2")
    c.put("/api/v1/profile", json={"user_id": "x", "profile": {"email": "d@d.com"}})
    r = c.post("/api/v1/jobs/discover/auto")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "running" and d["task_id"]
    # 轮询等完成（种子公司抓取较快）
    for _ in range(60):
        s = c.get(f"/api/v1/jobs/discover/status/{d['task_id']}").json()
        if s["status"] in ("done", "error"):
            break
        _time.sleep(2)
    assert s["status"] == "done", s.get("error")
    assert s["seen"] >= 0 and "new" in s


def test_settings():
    c = _authed_client("setqa")
    s = c.get("/api/v1/settings").json()
    assert s["schedule_time"] == "08:00" and s["daily_count"] == 10
    r = c.put("/api/v1/settings", json={"schedule_enabled": True, "schedule_time": "09:30", "daily_count": 5})
    assert r.status_code == 200
    s = c.get("/api/v1/settings").json()
    assert s["schedule_enabled"] == 1 and s["schedule_time"] == "09:30" and s["daily_count"] == 5
    # 非法时间
    assert c.put("/api/v1/settings", json={"schedule_time": "nope"}).status_code == 400
    # 隔离
    c2 = _authed_client("setqa2")
    assert c2.get("/api/v1/settings").json()["schedule_time"] == "08:00"


def test_keywords_fallback_no_llm():
    c = _authed_client("kwqa")
    c.put("/api/v1/profile", json={"user_id": "x", "profile": {"targetTitles": ["AI Engineer", "ML Engineer"]}})
    # 无 LLM key 时走画像兜底
    r = c.get("/api/v1/settings/keywords")
    assert r.status_code == 200
    assert "AI Engineer" in r.json()["keywords"]


def test_chat_validation():
    c = _authed_client("chatqa")
    assert c.post("/api/v1/chat", json={"message": ""}).status_code == 400


def test_push_subscribe():
    c = _authed_client("pushqa")
    r = c.post("/api/v1/push/subscribe", json={
        "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
        "p256dh": "k1", "auth": "a1"})
    assert r.status_code == 200
    # 非 https 拒绝
    r = c.post("/api/v1/push/subscribe", json={
        "endpoint": "http://x", "p256dh": "k", "auth": "a"})
    assert r.status_code == 400


def test_ashby_source_type():
    c = _authed_client("ashbyqa")
    r = c.post("/api/v1/sources", json={"type": "ashby", "key": "wealthsimple"})
    assert r.status_code == 200
    ss = c.get("/api/v1/sources").json()["sources"]
    assert ss[0]["type"] == "ashby"


def test_ca_seeds_present():
    import ingest
    assert len(ingest.CA_SEED_BOARDS) >= 10
    types = {t for t, _ in ingest.CA_SEED_BOARDS}
    assert {"ashby", "greenhouse", "lever"} <= types


def test_queue_is_readonly():
    """看队列不能消费职位：连续看两次结果一致，且职位不被标记为 queued。"""
    c = _authed_client("qroqa")
    c.put("/api/v1/profile", json={"user_id": "x", "profile": {
        "targetTitles": ["Cleaner"], "preferredLocations": ["Toronto"]}})
    jid = c.post("/api/v1/jobs", json={
        "user_id": "x", "url": "https://example.com/j1", "title": "Cleaner",
        "company": "Acme", "location": "Toronto"}).json()["id"]
    # 手动打高分
    import db as dbm
    dbm.update_job_score(jid, 80, ["test"], "rules")
    q1 = c.get("/api/v1/queue?top_n=10").json()
    q2 = c.get("/api/v1/queue?top_n=10").json()
    assert q1["count"] == 1 and q2["count"] == 1
    assert q1["jobs"][0]["id"] == q2["jobs"][0]["id"]


def test_profile_update_resets_keywords_and_scores():
    c = _authed_client("rsetqa")
    c.put("/api/v1/profile", json={"user_id": "x", "profile": {"targetTitles": ["AI Engineer"]}})
    c.post("/api/v1/settings/keywords/refresh")
    k1 = c.get("/api/v1/settings/keywords").json()["keywords"]
    assert k1  # 有缓存
    # 更新画像 → 缓存失效
    c.put("/api/v1/profile", json={"user_id": "x", "profile": {"targetTitles": ["Cleaner"]}})
    import db as dbm, time
    st = dbm.get_settings(c.post("/api/v1/auth/login", json={
        "username": "rsetqa", "password": "testpass123"}).json().get("user_id", ""))
    # keywords_updated 被清零（通过内部检查）
    assert st["keywords_updated"] == 0


def test_chat_conversations():
    c = _authed_client("chatconvqa")
    # 新建
    d = c.post("/api/v1/chat/conversations").json()
    cid = d["id"]
    # 发消息（LLM 未配置会 502——mock 掉 llm_chat）
    import app as appm
    orig = appm.llm_mod.llm_chat if hasattr(appm, "llm_mod") else None
    import llm as llm_mod
    old_fn = llm_mod.llm_chat
    llm_mod.llm_chat = lambda sys, hist: "好的收到"
    try:
        r = c.post("/api/v1/chat", json={"message": "今天投了几个", "conversation_id": cid}).json()
        assert r["reply"] == "好的收到" and r["conversation_id"] == cid
    finally:
        llm_mod.llm_chat = old_fn
    # 历史落盘
    msgs = c.get(f"/api/v1/chat/conversations/{cid}/messages").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    # 列表里有标题（首条消息）
    convs = c.get("/api/v1/chat/conversations").json()["conversations"]
    assert any(x["id"] == cid and "今天投了几个" in x["title"] for x in convs)
    # 删除
    assert c.delete(f"/api/v1/chat/conversations/{cid}").json()["ok"]
    assert c.get(f"/api/v1/chat/conversations/{cid}/messages").status_code == 404


def test_application_snapshot():
    import hashlib
    c = _authed_client("snapqa")
    c.put("/api/v1/profile", json={"user_id": "x", "profile": {"email": "s@s.com"}})
    jid = c.post("/api/v1/jobs", json={
        "user_id": "x", "url": "https://example.com/snap1", "title": "Engineer",
        "company": "Acme", "location": "Toronto"}).json()["id"]
    c.post("/api/v1/applications", json={"user_id": "x", "job_id": jid, "status": "submitted"})
    resume_data = "data:application/pdf;base64,QUJD"
    rhash = hashlib.sha256(resume_data.encode()).hexdigest()
    r = c.post("/api/v1/applications/snapshot", json={
        "user_id": "x", "job_id": jid, "job_url": "https://example.com/snap1",
        "site": "boards.greenhouse.io",
        "filled_fields": [{"intent": "salaryMin", "label": "薪资下限", "value": "50"},
                          {"intent": "firstName", "label": "名", "value": "不应存"}],
        "salary_info": "薪资下限: 50",
        "cover_letter": "Dear Hiring Manager...",
        "resume_name": "resume.pdf", "resume_hash": rhash, "resume_data_url": resume_data})
    assert r.json()["ok"]
    # 读取快照（不含简历正文）
    s = c.get(f"/api/v1/applications/{jid}/snapshot").json()["snapshot"]
    assert s["site"] == "boards.greenhouse.io"
    assert s["has_resume"] and s["resume_hash"] == rhash
    assert "data_url" not in s
    # 简历下载
    b = c.get(f"/api/v1/resume-blob/{rhash}").json()
    assert b["data_url"] == resume_data and b["filename"] == "resume.pdf"
    # 历史列表带标记
    apps = c.get("/api/v1/applications").json()["applications"]
    assert any(a["job_id"] == jid and a["has_snapshot"] for a in apps)
    # 404
    assert c.get("/api/v1/applications/99999/snapshot").status_code == 404


def test_invites():
    c = _authed_client("invqa")
    # 创建邀请
    d = c.post("/api/v1/invites", json={"note": "朋友A"}).json()
    assert len(d["code"]) == 12 and d["link"].endswith(d["code"])
    # 朋友用邀请码注册
    f = TestClient(app.app)
    r = f.post("/api/v1/auth/register", json={
        "username": "invfriend1", "password": "password123", "invite_code": d["code"]})
    assert r.status_code == 200
    # 邀请人看到进度
    invs = c.get("/api/v1/invites").json()["invites"]
    assert len(invs) == 1 and invs[0]["friend"] == "invfriend1"
    assert invs[0]["has_profile"] is False
    # 朋友填了画像 → 进度更新
    f.put("/api/v1/profile", json={"user_id": "x", "profile": {"targetTitles": ["Dev"]}})
    invs = c.get("/api/v1/invites").json()["invites"]
    assert invs[0]["has_profile"] is True
    # 邀请码只能用一次
    f2 = TestClient(app.app)
    r2 = f2.post("/api/v1/auth/register", json={
        "username": "invfriend2", "password": "password123", "invite_code": d["code"]})
    assert r2.status_code == 200  # 注册成功但不关联
    invs = c.get("/api/v1/invites").json()["invites"]
    assert invs[0]["friend"] == "invfriend1"  # 还是第一个人


def test_export_and_delete():
    c = _authed_client("expdelqa")
    c.put("/api/v1/profile", json={"user_id": "x", "profile": {"email": "e@e.com"}})
    d = c.get("/api/v1/account/export").json()
    assert d["profile"]["email"] == "e@e.com"
    assert "jobs" in d and "applications" in d
    # 删除
    assert c.delete("/api/v1/account").json()["ok"]
    # 数据没了（新 client 重新登录应失败）
    c2 = TestClient(app.app)
    r = c2.post("/api/v1/auth/login", json={"username": "expdelqa", "password": "password123"})
    assert r.status_code == 401


def test_gmail_oauth_flow():
    c = _authed_client("gmailqa")
    # 未配置时
    s = c.get("/api/v1/gmail/status").json()
    assert "connected" in s and "configured" in s
    # state 存取消耗
    import db as dbmod
    dbmod.save_gmail_state("teststate123", "u_test123")
    assert dbmod.consume_gmail_state("teststate123") == "u_test123"
    assert dbmod.consume_gmail_state("teststate123") is None  # 一次性
    # token 存取
    dbmod.save_gmail_token("u_test123", "test@gmail.com", "refresh_xyz")
    t = dbmod.get_gmail_token("u_test123")
    assert t["email"] == "test@gmail.com"
    assert len(dbmod.gmail_users()) >= 1
    dbmod.delete_gmail_token("u_test123")
    assert dbmod.get_gmail_token("u_test123") is None


def test_email_events():
    import db as dbmod
    assert dbmod.add_email_event("u_ev1", "msg1", "interview", "Acme", "Engineer", "Interview!", "面试邀请")
    assert not dbmod.add_email_event("u_ev1", "msg1", "interview", "Acme", "Engineer", "Interview!", "x")  # 去重
    evs = dbmod.list_email_events("u_ev1")
    assert len(evs) == 1 and evs[0]["kind"] == "interview"


def test_reports():
    c = _authed_client("repqa")
    d = c.get("/api/v1/reports/digest?period=weekly").json()
    assert d["period"] == "weekly" and "response_rate" in d
    d2 = c.get("/api/v1/reports/digest?period=daily").json()
    assert d2["days"] == 1


def test_chat_actions():
    c = _authed_client("actqa")
    # 暂停/恢复投递（不需要 LLM，用直接调用测试 action 执行器）
    import app as appmod
    r = appmod._run_chat_action("u_actqa", {"action": "pause_schedule"}, [])
    assert "已暂停" in r
    r = appmod._run_chat_action("u_actqa", {"action": "resume_schedule"}, [])
    assert "已恢复" in r
    r = appmod._run_chat_action("u_actqa", {"action": "add_to_queue", "jobs": []}, [])
    assert "没识别" in r or "具体" in r


def test_retry_failed_application():
    """失败记录可删除重入队；非失败状态不受影响。"""
    c = _authed_client("retryuser")
    c.put("/api/v1/profile", json={"user_id": "ignored", "profile": {
        "firstName": "Re", "email": "re@x.com", "location": "Toronto, ON",
        "targetTitles": ["AI Engineer"], "preferredLocations": ["Toronto"],
        "remotePreference": "any"}})
    d = c.post("/api/v1/jobs", json={
        "user_id": "ignored",
        "url": "https://boards.greenhouse.io/acme/jobs/retry1",
        "title": "AI Engineer", "company": "Acme", "location": "Toronto, ON"}).json()
    jid = d["id"]
    assert d["score"] >= 40, d
    assert any(j["id"] == jid for j in c.get("/api/v1/queue").json()["jobs"])
    rr = c.post("/api/v1/applications", json={"user_id": "ignored", "job_id": jid,
                                              "status": "failed",
                                              "detail": "Cannot set properties of undefined"})
    assert rr.status_code == 200, rr.text
    assert all(j["id"] != jid for j in c.get("/api/v1/queue").json()["jobs"])
    r = c.post("/api/v1/applications/retry", json={"job_id": jid})
    assert r.status_code == 200 and r.json()["removed"] == 1
    assert any(j["id"] == jid for j in c.get("/api/v1/queue").json()["jobs"])
    # submitted 的不能被 retry 删掉
    c.post("/api/v1/applications", json={"user_id": "ignored", "job_id": jid, "status": "submitted"})
    r2 = c.post("/api/v1/applications/retry", json={"job_id": jid})
    assert r2.json()["removed"] == 0


def test_run_progress():
    """投递进度上报/读取 + 用户隔离。"""
    ca = _authed_client("prog_a")
    cb = _authed_client("prog_b")
    assert ca.get("/api/v1/run-progress").json()["progress"] is None
    r = ca.post("/api/v1/run-progress", json={
        "state": "running", "total": 10, "processed": 3, "submitted": 2,
        "manual": 1, "failed": 0, "current_job_id": 42, "current_title": "AI Engineer"})
    assert r.status_code == 200
    p = ca.get("/api/v1/run-progress").json()["progress"]
    assert p["processed"] == 3 and p["current_job_id"] == 42 and p["started_at"] > 0
    # 同一轮后续上报不重置 started_at
    ca.post("/api/v1/run-progress", json={
        "state": "running", "total": 10, "processed": 4, "submitted": 3,
        "manual": 1, "failed": 0, "current_job_id": 43, "current_title": "ML Engineer"})
    p2 = ca.get("/api/v1/run-progress").json()["progress"]
    assert p2["processed"] == 4 and p2["started_at"] == p["started_at"]
    assert cb.get("/api/v1/run-progress").json()["progress"] is None


def test_resume_display_name():
    """投递简历文件名必须专业：First_Last_Resume.pdf；无姓名时退回 Resume.pdf。"""
    import app as appmod
    c = _authed_client("nameuser")
    me = c.get("/api/v1/auth/me").json()
    uid = me.get("app_user_id") or me.get("user_id") or me.get("id")
    c.put("/api/v1/profile", json={"user_id": "ignored", "profile": {
        "firstName": "Emma", "lastName": "Stone", "email": "e@x.com"}})
    assert appmod._resume_display_name(uid) == "Emma_Stone_Resume.pdf"
    c2 = _authed_client("noname")
    me2 = c2.get("/api/v1/auth/me").json()
    uid2 = me2.get("app_user_id") or me2.get("user_id") or me2.get("id")
    assert appmod._resume_display_name(uid2) == "Resume.pdf"


def test_resume_pdf_one_page():
    """定制简历 PDF：内容再多也必须恰好一页（缩放+删减兜底）。"""
    import io
    from pypdf import PdfReader
    import resume_pdf
    prof = {"firstName": "Emma", "lastName": "Stone", "email": "e@x.com"}
    exps = [{"company": f"C{i}", "title": "Senior Engineer", "dates": "2018 — 2020",
             "bullets": ["Delivered a large distributed system with measurable impact on revenue, reliability and team velocity across regions."] * 7}
            for i in range(5)]
    content = {"summary": "Engineer. " * 10, "experiences": exps,
               "skills": [f"S{k}" for k in range(20)], "education": "U of T — BSc"}
    pdf = resume_pdf.generate_pdf(content, prof)
    assert pdf[:4] == b"%PDF"
    assert len(PdfReader(io.BytesIO(pdf)).pages) == 1
    small = resume_pdf.generate_pdf({"summary": "Hi", "experiences": [], "skills": [], "education": ""}, prof)
    assert len(PdfReader(io.BytesIO(small)).pages) == 1


def test_for_job_prefers_existing_tailored_version():
    """手动生成过定制版，即使没开自动定制开关，for-job 也要用它。"""
    import db as dbmod
    c = _authed_client("forjobuser")
    me = c.get("/api/v1/auth/me").json()
    uid = me.get("app_user_id") or me.get("user_id") or me.get("id")
    c.put("/api/v1/profile", json={"user_id": "ignored", "profile": {
        "firstName": "Emma", "lastName": "Stone", "email": "e@x.com",
        "targetTitles": ["AI Engineer"]}})
    d = c.post("/api/v1/jobs", json={"user_id": "ignored",
        "url": "https://jobs.ashbyhq.com/acme/12345678-1234-1234-1234-123456789012",
        "title": "AI Engineer", "company": "Acme", "location": "Toronto, ON"}).json()
    jid = d["id"]
    ap = dbmod.get_active_profile(uid)
    dbmod.save_resume_blob("deadbeef" * 8, "x.pdf", "data:application/pdf;base64,AAAA")
    dbmod.save_resume_version(uid, ap["id"], jid, "tailored", {"summary": "s"}, "deadbeef" * 8)
    r = c.get("/api/v1/resume/for-job", params={"job_id": jid}).json()
    assert r["tailored"] is True and r["pdf_hash"] == "deadbeef" * 8
    assert r["filename"] == "Emma_Stone_Resume.pdf"


def test_claim_cannot_steal_registered_account():
    """审计修复：claim 迁移绝不能把注册账号的数据迁走。"""
    a = _authed_client("claimvictim")
    b = _authed_client("claimattacker")
    a_uid = a.get("/api/v1/auth/me").json()["app_user_id"]
    r = b.post("/api/v1/auth/claim", json={"old_user_id": a_uid})
    assert r.status_code == 400
    # 受害者数据完好
    assert a.get("/api/v1/auth/me").status_code == 200


def test_delete_account_wipes_gmail_push_progress():
    """审计修复：删号必须清 Gmail token、Push 订阅、投递进度等全部表。"""
    import db as dbmod
    c = _authed_client("delwipe")
    uid = c.get("/api/v1/auth/me").json()["app_user_id"]
    dbmod.save_gmail_token(uid, "delwipe@gmail.com", "fake-refresh-token")
    dbmod.add_push_subscription(uid, "https://fcm.googleapis.com/fcm/send/x", "k", "a")
    dbmod.upsert_run_progress(uid, {"state": "running", "total": 3, "processed": 1})
    assert c.delete("/api/v1/account").json()["ok"]
    assert dbmod.get_gmail_token(uid) is None
    assert dbmod.get_run_progress(uid) is None
    conn = dbmod.get_db()
    n = conn.execute("SELECT COUNT(*) c FROM push_subscriptions WHERE user_id=?",
                     (uid,)).fetchone()["c"]
    conn.close()
    assert n == 0


def test_manual_queued_job_enters_queue():
    """审计修复：chatbot 的 add_to_queue（status='queued'）必须真的进队列，
    哪怕分数不够 40；被投递后又自动离开队列。"""
    c = _authed_client("manualqueue")
    c.put("/api/v1/profile", json={"user_id": "ignored", "profile": {
        "firstName": "MQ", "email": "mq@x.com", "targetTitles": ["AI Engineer"]}})
    d = c.post("/api/v1/jobs", json={"user_id": "ignored",
        "url": "https://jobs.lever.co/acme/barista-1", "title": "Barista",
        "company": "Acme", "location": "Vancouver, BC"}).json()
    jid = d["id"]
    q0 = c.get("/api/v1/queue").json()["jobs"]
    assert all(j["id"] != jid for j in q0)  # 低分默认不在队列
    r = c.post("/api/v1/applications", json={"user_id": "ignored", "job_id": jid,
                                             "status": "queued", "detail": "chatbot 手动加入"})
    assert r.status_code == 200, r.text
    q1 = c.get("/api/v1/queue").json()["jobs"]
    assert any(j["id"] == jid for j in q1)  # 手动加入后进队列
    c.post("/api/v1/applications", json={"user_id": "ignored", "job_id": jid,
                                         "status": "submitted", "detail": "done"})
    q2 = c.get("/api/v1/queue").json()["jobs"]
    assert all(j["id"] != jid for j in q2)  # 投递后离开队列


def test_invite_code_multi_use_tracking():
    """审计修复：一个邀请码可供多人注册，且每位朋友的进度都可追踪。"""
    a = _authed_client("invhost2")
    code = a.post("/api/v1/invites", json={"note": "组局"}).json()["code"]
    assert len(code) == 12  # 新码 12 位
    from fastapi.testclient import TestClient
    import app as appmod
    for name in ("invpal1", "invpal2"):
        tc = TestClient(appmod.app)
        r = tc.post("/api/v1/auth/register", json={
            "username": name, "password": "password123", "invite_code": code})
        assert r.status_code == 200, r.text
    invs = a.get("/api/v1/invites").json()["invites"]
    mine = [i for i in invs if i["code"] == code][0]
    friends = {u["friend"] for u in mine.get("uses", [])}
    assert friends == {"invpal1", "invpal2"}


def test_resume_blob_requires_ownership():
    """审计修复：简历 blob 只能被引用过它的用户下载（凭 hash 猜/拿不能下）。"""
    import db as dbmod
    a = _authed_client("blobowner")
    b = _authed_client("blobstranger")
    a_uid = a.get("/api/v1/auth/me").json()["app_user_id"]
    h = "cafe" * 16
    dbmod.save_resume_blob(h, "A_Resume.pdf", "data:application/pdf;base64,AAAA")
    ap = dbmod.get_active_profile(a_uid)
    dbmod.save_resume_version(a_uid, ap["id"], 999001, "tailored",
                              {"summary": "s"}, h)
    assert b.get(f"/api/v1/resume-blob/{h}").status_code == 404
    r = a.get(f"/api/v1/resume-blob/{h}")
    assert r.status_code == 200 and r.json()["filename"] == "A_Resume.pdf"


def test_session_token_stored_hashed():
    """审计修复：auth_sessions 里存的是 session token 的 SHA-256，不是明文。"""
    import db as dbmod
    c = _authed_client("sesshash")
    raw = c.cookies.get("aap_session")
    assert raw
    conn = dbmod.get_db()
    rows = conn.execute("SELECT token FROM auth_sessions").fetchall()
    conn.close()
    stored = {r["token"] for r in rows}
    assert raw not in stored
    import hashlib as _hl
    assert _hl.sha256(raw.encode()).hexdigest() in stored


def test_push_subscribe_endpoint_allowlist():
    """审计修复：Push 订阅 endpoint 必须是已知推送服务域名，防 SSRF。"""
    c = _authed_client("pushguard")
    bad = c.post("/api/v1/push/subscribe", json={
        "endpoint": "https://evil.example.com/hook", "p256dh": "k", "auth": "a"})
    assert bad.status_code == 400
    good = c.post("/api/v1/push/subscribe", json={
        "endpoint": "https://fcm.googleapis.com/fcm/send/abc123",
        "p256dh": "k", "auth": "a"})
    assert good.status_code == 200


def test_discover_status_ownership():
    """审计修复：抓取任务状态只能由任务主人查看。"""
    import db as dbmod
    a = _authed_client("taskowner")
    b = _authed_client("taskstranger")
    a_uid = a.get("/api/v1/auth/me").json()["app_user_id"]
    dbmod.create_discover_task("task-audit-1", a_uid)
    assert b.get("/api/v1/jobs/discover/status/task-audit-1").status_code == 404
    assert a.get("/api/v1/jobs/discover/status/task-audit-1").status_code == 200


def test_export_includes_resume_blobs():
    """审计修复：数据导出要真正带上简历 blob（旧实现恒为空）。"""
    import db as dbmod
    c = _authed_client("exporter1")
    uid = c.get("/api/v1/auth/me").json()["app_user_id"]
    d = c.post("/api/v1/jobs", json={"user_id": "ignored",
        "url": "https://jobs.ashbyhq.com/acme/exp-1", "title": "AI Engineer",
        "company": "Acme", "location": "Toronto, ON"}).json()
    h = "beef" * 16
    dbmod.save_resume_blob(h, "Exp_Resume.pdf", "data:application/pdf;base64,BBBB")
    dbmod.save_snapshot(uid, d["id"], {"job_url": "https://x", "site": "ashby",
                                       "resume_hash": h, "resume_name": "Exp_Resume.pdf"})
    out = c.get("/api/v1/account/export").json()
    blobs = out.get("resume_blobs") or []
    assert any(b["hash"] == h and b["data_url"].endswith("BBBB") for b in blobs)
