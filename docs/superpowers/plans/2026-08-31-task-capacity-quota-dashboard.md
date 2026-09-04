# Task Capacity, User Quota, and Platform Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Limit ordinary users to a configurable number of tasks, admit at most one enabled task per two-minute Beijing-time slot, route retries to free slots, and show every signed-in user a privacy-safe live summary of today's platform activity.

**Architecture:** Add an additive per-user quota table and focused capacity/statistics services. Keep task admission in `TaskService`, serialize Web task writes with one process lock, and make every create/edit/enable path use the same checks. Derive platform status from enabled tasks, each task's latest run during the current Shanghai day, and the existing Worker lease; render once server-side and refresh through an authenticated JSON endpoint.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, SQLite WAL, Jinja2, vanilla JavaScript/CSS, pytest/unittest.

**Spec:** `docs/superpowers/specs/2026-08-31-task-capacity-quota-dashboard-design.md`

## Global Constraints

- Use `Asia/Shanghai` for daily boundaries and schedule buckets.
- One enabled task is admitted per two-minute bucket; paused tasks do not occupy a bucket.
- Ordinary users inherit a default total-task limit of 5; administrators are unlimited.
- Existing tasks are never deleted, paused, or moved automatically.
- Dashboard responses expose aggregate counts only, never usernames, account IDs, targets, or message text.
- Existing unsafe-to-retry stages remain excluded from automatic retry.
- Do not commit, push, or deploy until the user explicitly authorizes that external action.
- Preserve every pre-existing dirty-worktree change.

---

### Task 1: Per-user task quota persistence and service

**Files:**
- Modify: `spark_console/models.py`
- Create: `spark_console/services/task_capacity.py`
- Modify: `spark_console/services/tasks.py`
- Test: `tests/console/test_services.py`

**Interfaces:**
- Produces: `UserTaskQuota(user_id: str, task_limit: int)`.
- Produces: `TaskCapacityService.limit_for(user: User) -> int | None` where `None` means unlimited.
- Produces: `TaskCapacityService.usage_for(user_id: str) -> int`.
- Produces: `TaskCapacityService.set_limit(actor_id: str, user_id: str, limit: int) -> UserTaskQuota`.
- Produces: `TaskCapacityService.assert_can_create(user: User) -> None` raising `ValidationError` with the public `当前任务 X/Y` message.

- [ ] **Step 1: Write failing quota tests**

Add focused tests that create five total tasks for an ordinary user, assert the sixth raises `ValidationError`, assert paused tasks still count, assert an administrator remains unlimited, and assert changing the stored limit from 5 to 8 immediately permits more tasks. The mutation each test catches is removing one quota branch or counting only enabled tasks.

- [ ] **Step 2: Verify the quota tests fail for missing behavior**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/console/test_services.py -q
```

Expected: failures because `UserTaskQuota`/`TaskCapacityService` do not exist or the sixth task is still accepted.

- [ ] **Step 3: Add the additive model and quota service**

Declare the table without changing `users`:

```python
class UserTaskQuota(Base):
    __tablename__ = "user_task_quotas"
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    task_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
```

Implement `TaskCapacityService` with `DEFAULT_TASK_LIMIT = 5`, accepted overrides `1 <= limit <= 100`, `func.count(SparkTask.id)` across enabled and paused tasks, and an `AuditService.write(..., "user.task_limit_updated", "user", user_id)` event.

- [ ] **Step 4: Enforce quota in the task creation boundary**

Inject or construct `TaskCapacityService` inside `TaskService.create`, load the owner `User`, call `assert_can_create` immediately before adding `SparkTask`, and keep existing validation and duplicate handling unchanged.

- [ ] **Step 5: Run quota tests and model/schema tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/console/test_services.py tests/console/test_config_db.py -q
```

Expected: all tests pass and `create_schema` creates the new table on an existing database.

- [ ] **Step 6: Review checkpoint**

Inspect only the Task 1 diff and confirm no existing user/task data migration or deletion was introduced. Do not commit without explicit authorization.

---

### Task 2: Two-minute capacity, nearby suggestions, and all write paths

**Files:**
- Modify: `spark_console/services/task_capacity.py`
- Modify: `spark_console/services/tasks.py`
- Modify: `spark_console/web/app.py`
- Test: `tests/console/test_services.py`
- Test: `tests/console/test_web_user.py`
- Test: `tests/console/test_registration_web.py`

**Interfaces:**
- Produces: immutable `SlotAvailability(available: bool, remaining: int, suggestions: tuple[str, ...])`.
- Produces: `TaskCapacityService.bucket_for(send_time: str) -> int`, using `minutes_since_midnight // 2`.
- Produces: `TaskCapacityService.availability(send_time: str, exclude_task_id: str | None = None) -> SlotAvailability`.
- Produces: `TaskCapacityService.assert_slot_available(send_time: str, exclude_task_id: str | None = None) -> None`.
- Produces: `TaskCapacityService.next_available_times(send_time: str, count: int = 3) -> tuple[str, ...]`.
- Produces: authenticated `GET /tasks/availability?send_time=HH:MM&exclude_task_id=...` returning only `available`, `remaining`, and `suggestions`.

- [ ] **Step 1: Write failing slot boundary tests**

Cover these literal cases:

```python
assert bucket_for("11:00") == bucket_for("11:01")
assert bucket_for("11:02") != bucket_for("11:01")
```

Create an enabled 11:00 task and assert 11:01 is rejected, 11:02 is accepted, editing that same task at 11:01 ignores itself, pausing releases the slot, and re-enabling fails when another enabled task took the bucket. Assert suggestions for occupied 11:00 include the nearest free times in distance order with no occupied values.

- [ ] **Step 2: Verify slot tests fail**

Run the named new tests with `pytest -q`; expected failure is missing capacity behavior, not malformed fixtures.

- [ ] **Step 3: Implement slot calculation and validation**

Query enabled tasks and compare bucket integers rather than raw `send_time`, ignore `exclude_task_id`, and raise:

```python
ValidationError("该两分钟执行时段已满，请选择推荐时间")
```

Search outward by two-minute buckets, clamp/wrap within a 24-hour day, deduplicate candidates, and return exactly three available `HH:MM` suggestions when possible.

- [ ] **Step 4: Serialize Web task writes and route every mutation through TaskService**

Add one module-level `threading.Lock` in the Web application and hold it around the transaction for:

- ordinary task creation;
- ordinary task editing;
- ordinary pause/enable;
- administrator task editing;
- administrator pause/enable.

Extend `TaskService.set_enabled_owned` and add an administrator-safe service method that runs slot validation before enabling. Remove direct `task.enabled = not task.enabled` mutations from routes. Keep delete operations outside slot validation because deletion only frees capacity.

- [ ] **Step 5: Add and test the availability endpoint**

Require an authenticated session, validate `HH:MM`, verify `exclude_task_id` belongs to the requesting user unless the requester is an administrator, and return a fixed JSON shape:

```json
{"available": false, "remaining": 0, "suggestions": ["10:58", "11:02", "11:04"]}
```

Test unauthenticated redirect/rejection and assert the JSON contains no task/user fields.

- [ ] **Step 6: Add a same-process concurrent submission regression test**

Use two test clients/threads against the file-backed SQLite test engine to post different tasks into 11:00 and 11:01 simultaneously. Assert exactly one response succeeds and exactly one enabled task occupies that bucket.

- [ ] **Step 7: Run focused service and Web tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/console/test_services.py tests/console/test_web_user.py tests/console/test_registration_web.py -q
```

Expected: all focused tests pass.

- [ ] **Step 8: Review checkpoint**

Confirm every task-enabling path uses the capacity service and existing conflicting tasks were not changed. Do not commit without explicit authorization.

---

### Task 3: Route all automatic and manual retries to free execution slots

**Files:**
- Modify: `spark_console/services/task_capacity.py`
- Modify: `spark_console/services/tasks.py`
- Modify: `spark_console/worker.py`
- Modify: `spark_console/web/app.py`
- Test: `tests/console/test_scheduler_worker.py`
- Test: `tests/console/test_services.py`
- Test: `tests/console/test_registration_web.py`

**Interfaces:**
- Produces: `TaskCapacityService.next_available_run_at(earliest: datetime, exclude_task_id: str | None = None) -> datetime`.
- Consumes: existing `schedule_recent_safe_failures(...)`, `Worker._schedule_retry(...)`, and administrator safe-retry route.

- [ ] **Step 1: Write failing retry collision tests**

Create a normal task occupying the first retry bucket and assert Worker retry, successful-relogin retry, and administrator safe retry all choose the next unoccupied two-minute bucket. Keep existing tests proving sending/confirming/submitted failures are never retried.

- [ ] **Step 2: Verify retry tests fail for the fixed-delay behavior**

Run the three named tests and confirm they fail because current code writes `now + 1 minute`/fixed retry delays.

- [ ] **Step 3: Implement `next_available_run_at`**

Normalize `earliest` to UTC, convert candidate buckets through `Asia/Shanghai`, inspect enabled tasks' `next_run_at` values plus queued/running `TaskRun.scheduled_for` values, and move forward in two-minute increments until a free bucket is found. Exclude the task being rescheduled so its next daily run does not block itself.

- [ ] **Step 4: Replace fixed retry assignments**

Use the shared function in all three retry paths while preserving existing retry attempt counts, public summaries, idempotency markers, and unsafe-stage exclusions.

- [ ] **Step 5: Run retry and worker regression tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/console/test_scheduler_worker.py tests/console/test_services.py tests/console/test_registration_web.py -q
```

Expected: retry collision tests and all prior duplicate-send safety tests pass.

- [ ] **Step 6: Review checkpoint**

Verify only scheduling time changed; retry eligibility did not broaden. Do not commit without explicit authorization.

---

### Task 4: Privacy-safe Shanghai-day platform status service and API

**Files:**
- Create: `spark_console/services/platform_status.py`
- Modify: `spark_console/web/app.py`
- Test: `tests/console/test_web_user.py`
- Test: `tests/console/test_registration_web.py`

**Interfaces:**
- Produces: `PlatformStatus(total: int, success: int, running: int, pending: int, failed: int, worker_online: bool, updated_at: datetime)`.
- Produces: `build_platform_status(session: Session, now: datetime | None = None) -> PlatformStatus`.
- Produces: authenticated `GET /api/platform-status` with exactly `total`, `success`, `running`, `pending`, `failed`, `worker_online`, and `updated_at`.

- [ ] **Step 1: Write failing statistics tests**

Build literal fixtures around a Shanghai day boundary and assert:

- enabled tasks with no run today are pending;
- only the latest run per task is classified;
- failure followed by success counts only as success;
- `skipped` counts in failed;
- paused tasks are excluded from total;
- a valid Worker lease reports online and an expired lease reports offline.

- [ ] **Step 2: Verify statistics tests fail**

Run the named new tests; expected failure is the missing service/API.

- [ ] **Step 3: Implement the focused status service**

Compute Shanghai start/end, convert them to UTC, fetch current enabled task IDs and today's runs ordered newest first, retain the first run per task, classify into mutually exclusive buckets, and calculate pending as tasks without a classified run. Return a dataclass/dict containing aggregate values only.

- [ ] **Step 4: Add the authenticated JSON endpoint**

Call `auth.current`, serialize only the seven approved fields, and use ISO-8601 UTC for `updated_at`. Test ordinary user and administrator access, unauthenticated rejection, and absence of known username/target/message markers.

- [ ] **Step 5: Run status/API tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/console/test_web_user.py tests/console/test_registration_web.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Review checkpoint**

Inspect the JSON contract and verify it cannot reveal per-user data. Do not commit without explicit authorization.

---

### Task 5: Dashboard, task availability UX, and administrator quota controls

**Files:**
- Modify: `spark_console/templates/dashboard.html`
- Modify: `spark_console/templates/tasks.html`
- Modify: `spark_console/templates/admin.html`
- Modify: `spark_console/static/app.css`
- Modify: `spark_console/static/tasks.js`
- Create: `spark_console/static/dashboard.js`
- Modify: `spark_console/web/app.py`
- Test: `tests/console/test_web_user.py`
- Test: `tests/console/test_registration_web.py`

**Interfaces:**
- Consumes: `build_platform_status`, quota usage/limit methods, `/api/platform-status`, and `/tasks/availability`.
- Produces: `POST /admin/users/{user_id}/task-limit` with CSRF protection and integer `task_limit`.

- [ ] **Step 1: Write failing rendered-page and admin-action tests**

Assert the dashboard contains stable `data-platform-*` hooks and initial aggregate values; the task page contains `已使用 X/Y`, availability status, and suggestion container; the admin user row contains current task use and limit input. Submit a valid quota update and assert persistence/audit; submit 0/101/non-numeric values and assert public validation errors without changing the stored limit.

- [ ] **Step 2: Verify the UI tests fail**

Run the named page tests and confirm missing hooks/routes are the cause.

- [ ] **Step 3: Render initial dashboard status and add polling**

Pass `platform_status` from `/dashboard`, replace the old “recent execution count” card with mutually exclusive today metrics and a worker status chip, and load `dashboard.js`. Poll every 15 seconds, update only text/classes, retain previous values on failure, and show `状态更新暂时失败` without clearing counts.

- [ ] **Step 4: Add task quota and slot feedback**

Pass `task_usage`/`task_limit` into `/tasks`. Update `tasks.js` to fetch availability after a valid time change, render `剩余 0/1` or `剩余 1/1`, and make suggestion buttons copy the recommended time into the input and recheck it. Keep submission enabled so the server remains the authority and can show a race-safe final error.

- [ ] **Step 5: Add administrator quota editing**

Render usage and limit beside each ordinary user, add the CSRF-protected route, call `TaskCapacityService.set_limit`, and redirect back with a fixed success notice. Do not show an editable limit for administrators.

- [ ] **Step 6: Style responsive cards and controls**

Extend existing tokens/classes in `app.css`; keep the current visual language, ensure five status cards wrap cleanly on narrow screens, keep buttons separated, and preserve readable focus/disabled states.

- [ ] **Step 7: Run all Web tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/console/test_web_user.py tests/console/test_registration_web.py tests/console/test_account_scan_web.py -q
```

Expected: all tests pass with no secret-marker leakage.

- [ ] **Step 8: Review checkpoint**

Open `/dashboard`, `/tasks`, and `/admin` locally at desktop and mobile widths; verify statistics refresh, slot suggestions work, and quota changes are understandable. Do not commit without explicit authorization.

---

### Task 6: Full verification and production deployment

**Files:**
- Modify only if verification exposes an in-scope defect.
- Deploy changed application files and container images under `/opt/douyin-spark-console`.

**Interfaces:**
- Consumes all previous tasks.
- Produces verified local and production behavior plus rollback artifacts.

- [ ] **Step 1: Run formatting/diff checks and the complete suite**

Run:

```powershell
git diff --check
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: zero diff errors and zero test failures.

- [ ] **Step 2: Review original requirements against evidence**

Confirm default 5-task quota, administrator override, two-minute capacity, three suggestions, retry avoidance, authenticated aggregate-only dashboard, Shanghai-day classification, 15-second refresh, and no mutation of existing tasks.

- [ ] **Step 3: Create production recovery points**

Before changing production, verify no active login scan, create a SQLite online backup, copy every replaced source file to a timestamped backup directory, and tag current Web/Worker/Auth images for rollback.

- [ ] **Step 4: Deploy only affected services**

Copy verified source files, build or incrementally patch the Web and Worker images that import changed modules, verify image file hashes/imports, then recreate only those services. Recreate Auth only if its imported task service changed in a way required by successful relogin retry scheduling.

- [ ] **Step 5: Verify production**

Check container image IDs, `RestartCount == 0`, Web readiness at `/health/ready`, Worker lease freshness, authenticated dashboard/API output, quota persistence, and a non-destructive availability lookup. Do not create a real message-sending task solely for verification.

- [ ] **Step 6: Clean temporary deployment artifacts and report**

Remove exact temporary upload/key files after validating their paths, retain timestamped backups, and report code/test/deployment evidence plus any behavior requiring user testing.
