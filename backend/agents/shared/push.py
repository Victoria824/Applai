"""shared/push.py — Web Push 推送（手机浏览器通知）。

Moved as-is from backend/app.py (Step 1, Foundation): only send_web_push
moved, nothing else. Callers: Gloria (reply/interview pushes) and Caroline
(CAPTCHA / needs_manual pushes). Best effort — failures are silent.
"""
import os

import db


def send_web_push(user_id: str, title: str, body: str):
    """验证码等事件发生时推送到用户手机浏览器。失败静默（best effort）。"""
    priv = os.environ.get("APPLAI_VAPID_PRIVATE_KEY", "").strip()
    if not priv:
        return
    try:
        from pywebpush import webpush, WebPushException
        import json as _json
        for sub in db.list_push_subscriptions(user_id):
            try:
                webpush(
                    subscription_info={"endpoint": sub["endpoint"],
                                       "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                    data=_json.dumps({"title": title, "body": body}),
                    vapid_private_key=priv,
                    vapid_claims={"sub": "mailto:applai@localhost"},
                )
            except Exception:
                continue
    except Exception:
        pass
