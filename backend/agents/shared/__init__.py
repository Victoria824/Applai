"""backend/agents/shared — 所有 agent 共用的小工具 (shared helpers).

- push.py: send_web_push — Web Push 推送（手机浏览器通知），best effort。
  推送归属：回复/面试推送 = Gloria；CAPTCHA/待人工推送 = Caroline。
  共享的是发送通道，发送决策归各 agent。
"""
