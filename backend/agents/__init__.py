"""backend/agents — Applai 多智能体系统 (multi-agent system).

Layout:
    agents/
        __init__.py          — this docstring (layout only, no logic)
        jennifer.py          — 简历与市场分析 / Resume & Market Analysis
        amy.py               — 职位搜罗 / Job Hunter
        caroline.py          — 填表投递 / Application Specialist
        gloria.py            — 复盘 / Review Coach
        rachel.py            — 总管 · 排班 / Supervisor & Scheduler
        shared/
            __init__.py
            push.py          — send_web_push (Web Push, shared by all agents)

Rules (see docs/multi-agent-design.md):
- Agents communicate via DB rows (the blackboard), never direct cross-agent
  calls except commands routed via Rachel.
- Agents import shared services (db, llm, matcher, ingest, gmail, email_sync,
  resume_pdf); they never rewrite them.
- Each lane module exposes status(user_id) and recent_events(user_id, limit=5)
  when implemented; GET /api/v1/agents lazy-imports them and tolerates
  missing modules/functions (reports that agent as idle).
- app.py stays thin: routes validate input, resolve the user, delegate.
"""
