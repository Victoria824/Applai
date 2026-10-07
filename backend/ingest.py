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
    if source["type"] == "ashby":
        return fetch_ashby(source["key"], limit)
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


# 加拿大/GTA 种子公司（已验证的公开 ATS，智能抓取时自动包含）
CA_SEED_BOARDS = [
    ("ashby", "wealthsimple"), ("ashby", "1password"), ("ashby", "neofinancial"),
    ("ashby", "docebo"), ("ashby", "hopper"), ("ashby", "loopio"),
    ("ashby", "koho"), ("ashby", "clearco"),
    ("greenhouse", "stackadapt"), ("greenhouse", "tulip"), ("greenhouse", "hootsuite"),
    ("greenhouse", "d2l"), ("greenhouse", "abcellera"),
    ("lever", "wattpad"),
]


def fetch_ashby(board: str, limit=100) -> list:
    """Ashby 公开 API（免 key），加拿大 fintech 常用。"""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
    r = httpx.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", [])[:limit]:
        if not j.get("isListed", True):
            continue
        loc = j.get("location") or ""
        if j.get("isRemote"):
            loc = (loc + " / Remote").strip(" /")
        jobs.append({
            "url": j.get("jobUrl", ""),
            "title": j.get("title", ""),
            "company": board,
            "location": loc,
            "source": f"ashby:{board}",
            "raw": {"id": j.get("id")},
            "description": clean_html(j.get("descriptionPlain") or j.get("descriptionHtml") or ""),
        })
    return jobs


def _jobbank_detail(url: str, timeout: int = 12) -> str:
    """抓 Job Bank 详情页正文（best effort）。"""
    try:
        r = httpx.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        m = re.search(r'job-posting-details.*?<div[^>]*>(.*)', r.text, re.S)
        if not m:
            return ""
        return clean_html(m.group(1))[:4000]
    except Exception:
        return ""


def fetch_jobbank(keywords: list, limit=60) -> list:
    """加拿大 Job Bank RSS（联邦政府官方，免 key）。按关键词搜安省职位。"""
    import xml.etree.ElementTree as ET
    jobs, seen = [], set()
    queries = [k for k in (keywords or []) if k][:4] or [""]
    per_q = max(5, limit // max(1, len(queries)))
    for q in queries:
        params = {"sort": "D", "rows": per_q, "fprov": "ON"}
        if q:
            params["searchstring"] = q
        try:
            r = httpx.get("https://www.jobbank.gc.ca/jobsearch/feed/jobSearchRSSfeed",
                          params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception:
            continue
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for e in root.findall("a:entry", ns):
            link = e.find("a:link", ns)
            url = link.get("href", "") if link is not None else ""
            if not url or url in seen:
                continue
            seen.add(url)
            title = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
            summary = (e.findtext("a:summary", default="", namespaces=ns) or "")
            loc_m = re.search(r"Location:</strong>\s*([^<]+)", summary)
            emp_m = re.search(r"Employer:</strong>\s*([^<]+)", summary)
            sal_m = re.search(r"Salary:</strong>\s*([^<]+)", summary)
            location = (loc_m.group(1).strip() if loc_m else "")
            employer = (emp_m.group(1).strip() if emp_m else "")
            salary = (sal_m.group(1).strip() if sal_m else "")
            desc = _jobbank_detail(url)
            if salary:
                desc = f"Salary: {salary}\n{desc}"
            jobs.append({
                "url": url,
                "title": title,
                "company": employer,
                "location": location,
                "source": "jobbank",
                "raw": {},
                "description": desc,
            })
            if len(jobs) >= limit:
                return jobs
    return jobs
