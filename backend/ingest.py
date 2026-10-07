"""ingest.py — 从 ATS 公开 API 抓取职位（免登录、无反爬压力）

支持：
- Greenhouse: boards-api.greenhouse.io/v1/boards/{board}/jobs
- Lever: api.lever.co/v0/postings/{site}
board/site 从职位页 URL 里解析：boards.greenhouse.io/{board}/... / jobs.lever.co/{site}/...
"""
import re
import httpx

HEADERS = {"User-Agent": "Applai/0.2 (+https://github.com/Victoria824/Applai)"}
DESC_LIMIT = 4000


def clean_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:DESC_LIMIT]


def parse_source(url: str) -> dict | None:
    m = re.search(r"boards\.greenhouse\.io/([a-z0-9_-]+)", url, re.I)
    if m:
        return {"type": "greenhouse", "key": m.group(1).lower()}
    m = re.search(r"jobs\.lever\.co/([a-z0-9_-]+)", url, re.I)
    if m:
        return {"type": "lever", "key": m.group(1).lower()}
    return None


def fetch_greenhouse(board: str, limit=100) -> list:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
    r = httpx.get(url, headers=HEADERS, timeout=30)
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
            "description": clean_html(j.get("content", "")),
        })
    return jobs


def fetch_lever(site: str, limit=100) -> list:
    url = f"https://api.lever.co/v0/postings/{site}?mode=json"
    r = httpx.get(url, headers=HEADERS, timeout=30)
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
            "description": clean_html(j.get("descriptionPlain") or j.get("description") or ""),
        })
    return jobs


def fetch_source(source: dict, limit=100) -> list:
    if source["type"] == "greenhouse":
        return fetch_greenhouse(source["key"], limit)
    if source["type"] == "lever":
        return fetch_lever(source["key"], limit)
    raise ValueError(f"unsupported source type: {source['type']}")


def _kw_match(text: str, keywords: list) -> bool:
    t = (text or "").lower()
    return any(k.lower() in t for k in keywords if k)


def fetch_arbeitnow(keywords: list, limit=100) -> list:
    """免费聚合源（免 key）：按关键词过滤职位。"""
    r = httpx.get("https://www.arbeitnow.com/api/job-board-api", headers=HEADERS, timeout=30)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("data", []):
        title = j.get("title", "")
        desc = clean_html(j.get("description", ""))
        if keywords and not _kw_match(title + " " + desc, keywords):
            continue
        jobs.append({
            "url": j.get("url") or f"https://www.arbeitnow.com/job/{j.get('slug', '')}",
            "title": title,
            "company": j.get("company_name", ""),
            "location": j.get("location", ""),
            "source": "arbeitnow",
            "raw": {"slug": j.get("slug")},
            "description": desc,
        })
        if len(jobs) >= limit:
            break
    return jobs


def fetch_remotive(keywords: list, limit=100) -> list:
    """免费远程职位源（免 key），支持 search 参数。"""
    jobs, seen = [], set()
    queries = keywords[:4] if keywords else [""]
    for q in queries:
        params = {"limit": 50}
        if q:
            params["search"] = q
        r = httpx.get("https://remotive.com/api/remote-jobs", params=params,
                      headers=HEADERS, timeout=30)
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            url = j.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            jobs.append({
                "url": url,
                "title": j.get("title", ""),
                "company": j.get("company_name", ""),
                "location": j.get("candidate_required_location", "") or "Remote",
                "source": "remotive",
                "raw": {"id": j.get("id")},
                "description": clean_html(j.get("description", "")),
            })
            if len(jobs) >= limit:
                return jobs
    return jobs
