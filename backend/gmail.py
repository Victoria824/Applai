"""gmail.py — Gmail 只读接入（OAuth + 邮件同步）。

只用 gmail.readonly 权限，不发邮件不删邮件。
"""
import base64
import os
import time
import urllib.parse

import httpx

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI",
                              "https://applai-backend.fly.dev/api/v1/gmail/callback")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def is_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def auth_url(state: str) -> str:
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",  # 拿 refresh_token
        "prompt": "consent",
        "state": state,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(code: str) -> dict:
    """code 换 tokens。返回 {access_token, refresh_token, expires_in}。"""
    r = httpx.post(TOKEN_URL, data={
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }, timeout=20)
    r.raise_for_status()
    return r.json()


def refresh_access_token(refresh_token: str) -> dict:
    r = httpx.post(TOKEN_URL, data={
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=20)
    r.raise_for_status()
    return r.json()


def api_get(access_token: str, path: str, params: dict = None) -> dict:
    r = httpx.get(GMAIL_API + path,
                  headers={"Authorization": f"Bearer {access_token}"},
                  params=params or {}, timeout=20)
    r.raise_for_status()
    return r.json()


def get_profile_email(access_token: str) -> str:
    return api_get(access_token, "/profile").get("emailAddress", "")


def _decode_body(payload: dict) -> str:
    """递归提取邮件正文（text/plain 优先）。"""
    if payload.get("mimeType", "").startswith("text/plain") and payload.get("body", {}).get("data"):
        try:
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "ignore")
        except Exception:
            return ""
    for part in payload.get("parts", []) or []:
        t = _decode_body(part)
        if t:
            return t
    return ""


def _headers(payload: dict) -> dict:
    out = {}
    for h in payload.get("headers", []) or []:
        n = (h.get("name") or "").lower()
        if n in ("from", "subject", "date"):
            out[n] = h.get("value", "")
    return out


def get_message(access_token: str, msg_id: str) -> dict:
    """取一封邮件的发件人/主题/正文（截断）。"""
    m = api_get(access_token, f"/messages/{msg_id}", {"format": "full"})
    payload = m.get("payload", {})
    h = _headers(payload)
    body = _decode_body(payload)
    return {
        "id": msg_id,
        "from": h.get("from", ""),
        "subject": h.get("subject", ""),
        "date": h.get("date", ""),
        "snippet": m.get("snippet", ""),
        "body": body[:3000],
    }


def list_recent(access_token: str, query: str, max_results: int = 20) -> list:
    """按 query 搜最近邮件，返回 [{id}]。"""
    d = api_get(access_token, "/messages", {"q": query, "maxResults": max_results})
    return d.get("messages", []) or []


# 求职相关邮件的搜索词（4 小时 cron 用）
JOB_QUERY = ("newer_than:1d (subject:interview OR subject:application OR "
             "subject:offer OR subject:rejected OR subject:unfortunately OR "
             "from:greenhouse.io OR from:lever.co OR from:ashbyhq.com OR from:workday.com)")
