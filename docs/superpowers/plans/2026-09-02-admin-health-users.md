# Admin Health and User Directory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a secure administrator health center, split user management into an eight-per-page directory, integrate email operations into the admin overview, and remove the redundant recent-runs block.

**Architecture:** A root-owned host collector writes an allowlisted atomic JSON snapshot; the Web container reads it through a read-only bind mount. Existing FastAPI administrator dependencies and pagination helpers serve separate health and user pages while `/admin` remains an operations overview.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Jinja2, Docker Compose, systemd, vnStat, unittest.

**Spec:** `docs/superpowers/specs/2026-09-02-admin-health-users-design.md`

## Global Constraints

- Do not expose the Docker Socket or a host command execution endpoint to Web.
- All new routes are administrator-only.
- User directory page size is exactly 8.
- Health history is bounded to seven days at five-minute resolution.
- Do not commit, push, or touch BPS data and containers.

---

### Task 1: Host health snapshot contract

**Files:**
- Create: `spark_console/services/system_health.py`
- Create: `ops/spark_health_collector.py`
- Test: `tests/console/test_system_health.py`

**Interfaces:**
- Produces: `load_health_snapshot(path, now) -> HealthSnapshotView` and a collector JSON document with `schema_version`, `collected_at`, `resources`, `traffic`, `services`, `history`.

- [ ] Write tests for valid, missing and stale snapshots and for bounded, atomic collector history.
- [ ] Run the tests and confirm failures are caused by missing modules.
- [ ] Implement strict allowlisted parsing, severity derivation and collector output.
- [ ] Run the tests and confirm they pass.

### Task 2: Administrator health page

**Files:**
- Create: `spark_console/templates/admin_health.html`
- Modify: `spark_console/web/app.py`
- Modify: `spark_console/templates/base.html`
- Modify: `spark_console/static/app.css`
- Test: `tests/console/test_admin_health_page.py`

**Interfaces:**
- Consumes: `load_health_snapshot` from Task 1 and existing worker/account/task/email aggregation.
- Produces: `GET /admin/health` and an active administrator navigation link.

- [ ] Write route tests for admin authorization, valid snapshot rendering and stale fallback.
- [ ] Run tests and confirm the route is absent.
- [ ] Add route context, responsive template, pulse strip and resource cards.
- [ ] Run tests and confirm they pass.

### Task 3: Eight-per-page user directory

**Files:**
- Create: `spark_console/templates/admin_users.html`
- Modify: `spark_console/web/app.py`
- Modify: `spark_console/templates/admin.html`
- Modify: `spark_console/templates/base.html`
- Modify: `spark_console/static/app.css`
- Test: `tests/console/test_admin_user_directory.py`

**Interfaces:**
- Produces: `GET /admin/users?page=N&q=...` while retaining existing POST `/admin/users` creation and user action routes.

- [x] Write tests proving ordinary users get 404 and administrators see exactly 8 users on page 1.
- [ ] Run tests and confirm the GET route or template is missing.
- [ ] Move the user list and creation form into the new route/template and preserve quota actions.
- [ ] Run tests and confirm pagination and actions pass.

### Task 4: Admin overview simplification and email operations

**Files:**
- Modify: `spark_console/web/app.py`
- Modify: `spark_console/templates/admin.html`
- Test: `tests/console/test_registration_web.py`

**Interfaces:**
- Consumes: aggregate notification counts from `NotificationEvent`.
- Produces: an email operations module on `/admin` and no recent-run cards.

- [ ] Write a failing test for the new module and removal of duplicated user/recent-run sections.
- [ ] Remove unused context queries and add pending/failed/sent email aggregates.
- [ ] Run affected admin tests and update only assertions whose intentional page location changed.

### Task 5: Deployment contract and production installation

**Files:**
- Modify: `compose.console.yml`
- Create: `ops/systemd/spark-health-collector.service`
- Create: `ops/systemd/spark-health-collector.timer`
- Modify: `tests/console/test_deployment_contract.py`

**Interfaces:**
- Produces: read-only `/run/spark-health` mount and a minute systemd timer writing `runtime/host-health.json`.

- [ ] Write a failing deployment-contract test proving the mount is read-only and no Docker Socket is exposed.
- [ ] Add the runtime mount and hardened systemd units.
- [ ] Run deployment tests, compile checks, diff checks and the full unittest suite.
- [ ] Back up production source and image, install vnStat and units, deploy only changed Spark services, then verify health endpoints and all existing BPS containers.
