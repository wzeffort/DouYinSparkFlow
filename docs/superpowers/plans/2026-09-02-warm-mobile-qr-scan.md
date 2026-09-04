# Warm Mobile QR Scan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make normal QR binding fast with a one-use warm browser context while showing a clear QR crop on mobile and the full remote page on desktop.

**Architecture:** The auth worker owns one reusable Playwright/Chromium runtime and one unassigned anonymous prepared context. Assignment consumes the context exactly once; every terminal path destroys it and prepares a replacement. Scan persistence stores separate full-view and QR-crop PNGs, both owner-authorized.

**Tech Stack:** asyncio, Playwright, FastAPI, SQLAlchemy/SQLite, Jinja2, vanilla JavaScript, unittest/pytest, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-02-warm-qr-scan-design.md`

## Global Constraints

- Keep exactly one global active binding session.
- Never reuse a browser context or cookies across owners.
- Never persist an unassigned QR image or browser state.
- Desktop uses the full screenshot; mobile uses the QR crop until confirming.
- Every image endpoint is owner-authorized and `Cache-Control: no-store`.
- Do not push to GitHub; deploy only after full verification.

---

### Task 1: Persist and authorize a QR crop

**Files:**
- Modify: `spark_console/models.py`
- Modify: `spark_console/db.py`
- Modify: `spark_console/services/scan_sessions.py`
- Modify: `spark_console/web/account_scan_routes.py`
- Test: `tests/console/test_config_db.py`
- Test: `tests/console/test_scan_sessions.py`
- Test: `tests/console/test_account_scan_web.py`

**Interfaces:**
- Produces: `DouyinLoginSession.qr_crop_png: bytes | None`.
- Produces: `ScanSessionService.publish_qr(scan_id, full_png, qr_crop_png)`.
- Produces: `GET /accounts/scan/{scan_id}/qr-crop`.

- [ ] **Step 1: Write failing model/service/route tests** for migration, PNG validation, owner isolation, no-store headers, and unavailable crops.
- [ ] **Step 2: Run focused tests** and verify failures reflect the missing column/API.
- [ ] **Step 3: Add the nullable column and idempotent SQLite migration, update publish logic, and add the authorized endpoint.**
- [ ] **Step 4: Re-run focused tests** and verify PASS.

### Task 2: Produce separate full and cropped images

**Files:**
- Modify: `spark_console/auth_scanner.py`
- Modify: `spark_console/auth_worker.py`
- Test: `tests/console/test_auth_scanner.py`
- Test: `tests/console/test_auth_worker.py`

**Interfaces:**
- Produces: QR-ready callback accepting `(full_png: bytes, qr_crop_png: bytes)`.
- Preserves: full-page screenshot coordinate space for forwarded clicks.

- [ ] **Step 1: Write failing scanner and worker tests** proving the QR locator screenshot becomes the crop while the page screenshot remains the remote view.
- [ ] **Step 2: Run focused tests** and verify the old one-image callback fails them.
- [ ] **Step 3: Capture both screenshots, validate PNG data, and publish them together.**
- [ ] **Step 4: Re-run focused tests** and verify PASS.

### Task 3: One-use warm browser lifecycle

**Files:**
- Modify: `spark_console/auth_scanner.py`
- Modify: `spark_console/auth_worker.py`
- Modify: `spark_console/config.py`
- Modify: `.env.console.example`
- Modify: `compose.console.yml`
- Test: `tests/console/test_auth_scanner.py`
- Test: `tests/console/test_auth_worker.py`
- Test: `tests/console/test_config_db.py`

**Interfaces:**
- Produces: scanner lifecycle `start()`, `prepare()`, `run_prepared(...)`, `close()` with cold fallback.
- Produces: bounded warm age and restart backoff settings.

- [ ] **Step 1: Write failing lifecycle tests** for one-time consume, context destruction on every terminal outcome, stale replacement, browser crash recreation, and cold fallback.
- [ ] **Step 2: Run focused tests** and verify failures are caused by absent lifecycle methods.
- [ ] **Step 3: Implement the minimum lifecycle.** Keep Playwright/browser alive, keep unassigned context only in worker memory, close assigned context in `finally`, and close all resources on shutdown.
- [ ] **Step 4: Update the auth loop** to prepare while idle, claim at one-second cadence, emit stage-duration logs, and shut down cleanly.
- [ ] **Step 5: Re-run focused tests** and verify PASS with no pending asyncio-task warnings.

### Task 4: Responsive scan experience

**Files:**
- Modify: `spark_console/templates/accounts.html`
- Modify: `spark_console/static/account_scan.js`
- Modify: `spark_console/static/app.css`
- Modify: `tests/console/account_scan_js_harness.js`
- Modify: `tests/console/test_account_scan_js.py`
- Modify: `tests/console/test_account_scan_web.py`

**Interfaces:**
- Consumes: `/qr` full view and `/qr-crop` mobile image.
- Produces: CSS-driven desktop/mobile presentation and QR-to-full-view transition at `confirming`.

- [ ] **Step 1: Write failing browser tests** for no page-load scan creation, desktop full view, mobile crop, image-open/save affordance, and confirming transition.
- [ ] **Step 2: Run focused tests** and verify expected failures.
- [ ] **Step 3: Remove real session preload and implement responsive image behavior.** Keep click forwarding bound only to the full screenshot.
- [ ] **Step 4: Add mobile guidance and accessible controls, then re-run focused tests** and verify PASS.

### Task 5: Full verification and production deployment

**Files:**
- Deployment only; preserve production database, key files, and environment secrets.

- [ ] **Step 1: Run** `python -m pytest tests/console -q`.
- [ ] **Step 2: Run** `git diff --check`, inspect the scoped diff, and scan tracked/untracked source for credential material.
- [ ] **Step 3: Create a server-side rollback snapshot, sync only intended application files, rebuild affected containers, and keep GitHub untouched.**
- [ ] **Step 4: Verify** container health, `/login`, `/register`, authenticated scan endpoints, and structured auth timing logs.
- [ ] **Step 5: Report measured warm-path latency and any remaining Douyin-side variance without claiming an unmeasured target.**
