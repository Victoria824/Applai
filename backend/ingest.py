"""ingest.py — 从 ATS 公开 API 抓取职位（免登录、无反爬压力）

支持：
- Greenhouse: boards-api.greenhouse.io/v1/boards/{board}/jobs
- Lever: api.lever.co/v0/postings/{site}
board/site 从职位页 URL 里解析：boards.greenhouse.io/{board}/... / jobs.lever.co/{site}/...
"""
import re
import httpx

HEADERS = {"User-Agent": "Applai/0.2 (+https://github.com/Victoria824/Applai)"}


def parse_source(url: str) -> dict | None:
    m = re.search(r"boards\.greenhouse\.io/([a-z0-9_-]+)", url, re.I)
    if m:
        return {"type": "greenhouse", "key": m.group(1).lower()}
    m = re.search(r"jobs\.lever\.co/([a-z0-9_-]+)", url, re.I)
    if m:
        return {"type": "lever", "key": m.group(1).lower()}
    return None


def fetch_greenhouse(board: str, limit=100) -> list:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=false"
    r = httpx.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", [])[:limit]:
        loc = (j.get("location") or {}).get("name", "")
        jobs.append({
            "url": j.get("absolute_url", ""),
            "title": j.get("title", ""),
            "company": board,
            "location": loc,
            "source": f"greenhouse:{board}",
            "raw": {"id": j.get("id")},
        })
    return jobs


def fetch_lever(site: str, limit=100) -> list:
    url = f"https://api.lever.co/v0/postings/{site}?mode=json"
    r = httpx.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in (data if isinstance(data, list) else data.get("data", []))[:limit]:
        cats = j.get("categories") or {}
        jobs.append({
            "url": j.get("hostedUrl", ""),
            "title": j.get("text", ""),
            "company": site,
            "location": cats.get("location", ""),
            "source": f"lever:{site}",
            "raw": {"id": j.get("id")},
        })
    return jobs


def fetch_source(source: dict, limit=100) -> list:
    if source["type"] == "greenhouse":
        return fetch_greenhouse(source["key"], limit)
    if source["type"] == "lever":
        return fetch_lever(source["key"], limit)
    raise ValueError(f"unsupported source type: {source['type']}")
