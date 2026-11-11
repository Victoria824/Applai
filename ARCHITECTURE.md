# Applai — Architecture & Multi-Agent Lanes (ARCHITECTURE.md)

> Status: DRAFT v0.1 — 2026-11-05 (草稿 v0.1). Written after Victoria asked (2026-11-05) to split the work across multiple agents because small cross-boundary bugs kept appearing (0.2.13 banner, 0.2.10 Ashby tab). Not yet approved — the pilot feature that tests this structure is still her call. Nothing in this file changes code; it only records who owns what and which contracts must hold before a release.
>
> Codebase snapshot this draft is checked against: extension `0.2.14` (`extension/manifest.json`), backend at `https://applai-backend.fly.dev`, latest commit `a146a66` (0.2.14, required-questions + country-aware work authorisation). File sizes below were read from the working tree on 2026-11-05; re-check them if the tree has moved on.

## 1. Why split (为什么要拆)

The bugs were not "one agent is too slow". They were context overload plus no independent check at the boundary:

- **0.2.13** — the fill report and the resume banner disagreed about whether a resume was attached. Root cause: two places derived the same status instead of one.
- **0.2.10** — the Ashby form was missed because the test fixture did not match the real page structure (Overview tab vs Application tab).
- **2026-11-03 deploy** — a 0.2.14 deploy restarted the Fly machine while a fetch was running and killed it (daemon thread dies with the process). Root cause: no pre-deploy check for a running fetch.

Splitting helps only if each lane owns one area, boundaries are written contracts, and an independent QA lane can block a release. Without those three, more agents just means more integration bugs — and cross-lane work gets a little slower, because both sides must align first.

## 2. Roles (角色): 5 roles, 4 lanes

| Role (角色) | Owns (负责) | Files (文件) | Must NOT do (不做) |
|---|---|---|---|
| **Coordinator (总协调, still Muse)** | Single point of contact with Victoria; splits tasks; sets cross-lane contracts; decides releases; tiny one-line fixes directly | This file, release notes | Write lane code |
| **Backend lane (后端线)** | FastAPI, Postgres, scoring, fetching, Gmail sync, push, API contract | `backend/app.py` (1,565 lines), `backend/db.py` (1,731), `backend/ingest.py`, `backend/llm.py`, `backend/matcher.py`, `backend/gmail.py`, `backend/email_sync.py`, `backend/resume_pdf.py` | Touch extension fill logic or dashboard markup to "fix" a backend field |
| **Extension lane (插件线)** | Content scripts, fill engine, batch runner, manual refill, manifest, ATS quirks | `extension/content/fill.js` (610), `ats.js`, `bridge.js`, `captcha.js`, `connect.js`, `jobbank.js`, `manual.js`, `submit.js`, `extension/content/lib/`, `extension/background/sw.js` (482), `extension/popup/popup.js` (511), `extension/manifest.json` | Change an API field or profile shape unilaterally |
| **Console lane (控制台线)** | Dashboard page, onboarding wizard, profile forms, queue / manual / history views, and the data shapes they share with backend + extension | `backend/dashboard.html` (2,374 lines), `backend/login.html`, `backend/dashboard-sw.js` | Re-derive a fill status in the UI instead of rendering the report |
| **QA lane (验证线)** | Tests and acceptance only; runs full suites; verifies live on a throwaway account; **the only role that can block a release** | `backend/test_backend.py` (56 tests, verified 2026-11-05), `test/fill.test.js`, `test_fixtures/` | Write product code |

## 3. Contracts (约定) — write the contract first, then build to it

### C1 — Fill report is the single source of truth (填写报告是唯一事实来源)

- Producer: `fillApplication()` in `extension/content/fill.js:488-495` returns `{ filled: [], missing: [], needsReview: [], captcha, autoSubmitted, ats }`.
- Consumer: `renderReport()` in `extension/popup/popup.js:188-197` renders those three lists verbatim. It never re-derives a status.
- Resume rule (the 0.2.13 lesson, `fill.js:560-581`): if any file box received the resume, `resumeAttached = true` and the report records `filled` with intent `resume`; only when no box got it does the report record `missing` intent `resume` ("请手动点击红色框上传"). A resume counts as attached if **any** file box got it — including the Ashby Autofill helper box case.
- Any new status shown in the popup must first exist in the report shape. If the popup and the report disagree, the report wins and the popup is the bug.

### C2 — Profile + work authorisation (画像与工作许可)

- API: `PUT /api/v1/profile` and `GET /api/v1/profile` (`backend/app.py:149-155`); multi-profile endpoints at `app.py:170-213`.
- New 0.2.14 fields: `authorizedWorkCountries` (list), `needsSponsorship` (boolean), `currentCompany`, `currentTitle`. The old `workAuth` free-text field no longer drives answers.
- Fill rule (`fill.js:71-74, 129-134`): work authorisation is answered **Yes only when every country on the posting is in `authorizedWorkCountries`**; otherwise leave it for manual review. **Never auto-answer No.** The posting's countries arrive via `options.jobLocation` (`fill.js:112`) — backend and extension must both pass it on batch and manual paths.
- Sponsorship: `needsSponsorship === true` → `yes`, `false` → `no`, unset → blank (never default to `no`).

### C3 — ATS detection and page fixtures (ATS 识别与测试夹具)

- Detection rules live in `extension/content/ats.js` (Greenhouse, Lever, Ashby, Workday, iCIMS — URL + DOM selectors, URL 70 / DOM 30).
- Ashby tab rule (`fill.js:474-511`): on `ashbyhq.com`, click the **Application** tab before scanning — the form hides behind it; scanning the Overview page mistakes the showcase page for a form. This is the 0.2.10 lesson.
- Fixtures must replicate the real page's actual DOM structure (the selectors in `ats.js` and the tab layout), not a simplified form. A fixture that passes while the real page fails is a fixture bug, and QA owns catching it.

### C4 — Queue, snapshots, and fetch tasks (队列、快照与抓取任务)

- Queue status enum, set only by the popup (`popup.js:250`): `queued | filled | needs_manual | submitted | failed`. The backend records applications via `POST /api/v1/applications` and snapshots via `POST /api/v1/applications/snapshot` (`app.py:537, 640`); the snapshot freezes URL, JD, filled fields, and resume hash.
- Fetch runs server-side as a daemon thread (`app.py:386-392`): leaving the fetch page never interrupts it — only the local progress poll stops, and re-clicking fetch starts a **parallel** run (no duplicate guard yet).
- Stale-task rule (`app.py:401-407`): a task stuck in `running` for more than 20 minutes is marked `error / 任务中断`. Already-saved jobs are safe; only the unfinished half is lost, and recovery is a re-click.
- **Deploy rule (2026-11-03 lesson): never deploy while a fetch is running.** Check `GET /api/v1/jobs/discover/status/{task_id}` first. GitHub Actions auto-deploys on push touching `backend/**` (`.github/workflows/deploy.yml`), so the check happens before pushing, not after.

### C5 — Release gate (发布门)

- No deploy without QA's live sign-off on a throwaway account (fresh-account verification, per the clean-slate rule: persisted state is scoped by account id, never by browser).
- QA runs `backend/test_backend.py` (56 tests) and `test/fill.test.js` (extension suite; count recorded as 77 on 2026-11-03 — re-verify before citing) plus the live check.
- Small fixes skip the full process: a one-line fix goes straight through the Coordinator, no four-role meeting.

## 4. How work flows (协作流程)

1. **Contract first (先定约定再动手):** any cross-lane change starts with a written contract here (API fields, message types, profile fields). Both sides build to the same text.
2. **Small batches (小步交付):** one feature = one lane's change + its tests. Cross-lane features ship as a coordinated pair with one shared acceptance test.
3. **QA blocks (验证线拦发布):** only QA can block a release; no live verification sign-off, no deploy.
4. **One source of truth (一个事实来源):** decisions live in this file and the contracts above, not only in chat history.

## 5. Pilot (试点) — run ONE feature through this structure, then judge

Victoria picks one (still her call; the queue-filter choice is one of the three open Applai items from 2026-11-04 and is not re-raised here):

- **Option A — queue filtering for unauthorised-country jobs** (fill-time check only is live today; queue filtering is not built). Touches Backend (queue endpoint `GET /api/v1/queue`, `app.py:483`) + Console (queue display) + QA (acceptance: an unauthorised-country job never appears in the queue).
- **Option B — portfolio file-box follow-up** (the leftover prompt). Touches Extension only: `fill.js:557-579` — today every non-resume file box (portfolio, transcript, cover letter) goes to the manual queue with a "manual upload" prompt. The pilot question is what, if anything, should be auto-filled vs. left manual, and the contract for how the report labels it (C1).

Judge the pilot on: fewer cross-boundary bugs than the 0.2.10–0.2.14 run, and whether the extra alignment step was worth it. If it was not, keep single-agent for small features and use lanes only for cross-lane work.

## 6. What this draft does NOT decide (本稿不决定的事)

- The queue-filter scope (Option A) — hers.
- The three open Applai items from 2026-11-04 (profile fields, fetch re-click, queue-filter choice) — unchanged, hers.
- Friend-beta send pack still ships extension 0.2.14.
