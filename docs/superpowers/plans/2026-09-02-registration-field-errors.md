# Registration Field Errors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the ambiguous registration failure with safe, precise field errors and accessible live client validation.

**Architecture:** A small validation result type maps known domain failures to field codes and Chinese messages. The route preserves only non-secret submitted values and the template renders field-bound errors; browser JavaScript mirrors format rules but never validates invite state remotely.

**Tech Stack:** FastAPI, SQLAlchemy, Jinja2, vanilla JavaScript, unittest/pytest.

**Spec:** `docs/superpowers/specs/2026-09-02-warm-qr-scan-design.md`

## Global Constraints

- Never echo passwords or raw exception text.
- Invitation consumption stays atomic.
- Do not add an invite-probing endpoint.
- Preserve the existing per-IP failed-attempt limiter.
- Do not push to GitHub; deployment targets only the existing cloud server.

---

### Task 1: Server field-error contract

**Files:**
- Modify: `spark_console/web/registration_routes.py`
- Modify: `spark_console/services/users.py`
- Modify: `spark_console/services/invites.py`
- Test: `tests/console/test_registration_web.py`

**Interfaces:**
- Produces: template context `field_errors: dict[str, str]`, `form_values: dict[str, str]`.
- Produces: stable validation messages for username, password, confirmation, email, invite, and rate limit failures.

- [ ] **Step 1: Write failing route tests** for weak-password reasons, mismatched confirmation, username format/unavailability, invite invalid/expired/used/revoked, rate limiting, preserved non-secret values, and absent password values.
- [ ] **Step 2: Run** `python -m pytest tests/console/test_registration_web.py -q` and verify failures are caused by the current generic response.
- [ ] **Step 3: Implement minimal typed field mapping.** Validate local fields in deterministic order, convert only recognized domain errors, retain atomic invite consumption, and pass safe form values back to the template.
- [ ] **Step 4: Run** `python -m pytest tests/console/test_registration_web.py -q` and verify PASS.

### Task 2: Accessible live browser validation

**Files:**
- Create: `spark_console/static/register.js`
- Modify: `spark_console/templates/register.html`
- Modify: `spark_console/static/app.css`
- Create: `tests/console/register_js_harness.js`
- Modify: `tests/console/test_registration_web.py`

**Interfaces:**
- Consumes: field keys from Task 1.
- Produces: `aria-invalid`, `aria-describedby`, inline `.field-error`, and password-rule checklist state.

- [ ] **Step 1: Write failing HTML/JS tests** proving invalid username/password/confirmation are marked locally, corrected values clear errors, and no invite-check network request exists.
- [ ] **Step 2: Run** `python -m pytest tests/console/test_registration_web.py -q` and verify the markup/script assertions fail.
- [ ] **Step 3: Implement template, CSS, and JavaScript.** Validate on input/blur and submit, keep native email validation, and leave invitation state to form submission.
- [ ] **Step 4: Re-run** `python -m pytest tests/console/test_registration_web.py -q` and verify PASS.

### Task 3: Registration regression verification

**Files:**
- Test only: registration, email verification, and deployment contract suites.

- [ ] **Step 1: Run** `python -m pytest tests/console/test_registration_web.py tests/console/test_email_verification.py tests/console/test_deployment_contract.py -q`.
- [ ] **Step 2: Run** `git diff --check` and inspect only files touched by this plan for secret leakage and unrelated changes.
