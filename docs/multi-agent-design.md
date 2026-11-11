# Applai 多智能体系统设计 / Multi-Agent Design

> Branch: `multi-agent` · Status: Step 1 — Foundation (2026-11-05)
> 原则：业务逻辑不变 (business logic unchanged) — same tables, same API routes,
> same fetch / score / apply / review flow. Only the *organisation* of the code changes:
> one monolithic `backend/app.py` becomes thin routes delegating to agents.
> Extension (`extension/`, v0.2.14) is OFF-LIMITS; backend API routes and response
> shapes must remain identical.

## 1. 团队 Roster / Team

| id | Name | 角色 Role (zh) | Role (en) |
|----|------|----------------|-----------|
| `jennifer` | Jennifer | 简历与市场分析 | Resume & Market Analysis |
| `amy` | Amy | 职位搜罗 | Job Hunter |
| `caroline` | Caroline | 填表投递 | Application Specialist |
| `gloria` | Gloria | 复盘 | Review Coach |
| `rachel` | Rachel | 总管 · 排班 | Supervisor & Scheduler |

Order on the team page is always: Jennifer, Amy, Caroline, Gloria, Rachel.

## 2. 拓扑 Topology

Supervisor + sequential pipeline + shared-state blackboard (Postgres).

```
                ┌──────────────┐
                │   Rachel     │  总管：排班 / 全局状态 / 交接路由 / 重试
                │  (supervisor)│  thin — no business logic of her own
                └──────┬───────┘
                       │ routes commands, owns schedule
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   Jennifer ──► Amy ──► Caroline ──► Gloria
   简历/画像    职位搜罗    填表投递      复盘
   + 市场分析    (fetch)    (apply)     (Gmail/聊天)
        ▲                                  │
        └────── 关键词缓存 ◄────────────────┘
              (feedback loop via shared state only)
```

- **Agents communicate via DB rows, never direct cross-agent calls** — except
  commands routed via Rachel. The DB is the blackboard: `jobs`, `applications`,
  `profiles`, `user_settings`, `discover_tasks`, `run_progress`, `email_events`,
  and the new `agent_events` table (see §6).
- **No LangGraph, no new framework** — plain Python modules under `backend/agents/`.
- **Shared services are imported, not rewritten**: `db.py`, `llm.py`, `matcher.py`,
  `ingest.py`, `gmail.py`, `email_sync.py`, `resume_pdf.py`. Agents call these;
  lane work must not fork or rewrite them.

## 3. 所有权 Ownership

Grounded in the current code (`backend/app.py`, `backend/db.py`, and the shared
service modules as of 2026-11-05). "Owns" = the agent is the only writer/decision
maker for that function/table; other agents read through the DB.

### 3.1 Jennifer — 简历与市场分析 / Resume & Market Analysis

| Function / route (today in `app.py`) | Shared service used | Tables |
|---|---|---|
| `POST /api/v1/resume/parse` — 简历解析 (`llm_parse_resume`, regex fallback) | `llm.py: llm_parse_resume / regex_parse_resume` | `profiles` (kind=`person`) |
| `POST /api/v1/profile/suggest-job` — 目标岗位画像初稿 | `llm.py: llm_suggest_job_profile` | `profiles` (kind=`job`) |
| `_keywords_for()` — 搜索关键词生成，7 天缓存 | `llm.py: llm_job_keywords` | `user_settings.job_keywords`, `keywords_note`, `keywords_updated` |
| 画像 CRUD (`PUT/GET /api/v1/profile`, `/api/v1/profiles*`) | `db.py: upsert_profile / get_active_person / get_active_job / list_profiles` | `profiles`, `users` |
| 母版简历 (master resume) — `resume_versions` kind=`master` | `resume_pdf.py` (rendering only) | `resume_versions` (master rows) |
| 匹配打分 (`score_and_store`, background scorer thread) | `matcher.py: score_job / hybrid_score`, `llm.py: llm_score` | `jobs.score`, `jobs.score_reasons`, `jobs.score_via` |

Jennifer owns the **master resume** and the **profile**. She decides what the
candidate looks like on paper; Caroline may only derive per-job variants from it.

### 3.2 Amy — 职位搜罗 / Job Hunter

| Function / route (today in `app.py`) | Shared service used | Tables |
|---|---|---|
| `_run_discover()` — 按来源抓取入库 | `ingest.py: fetch_source` | `jobs`, `job_sources` |
| `_run_smart_discover()` + `_run_smart_discover_bg()` — 智能抓取：种子公司 + 免费聚合源 | `ingest.py: fetch_jobbank / fetch_arbeitnow / fetch_remotive`, `CA_SEED_BOARDS` | `jobs`, `discover_tasks` |
| `POST /api/v1/jobs/discover/auto`, `GET /api/v1/jobs/discover/status/{task_id}` | `db.py: create/update/get_discover_task` | `discover_tasks` |
| `POST /api/v1/jobs/discover`, `GET/POST/DELETE /api/v1/sources`, `GET /api/v1/jobs/lookup`, `POST /api/v1/jobs` | `ingest.py: parse_source / fetch_*` | `jobs`, `job_sources` |

Amy owns **fetch only**: she ingests jobs as-is and never rewrites them, never
scores them (scoring is Jennifer's), never applies to them (Caroline's).
Keywords come to her from Jennifer **via the existing cache** (`user_settings.job_keywords`):
Amy's `_keywords_for()` delegates (lazy import) to Jennifer's full
`_keywords_for()` — cache read plus LLM regeneration when the cache is
stale/empty — exactly as `app.py` did before the split. The cache is the
handoff; the delegation only preserves the read-through behaviour.

### 3.3 Caroline — 填表投递 / Application Specialist

| Function / route (today in `app.py`) | Shared service used | Tables |
|---|---|---|
| `_queue_jobs()` — 待投递队列（手动 queued 优先，其余按 stored 分数 ≥ 40，排除已处理） | stored `jobs.score`（hybrid: rules×0.35 + LLM×0.65）；**不**调用 `matcher.build_queue`（它只算规则分、无手动优先、无已处理排除，改用它会改变队列） | `applications`, `jobs` |
| `POST/GET /api/v1/applications`, `POST /api/v1/applications/retry` | `db.py: record_application / list_applications` | `applications` |
| `POST/GET /api/v1/run-progress` — 插件跑批进度 | `db.py: upsert/get_run_progress` | `run_progress` |
| 按岗定制简历 (`_generate_tailored`, `_maybe_auto_tailor`, `POST /api/v1/resume/tailor`, `GET /api/v1/resume/for-job`, `GET /api/v1/resume/versions`) | `llm.py: llm_tailor_resume`, `resume_pdf.py: generate_pdf` | `resume_versions` (kind=`tailored`), `resume_blobs` |
| 投递留痕 `POST /api/v1/applications/snapshot`, `GET /api/v1/applications/{job_id}/snapshot` | `db.py: save/get_snapshot` | `application_snapshots` |

Caroline owns the **per-job tailored resume** (never invents / deletes experience —
`llm.py: TAILOR_PROMPT` iron rules stay). The master resume stays Jennifer's.
CAPTCHA / 待人工 (`needs_manual`) is Caroline's: she sends the manual push
(`send_web_push`, via `backend/agents/shared/push.py`) and drives the
`_maybe_auto_tailor` background generation.

### 3.4 Gloria — 复盘 / Review Coach

| Function / route (today in `app.py`) | Shared service used | Tables |
|---|---|---|
| Gmail 同步 cron (`POST /api/v1/admin/email-sync` → `email_sync.sync_all`) | `email_sync.py: sync_user / sync_all`, `gmail.py` | `email_events`, `gmail_tokens`, `gmail_oauth_states` |
| `GET /api/v1/email-events`, `GET /api/v1/gmail/*` | `db.py: list_email_events` | `email_events`, `gmail_tokens` |
| 复盘 `GET /api/v1/reports/digest`, `POST /api/v1/reports/advice` | `llm.py` (advice prompt) | reads `applications`, `email_events` |
| 聊天 `POST /api/v1/chat`, `GET/POST/DELETE /api/v1/chat/conversations*` | `llm.py: llm_chat / llm_detect_action` | `chat_conversations`, `chat_messages` |

Gloria owns **review and Gmail**: she classifies replies, updates application
statuses from mail (`interviewing` / `rejected`), and sends the reply/interview
pushes. Her chat is the user-facing conversation surface — but **chat commands
that act on other agents' domains never call those agents directly**: they are
routed via Rachel (§4), e.g. `pause_schedule` / `resume_schedule` /
`add_to_queue` / `mark_applied` (today `_run_chat_action` in `app.py`).

### 3.5 Rachel — 总管 · 排班 / Supervisor & Scheduler (thin)

| Function / route (today in `app.py`) | Tables |
|---|---|
| 定时设置 `GET/PUT /api/v1/settings` (`schedule_enabled`, `schedule_time`, `daily_count`) | `user_settings` |
| 聊天命令路由 (`run_chat_action` dispatch: pause/resume/add-to-queue/mark-applied) | `user_settings`, `applications` |
| 抓取失败重试 `retry_stale_discover(uid, task_id)` — 重跑被打断的抓取任务 | `discover_tasks` |
| 全局状态聚合 `GET /api/v1/agents` (Step 1) | reads `agent_events`, `discover_tasks`, `run_progress` |

Rachel holds **no business logic of her own** — scheduling, global state,
routing, retries. If a decision belongs to a lane agent, Rachel routes to it;
she does not re-implement it. (The background scorer thread belongs to
Jennifer, not Rachel — it lives in `agents/jennifer.py` as
`start_background_scorer()`; the application-retry endpoint belongs to
Caroline. `retry_stale_discover` is implemented but UNWIRED — see §11.)

### 3.6 Shared / 共享 (no single owner)

| Piece | Notes |
|---|---|
| `backend/agents/shared/push.py` — `send_web_push(user_id, title, body)` | Moved as-is from `app.py` (Step 1). Best-effort Web Push. Push routing rule: reply/interview pushes = Gloria; CAPTCHA/manual pushes = Caroline. |
| `db.py`, `llm.py`, `matcher.py`, `ingest.py`, `gmail.py`, `email_sync.py`, `resume_pdf.py` | Shared services — imported by agents, never rewritten by lane work. |
| Auth (`auth_users`, `api_tokens`, `auth_sessions`), invites, `push_subscriptions` | Infrastructure; stays in `app.py` / `db.py`, not agent-owned. |
| `GET /health`, Dashboard HTML routes | Unchanged, not agent-owned. |

## 4. 边界决策 Boundary Decisions

1. **Keywords: Jennifer → Amy via existing cache.** Jennifer generates keywords
   (`llm_job_keywords`, 7-day cache in `user_settings`); the cache is the
   handoff. Amy's `_keywords_for()` delegates (lazy import) to Jennifer's full
   version so a stale/empty cache still regenerates via LLM, exactly as
   `app.py` did — a read-through, not Amy doing Jennifer's work.
2. **Resumes: master = Jennifer, per-job tailored = Caroline.** Caroline's
   tailor may only reword/reorder Jennifer's master resume; it must never invent
   or delete experience (existing `TAILOR_PROMPT` iron rules).
3. **Push: shared channel, split ownership.** `send_web_push` lives in
   `backend/agents/shared/push.py` and is callable by any agent, but the
   *decision* to push is owned: reply/interview pushes → Gloria; CAPTCHA /
   manual-intervention pushes → Caroline.
4. **Gloria's chat commands route via Rachel, never direct cross-agent calls.**
   When chat detects an action (`llm_detect_action`), Gloria hands the action to
   Rachel, who dispatches it to the owning agent (pause/resume schedule → Rachel
   herself; add-to-queue / mark-applied → Caroline). Gloria never touches
   `applications` or `user_settings` writes directly for these.
5. **Fetching only ingests as-is; scoring only adds scores.** The
   authorized-countries list never rewrites fetched jobs — it acts at fill time
   (Caroline) and, if chosen, at queue display.
6. **No behaviour change in Step 1–4.** Same tables, same routes, same response
   shapes. The extension (0.2.14) sees zero change.

## 5. 拓扑与通信 Topology & Communication

- **Supervisor + sequential pipeline + shared-state blackboard (Postgres).**
  Pipeline order: Jennifer (profile/keywords/score) → Amy (fetch) →
  Caroline (queue/apply) → Gloria (review/chat). Rachel supervises and routes.
- **Blackboard = Postgres rows.** Agents read/write their owned tables and read
  others' tables. Cross-agent calls are allowed ONLY as lazy imports of a
  pure function where `app.py` already had that coupling (the sanctioned
  exceptions, §11): keywords read-through (Amy → Jennifer), synchronous
  scoring inside `add_job` (Amy → Jennifer), resume-text helpers (Caroline →
  Jennifer), queue read for chat context (Gloria → Caroline). No lane calls
  another lane's state-changing orchestration; commands go via Rachel.
- **Activity log = `agent_events`** (§6): every agent writes short events
  (`agent`, `event`, `summary`, `ref_id`) so the team page and Rachel can see
  what happened without polling business tables.
- **No new framework.** Plain Python modules; `app.py` becomes thin routes that
  delegate to `backend/agents/<name>.py`.

## 6. Agent 事件表 agent_events

```sql
-- Postgres: id BIGSERIAL PRIMARY KEY; SQLite: id INTEGER PRIMARY KEY AUTOINCREMENT
CREATE TABLE IF NOT EXISTS agent_events (
    id          <serial>,
    user_id     TEXT NOT NULL,
    agent       TEXT NOT NULL,   -- jennifer | amy | caroline | gloria | rachel
    event       TEXT NOT NULL,   -- short machine-readable key, e.g. "discover_done"
    ref_id      TEXT,            -- related id (job_id / task_id / application id…)
    summary     TEXT,            -- human-readable, Chinese, one line
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Helpers in `db.py` (style follows `add_email_event` / `list_email_events`):

- `db.log_agent_event(user_id, agent, event, summary, ref_id=None)`
- `db.list_agent_events(user_id, agent=None, limit=50) -> list[dict]`,
  newest first.
- `delete_user_data()` wipes a user's `agent_events` rows (account deletion).

## 7. Agent 接口 Agent Interface

Each lane module lives at `backend/agents/<name>.py` (`jennifer.py`, `amy.py`,
`caroline.py`, `gloria.py`, `rachel.py`) and exposes, when implemented:

```python
def status(user_id: str) -> dict:
    """Return {"status": "active" | "idle",
               "current_task": <Chinese string> | None,
               "today_stats": [{"label_zh": ..., "label_en": ..., "value": int}],
               "last_activity": <ISO timestamp> | None}"""

def recent_events(user_id: str, limit: int = 5) -> list:
    """Return [{"event": ..., "summary": ..., "created_at": ..., "ref_id": ...}],
    newest first. Defaults to db.list_agent_events(user_id, agent=<id>, limit)."""
```

Rules:

- `status` is `"active"` **only from real evidence** (a running discover task,
  `run_progress` showing `running`, recent `agent_events` within 1h); otherwise
  `"idle"`. Never invent activity.
- `today_stats` values must come from real DB counts (e.g. today's new jobs,
  submitted applications, interview events); `[]` until the lane lands.
- The `GET /api/v1/agents` endpoint lazy-imports each module and calls
  `status(user_id)` / `recent_events(user_id, 5)` **if present**; a missing
  module or function is not an error — that agent is reported `idle` with
  events from `db.list_agent_events`. The endpoint must never 500 because a
  lane module is missing.

## 8. 团队状态接口 GET /api/v1/agents

Auth: same `Depends(get_current_user)` as other `/api/v1` routes.
Response shape (exact):

```json
{"agents": [{"id": "jennifer", "name": "Jennifer",
             "role_zh": "简历与市场分析", "role_en": "Resume & Market Analysis",
             "status": "active",
             "current_task": "正在抓取职位…",
             "today_stats": [{"label_zh": "新职位", "label_en": "New jobs", "value": 12}],
             "last_activity": "2026-11-05T10:00:00",
             "recent_events": [{"event": "discover_done", "summary": "抓取完成：新增 12 个职位",
                               "created_at": "2026-11-05T10:00:00", "ref_id": null}]}]}
```

Order: jennifer, amy, caroline, gloria, rachel. Roles:

| id | role_zh | role_en |
|----|---------|---------|
| jennifer | 简历与市场分析 | Resume & Market Analysis |
| amy | 职位搜罗 | Job Hunter |
| caroline | 填表投递 | Application Specialist |
| gloria | 复盘 | Review Coach |
| rachel | 总管·排班 | Supervisor & Scheduler |

## 9. app.py 变薄 Thin Routes (target state — DONE 2026-11-05)

- `app.py` (835 lines, was ~1,692) keeps: FastAPI setup, auth (`get_current_user`), rate limiting,
  shared Pydantic models (AuthIn/TokenIn/ClaimIn/InviteIn/PushSubIn), the agents-endpoint
  helpers, and thin route handlers that resolve the user and delegate to the owning
  agent module (`backend/agents/<name>.py`). All business logic lives in the lane modules.
- Step 1 moved only `send_web_push` →
  `backend/agents/shared/push.py` and adds `GET /api/v1/agents` + `agent_events`.
- No new public admin/diagnostic endpoints (only `/api/v1/agents`). No extension changes. No secrets in code.

## 10. 实施顺序 Rollout

1. **Foundation (this step):** this doc, `backend/agents/` package layout,
   `shared/push.py` (only `send_web_push` moved), `agent_events` table +
   helpers, `GET /api/v1/agents`. Tests: 56 existing + new agent tests.
2. **Jennifer lane:** resume parse / profile / keywords / scoring move into
   `backend/agents/jennifer.py`. ✅ Done 2026-11-05, tests green after the slice.
3. **Amy lane:** discover / fetch move into `backend/agents/amy.py`. ✅ Done 2026-11-05.
4. **Caroline lane:** queue / apply / tailor move into `backend/agents/caroline.py`. ✅ Done 2026-11-05.
5. **Gloria lane:** Gmail sync / reports / chat move into `backend/agents/gloria.py`. ✅ Done 2026-11-05.
6. **Rachel + acceptance:** supervisor routing, team page, full test suite +
   fresh-account E2E; deploy only after confirming no fetch is running.
   Rachel lane ✅ done 2026-11-05; team page ✅ done; backend 59 + extension 77 green.
   Live E2E still pending deploy approval (see `docs/multi-agent-e2e-plan.md`).

## 11. 落地决议 Integration Decisions (2026-11-05, coordinator)

Decisions taken while wiring `app.py` to the lane modules. Business logic unchanged
unless a decision below says otherwise.

1. **Duplicate `put_profile` — live behaviour preserved.** `app.py` defined
   `PUT /api/v1/profile` twice: #1 (plain upsert) and #2 (upsert + keywords
   invalidation + score reset + rescore). Probed with a real TestClient: #1
   serves HTTP (Starlette matches the first-registered route); #2's reset
   logic is dead code for HTTP traffic. The thin route wires to #1's behaviour.
   #2 lives on as `jennifer.put_profile_with_reset`, UNWIRED — enabling it
   would be a behaviour change (profile saves would start resetting scores)
   and needs Victoria's call. Side effect of the fix: the FastAPI duplicate
   operation-ID warning is gone (single PUT route now). The existing test
   `test_profile_update_resets_keywords_and_scores` passes vacuously (it reads
   settings for `user_id ""`) and does not cover either behaviour.
2. **Sanctioned cross-agent service calls (lazy imports).** The only allowed
   direct calls between lanes are pure-function delegations where `app.py`
   already had that coupling: Amy → Jennifer `_keywords_for` (read-through)
   and `score_and_store` (synchronous scoring inside `add_job`); Caroline →
   Jennifer `_resume_display_name` / `_resume_text_from_profile`; Gloria →
   Caroline `_queue_jobs` (chat context). Each is a single canonical
   implementation; no copies. No lane calls another lane's orchestration.
3. **`_queue_jobs` uses stored scores, not `matcher.build_queue`.** The queue
   filters stored hybrid scores (≥ 40), manual-first, applied-excluded.
   `build_queue` recomputes rules-only scores and would change queue order.
4. **Snapshots do NOT record `version_id` / tailored flag.** The design
   assumed they did; `app.py` as-is passes no such keys and `db.save_snapshot`
   defaults them to 0. Preserved verbatim — recording them would be a
   behaviour change, not part of this split.
5. **`retry_stale_discover` implemented, UNWIRED.** Rachel's helper re-runs a
   stale/error discover task via Amy's runner, but no existing call path
   (route or chat action) invokes it. Wiring it needs a product decision
   (new chat action? auto-retry on stale?) — pending Victoria.
6. **Dead `admin_chat_debug` ×3 left untouched, now references deleted names.**
   The three unrouted dead functions reference `_queue_jobs` and
   `_run_chat_action`, which no longer exist at `app.py` module level. They
   are never called (no route, no caller), so this is inert — but the next
   cleanup should delete them rather than revive them.
7. **Test expectation updated, not behaviour.** `test_agents_endpoint_returns_five_and_requires_auth`
   previously expected `today_stats == []` for all agents on a fresh user;
   registration enables the schedule by default (08:00, 10/day), so Rachel
   truthfully reports her plan stat. The test now asserts that stat for
   Rachel and `[]` for the other four.
8. **Rewire method.** `app.py` was rewired lane by lane with a script
   (`/tmp/rewire_app.py`, kept out of the repo) that replaced each moved
   handler with a thin delegator (decorators, def line, and docstring
   preserved) and deleted moved helpers/models. Temporary shims
   (`score_and_store`, `_keywords_for`, resume helpers, `_queue_jobs`
   aliases) kept the suite green between slices and were all removed by
   the final slice. One script bug caught in review: a dangling
   `@app.put("/api/v1/profile")` decorator from the deleted duplicate
   `put_profile` #2 stacked onto `post_snapshot` — fixed before testing,
   verified by route-table diff (74 routes = 73 baseline + `GET /api/v1/agents`).
