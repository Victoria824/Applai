# Applai Multi-Agent — Live End-to-End Verification Plan

**Status: PREPARED, NOT RUN.** This plan requires a deployed backend built from
branch `multi-agent`. Do not run it against production/main or against a local
dev server and call that a pass. The coordinator arranges deployment approval;
the QA lane runs the plan only after that approval lands.

**Branch:** `multi-agent` in `~/workspace/job-apply-tool` (GitHub Victoria824/Applai).
**Never merge to main, never push to main, never deploy from this lane.**
**Extension under test:** 0.2.14 (unchanged — `extension/` must stay clean).

## 0. Baselines captured 2026-11-05 (pre-refactor, branch `multi-agent` @ `a146a66`)

| Check | Result |
|---|---|
| Backend suite (`backend/.venv/bin/python -m pytest test_backend.py -q`) | **56 passed**, 3 warnings (pre-existing `on_event` deprecation warnings) |
| Extension suite (`node test/fill.test.js`) | **77 passed, 0 failed** |
| OpenAPI snapshot | `/tmp/applai-baseline-openapi.json` (53 paths, 60,045 bytes) |
| Route list | `/tmp/applai-baseline-routes.txt` — **77 lines** (73 excluding the 4 auto `HEAD` entries for `/docs`, `/redoc`, `/openapi.json`) |
| `GET /api/v1/agents` in baseline | **Absent** (it is the one allowed addition) |

Pre-existing baseline quirk (not a regression, do not "fix" in this lane):
FastAPI warns `Duplicate Operation ID put_profile_api_v1_profile_put` for
`PUT /api/v1/profile`. If it disappears or multiplies after the refactor, note
it; it is not by itself a gate failure.

## 1. Preconditions & deploy guard

### 1.1 Deploy guard — MUST run before any deploy
1. Check whether Victoria has a fetch running: `GET /api/v1/jobs/discover/status/{task_id}`
   for any recent task, and/or ask the coordinator to confirm with her.
   **Rule (from the 2026-11-03 lesson): never deploy while a fetch is running** —
   a Fly restart kills the daemon thread mid-fetch and the unfinished half is lost.
2. Confirm the deploy target is the `multi-agent` branch build, not `main`.
3. Confirm the deployed backend answers `GET /health` and `GET /api/v1/auth/me`
   returns 401 unauthenticated (i.e. auth is live, not bypassed).

**Pass criteria:** no fetch in flight at deploy time; health OK; deploy target verified.

### 1.2 Test account
- Use a **throwaway account only** (fresh registration, e.g. `qa-multiagent+<date>@…`).
  Never use Victoria's real account or any friend-tester account.
- The account is deleted at the end of the run (`DELETE /api/v1/account`,
  expect 401 afterwards on any authed call — same check as the 0.2.14
  one-off verification).
- Use a small, known-good source set for fetch (the same free aggregators /
  boards used in the 0.2.14 verification) so results are comparable.

## 2. The chain — step by step

Each step lists the **exact endpoint/page**, **what to observe**, and **pass criteria**.
Record the actual observed value for every step (status code, counts, IDs);
"it worked" without evidence is not a pass. Stop the run on the first hard
failure, file the failure with the step number, and do not skip ahead to
"make the rest pass".

### Step 1 — Register (auth)
- **Endpoint:** `POST /api/v1/auth/register`, then `GET /api/v1/auth/me`.
- **Observe:** registration returns a usable session/token; `/auth/me` returns
  the new account.
- **Pass:** 2xx on both; `/auth/me` shows the throwaway account, not someone else's.

### Step 2 — Onboarding wizard: upload resume (Jennifer)
- **Page/endpoint:** dashboard onboarding wizard → `POST /api/v1/resume/parse`
  (upload a real resume file, e.g. the standard test resume).
- **Observe:** parse returns extracted profile fields (name, skills, experience);
  no 500, no empty profile.
- **Pass:** parsed fields are non-empty and plausible; the profile draft is created.

### Step 3 — Onboarding wizard: approve profile (Jennifer)
- **Page/endpoint:** dashboard profile page (`#page-profile`) →
  `PUT /api/v1/profile` (approve/confirm the parsed profile).
- **Observe:** `GET /api/v1/profile` returns the approved profile afterwards.
- **Pass:** saved profile matches what was approved; `GET /api/v1/profile`
  round-trips the same values.

### Step 4 — Onboarding wizard: required questions → start
- **Page/endpoint:** wizard "required questions" step → `PUT /api/v1/profile`
  (or the wizard's save call) with the four required fields:
  legal work countries, employer sponsorship, current company, current title.
  (Optional: X link, OTE range, customer segments.)
- **Observe:** the wizard refuses to start while any required field is missing;
  after filling all four, Start proceeds. The wizard remains one-click
  skippable, per spec.
- **Pass:** missing-field blocks start; all-four-filled allows start; skip still works.

### Step 5 — Fetch (Amy: discover / auto)
- **Endpoints:** `POST /api/v1/jobs/discover` and/or `POST /api/v1/jobs/discover/auto`,
  poll `GET /api/v1/jobs/discover/status/{task_id}` until done.
- **Observe:** task completes (not stuck, not "interrupted"); jobs land in the DB.
- **Pass:** status reaches a terminal success state; `GET /api/v1/queue` (or the
  jobs list) shows newly discovered jobs; count > 0. Leaving the fetch page
  mid-run must not interrupt the server-side fetch (known behaviour: only the
  local progress poll stops).

### Step 6 — Score (Jennifer: queue shows scored jobs)
- **Endpoint/page:** dashboard queue page (`#page-queue`) → `GET /api/v1/queue`.
- **Observe:** queued jobs carry scores / match signals (rules + LLM hybrid path
  unchanged); jobs are ordered/filtered as before.
- **Pass:** every queued job has a score; at least one job scores high enough
  to be actionable; queue contents match the pre-refactor behaviour for the
  same sources (no jobs silently dropped, no unscored jobs).

### Step 7 — Apply / manual (Caroline: queue, run-progress, snapshots)
- **Endpoints/pages:** queue page → run batch; `GET /api/v1/run-progress`;
  `GET /api/v1/applications`; `GET /api/v1/applications/{job_id}/snapshot`.
- **Observe:** run-progress advances; application records are created with
  per-application snapshots (URL, site, frozen JD, filled fields, salary,
  cover letter, resume hash); a job that needs manual review appears on the
  Manual page (`#page-manual`).
- **Pass:** (a) at least one application completes end-to-end with a snapshot
  retrievable via the snapshot endpoint; (b) at least one job lands in Manual
  (e.g. work-permission country mismatch → manual, never auto-No — the 0.2.14
  rule); (c) `POST /api/v1/applications/retry` works on a failed record if one
  occurs. **Do not** submit applications to real employers beyond the intended
  test scope — use the same safe/known boards as prior verifications, and
  confirm with the coordinator before any real submission.

### Step 8 — Review (Gloria: digest, email-events, chat + one chat action)
- **Endpoints/pages:** review page (`#page-review`) →
  `GET /api/v1/reports/digest`, `GET /api/v1/email-events`,
  `POST /api/v1/reports/advice`, `POST /api/v1/chat` (+ `GET /api/v1/chat/conversations`).
- **Observe:** digest renders with the run's stats; email-events list (empty is
  acceptable on a fresh account — record it as empty, not as a failure);
  chat returns a reply; **one chat action executes** (e.g. pause/resume, add to
  queue, or mark applied — pick one, record which).
- **Pass:** digest matches the run's actual counts; chat replies; the chosen
  chat action takes visible effect (verify by re-reading the affected state,
  e.g. queue or application status, not just the chat's acknowledgement text).

### Step 9 — Team page (Rachel + all five agents)
- **Endpoint:** `GET /api/v1/agents`.
- **Page:** dashboard Team page (new; nav entry renders).
- **Observe:** endpoint returns **5 agents** — Jennifer (resume/market),
  Amy (fetch), Caroline (apply), Gloria (review), Rachel (supervisor) —
  each with a live status (not hardcoded strings: status must reflect the
  run just performed, e.g. Amy shows the fetch task, Caroline shows the
  apply run).
- **Pass:** exactly 5 agents; names/roles match the spec; every agent has a
  status field; statuses are consistent with the steps above (no agent
  reports "idle/unknown" for work this run actually did); Team page cards
  render for all 5 in the dashboard.

### Step 10 — Cleanup
- **Endpoint:** `DELETE /api/v1/account`; then `GET /api/v1/auth/me` (expect 401).
- **Pass:** account deleted; subsequent authed call is 401; no throwaway data
  left in the queue/stats visible to other accounts.

## 3. Regression gates (enforced at final verification)

These are the gates the QA lane enforces before the multi-agent branch can be
called done. All five must pass; any failure blocks sign-off.

| # | Gate | How to check |
|---|---|---|
| (a) | Backend suite green | `cd backend && .venv/bin/python -m pytest test_backend.py -q` → **59 passed** (56 baseline + 3 new agents-endpoint tests added by the split; any drop or new failure blocks) |
| (b) | Extension suite green | `cd ~/workspace/job-apply-tool && node test/fill.test.js` → **77 passed, 0 failed** |
| (c) | OpenAPI diff vs baseline shows ONLY the added `GET /api/v1/agents` | Regenerate `/openapi.json` the same way as the baseline (TestClient snippet, §0) and diff against `/tmp/applai-baseline-openapi.json`: no removed routes, no changed paths/methods on existing routes, no changed request/response schemas on existing routes. The single allowed delta is the new `GET /api/v1/agents` path (plus its schemas). Any other delta blocks. |
| (d) | No extension file changed | `git status --short -- extension/` is clean on branch `multi-agent` |
| (e) | Dashboard still serves and Team page exists | `GET /dashboard` returns 200 and the served HTML contains the Team page / nav entry; `GET /api/v1/agents` returns the 5 agents (Step 9) |

## 4. Rollback

- Branch `multi-agent` can be abandoned at any point: **main is untouched**
  (no merges, no pushes to main, no deploys from this lane).
- Rollback = stop using the `multi-agent` deployment and redeploy `main`.
  No data migration is involved (same tables), so no rollback migration is needed.
- If the live E2E fails at any step, record the step, the observed vs expected
  values, and the backend logs for that window; do not patch product code from
  the QA lane — hand the failure back to the owning lane with the evidence.

## 5. Reporting

The QA lane reports, per step: endpoint/page, observed result, pass/fail, and
the evidence (status codes, counts, IDs, timestamps). The final report also
restates the five regression gates with their outcomes. The live E2E is
**pending deploy approval** until the coordinator confirms a deployment window
with the fetch guard (§1.1) satisfied.
