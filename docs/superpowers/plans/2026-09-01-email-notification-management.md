# DouYinSparkFlow Email and Notification Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Add verified email registration, existing-user email binding, deduplicated in-app and email alerts for expired Douyin credentials, and an independently retryable Resend notification worker.

**Architecture:** Business transactions write encrypted email state, in-app notices, and unique notification Outbox rows into SQLite. A single spark-notifier process claims due events, calls Resend outside database transactions, and persists provider-safe results; Web, Auth, and the Douyin Worker never send email directly.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2, SQLite WAL, Jinja2, httpx, cryptography, Docker Compose, Resend HTTP API, unittest.

**Spec:** docs/superpowers/specs/2026-09-01-email-notification-management-design.md

## Global Constraints

- Existing users remain usable without email; new invited registrations require verified email.
- Six-digit verification codes expire after 10 minutes, allow 5 wrong attempts, and have a 60-second resend cooldown.
- Limits: 5 sends/email/hour, 10/email/day, 20/IP/hour, 50/IP/day.
- Only cookie_invalid and login_expired open an authentication incident.
- One authentication incident creates at most one in-app notice and one email event.
- Notification retry delays are 1, 5, 20, and 60 minutes.
- Notification lists show 6 rows per page and pagination retains a page anchor.
- RESEND_API_KEY exists only in Git-ignored .env.console and must not enter tracked files, images, database rows, logs, URLs, tests, or responses.
- Email encryption/HMAC uses an independent 32-byte PII key mounted at /run/secrets/pii.key.
- Existing Cookie, invite, quota, task, BPS, and port-8888 behavior remains unchanged.
- No commit or push is authorized; each task ends with a diff/review checkpoint.

---

## File Map

New production files:

- spark_console/pii.py — normalization, encryption, lookup HMAC, masking, code hashing.
- spark_console/services/email_verification.py — pending registration and bind/change verification.
- spark_console/services/notifications.py — in-app notices and Outbox state machine.
- spark_console/resend.py — narrow sanitized Resend client.
- spark_console/notifier.py — Outbox polling and recovery.
- spark_console/web/email_routes.py — settings and notification routes.
- spark_console/templates/register_verify.html
- spark_console/templates/email_settings.html
- spark_console/templates/notifications.html

Existing production files:

- spark_console/config.py, models.py, db.py
- spark_console/web/registration_routes.py, web/app.py
- spark_console/services/accounts.py, worker.py
- register.html, base.html, dashboard.html, accounts.html, tasks.html, admin.html
- spark_console/static/app.css
- .env.console.example, .gitignore, compose.console.yml, docs/console-operations.md

Tests:

- tests/console/test_pii.py
- tests/console/test_email_verification.py
- tests/console/test_notifications.py
- tests/console/test_notifier.py
- existing config, registration, Web, service, Worker, deployment, and secret-regression tests

---

### Task 1: PII Configuration and Cryptography

**Files:**
- Create: spark_console/pii.py
- Modify: spark_console/config.py
- Create: tests/console/test_pii.py
- Modify: tests/console/test_config_db.py

**Interfaces:**
- normalize_email(value: str) -> str
- mask_email(value: str) -> str
- PiiCipher.encrypt_email(email: str, *, aad: bytes) -> tuple[bytes, bytes]
- PiiCipher.decrypt_email(ciphertext: bytes, nonce: bytes, *, aad: bytes) -> str
- PiiCipher.lookup_hash(email: str) -> str
- PiiCipher.code_hash(scope: str, code: str) -> str
- PiiCipher.verify_code(scope: str, code: str, digest: str) -> bool
- Settings adds pii_key_file, public_base_url, email_enabled, email_poll_seconds, resend_api_key, resend_from.

- [ ] **Step 1: Write failing tests**

~~~python
class PiiCipherTests(unittest.TestCase):
    def setUp(self):
        self.pii = PiiCipher(b"p" * 32)

    def test_email_is_normalized_encrypted_masked_and_aad_bound(self):
        self.assertEqual("2010039681@qq.com", normalize_email(" 2010039681@QQ.COM "))
        ciphertext, nonce = self.pii.encrypt_email(
            "2010039681@qq.com", aad=b"user:1"
        )
        self.assertNotIn(b"2010039681", ciphertext)
        self.assertEqual(
            "2010039681@qq.com",
            self.pii.decrypt_email(ciphertext, nonce, aad=b"user:1"),
        )
        with self.assertRaises(InvalidTag):
            self.pii.decrypt_email(ciphertext, nonce, aad=b"user:2")
        self.assertEqual("20******81@qq.com", mask_email("2010039681@qq.com"))
~~~

Add settings tests proving a missing/short PII key fails when email is enabled, an HTTPS public base URL is required, and repr(settings) omits the API key.

- [ ] **Step 2: Run RED**

Run:

~~~powershell
.\.venv\Scripts\python.exe -m unittest tests.console.test_pii tests.console.test_config_db.SettingsTests -v
~~~

Expected: missing module and settings attributes.

- [ ] **Step 3: Implement minimal production code**

~~~python
EMAIL_PATTERN = re.compile(r"^[^\s@\x00-\x1f\x7f]+@[^\s@\x00-\x1f\x7f]+$")

def normalize_email(value: str) -> str:
    clean = value.strip().lower()
    if not (3 <= len(clean) <= 254) or not EMAIL_PATTERN.fullmatch(clean):
        raise ValueError("invalid email")
    local, domain = clean.rsplit("@", 1)
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("invalid email")
    return clean

class PiiCipher:
    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("PII key must be exactly 32 bytes")
        self._key = key
        self._aead = AESGCM(key)

    def lookup_hash(self, email: str) -> str:
        return hmac.new(
            self._key,
            b"email-lookup\0" + normalize_email(email).encode(),
            hashlib.sha256,
        ).hexdigest()
~~~

Use AESGCM with a random 12-byte nonce and AAD. Use HMAC-SHA256 with separate prefixes for email lookup and verification code. Mark resend_api_key with dataclasses.field(repr=False).

- [ ] **Step 4: Run GREEN**

Run the Step 2 command. Expected: selected tests pass.

- [ ] **Step 5: Review checkpoint**

Run git diff --check on the four task files and scan the diff for the exact API key. Do not commit.

---

### Task 2: Additive Schema and Idempotent Migration

**Files:**
- Modify: spark_console/models.py
- Modify: spark_console/db.py
- Modify: tests/console/test_config_db.py

**Interfaces:**
- Models/tables: PendingRegistration (pending_registrations), EmailVerificationRequest (email_verification_requests), NotificationPreference (notification_preferences), UserNotification (user_notifications), NotificationEvent (notification_events), EmailActionToken (email_action_tokens), AppSetting (app_settings).
- User adds nullable email_ciphertext, email_nonce, email_lookup_hash, email_verified_at, email_updated_at.
- DouyinAccount adds invalidated_at, invalid_reason_code, auth_incident_id.
- run_additive_migrations(engine: Engine) -> None.

- [ ] **Step 1: Write failing legacy migration tests**

Build a pre-feature SQLite users/douyin_accounts schema, call create_schema twice, and assert all new columns, the seven named tables, and their indexes exist. Insert duplicate non-null email_lookup_hash values and expect IntegrityError. Confirm existing users/tasks survive unchanged. Assert EmailActionToken stores token_hash but has no plaintext token column.

- [ ] **Step 2: Run RED**

~~~powershell
.\.venv\Scripts\python.exe -m unittest tests.console.test_config_db.DatabaseTests -v
~~~

Expected: missing columns and tables.

- [ ] **Step 3: Implement models and allow-listed migration**

~~~python
SQLITE_COLUMNS = {
    "users": {
        "email_ciphertext": "BLOB",
        "email_nonce": "BLOB",
        "email_lookup_hash": "VARCHAR(64)",
        "email_verified_at": "DATETIME",
        "email_updated_at": "DATETIME",
    },
    "douyin_accounts": {
        "invalidated_at": "DATETIME",
        "invalid_reason_code": "VARCHAR(48)",
        "auth_incident_id": "VARCHAR(36)",
    },
}

def run_additive_migrations(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        raise RuntimeError("email migration currently supports SQLite only")
    with engine.begin() as connection:
        for table, declarations in SQLITE_COLUMNS.items():
            existing = {
                row[1]
                for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            for column, declaration in declarations.items():
                if column not in existing:
                    connection.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                    )
~~~

Only constant table/column names may be interpolated. Base.metadata.create_all creates new tables. Add partial unique indexes for non-null email_lookup_hash and unique dedupe keys. Seed AppSetting(email_paused=false).

- [ ] **Step 4: Run GREEN**

Run the Step 2 command twice. Expected: both runs pass.

- [ ] **Step 5: Review checkpoint**

Inspect every SQL literal and git diff --check. Do not commit.

---

### Task 3: Outbox Foundation Before Registration

**Files:**
- Create: spark_console/services/notifications.py
- Create: tests/console/test_notifications.py

**Interfaces:**
- create_in_app(user_id, kind, title, summary, action_path, dedupe_key)
- enqueue_template(user_id, kind, recipient, template_key, payload, dedupe_key)
- claim_due(worker_id, now)
- mark_sent(event_id, provider_id, now)
- mark_failed(event_id, error_code, retryable, now)
- recover_stale(now)
- set_paused(actor_id, paused)
- create_action_token(user_id, incident_id, now) -> str
- consume_action_token(user_id, plaintext_token, now) -> EmailActionToken

- [ ] **Step 1: Write failing Outbox tests**

~~~python
def test_duplicate_dedupe_key_creates_one_event(self):
    first = service.enqueue_template(
        user.id, "email_verification", email, "verify", payload, "verify:abc"
    )
    second = service.enqueue_template(
        user.id, "email_verification", email, "verify", payload, "verify:abc"
    )
    self.assertEqual(first.id, second.id)
    self.assertEqual(1, count_events())

def test_retry_delays_are_exact(self):
    self.assertEqual(
        [1, 5, 20, 60],
        [int(delay.total_seconds() / 60) for delay in service.RETRY_DELAYS],
    )
~~~

Add tests for pause, claim ownership, stale recovery, sent immutability, failed-only manual retry, relative action paths, and sanitized errors. Add action-token tests proving only a hash is stored, the token expires after 30 minutes, it is single-use, and another user cannot consume it.

- [ ] **Step 2: Run RED**

~~~powershell
.\.venv\Scripts\python.exe -m unittest tests.console.test_notifications -v
~~~

Expected: missing service.

- [ ] **Step 3: Implement state machine**

~~~python
ALLOWED = {
    "pending": {"sending", "cancelled"},
    "sending": {"sent", "failed", "pending"},
    "failed": {"pending", "cancelled"},
    "sent": set(),
    "cancelled": set(),
}
RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=20),
    timedelta(minutes=60),
)
~~~

Claims use short transactions and set worker_id/claimed_at. Unique dedupe conflicts re-query and return the existing event. Payload JSON is allow-listed per template. Action tokens use secrets.token_urlsafe(32), HMAC hashing through PiiCipher, and constant-time comparison; only the plaintext returned by create_action_token is placed into the one email action URL.

- [ ] **Step 4: Run GREEN**

Run the Step 2 command. Expected: all Outbox tests pass.

- [ ] **Step 5: Review checkpoint**

Scan model/service serialization for Cookie, full provider responses, API keys, and arbitrary action URLs. Do not commit.

---

### Task 4: Pending Registration and Email Verification

**Files:**
- Create: spark_console/services/email_verification.py
- Modify: spark_console/web/registration_routes.py
- Modify: spark_console/templates/register.html
- Create: spark_console/templates/register_verify.html
- Create: tests/console/test_email_verification.py
- Modify: tests/console/test_registration_web.py

**Interfaces:**
- start_registration(username, password, email, invite_code, client_key, now) -> PendingRegistration
- verify_registration(pending_id, code, now) -> User
- resend_registration(pending_id, client_key, now) -> PendingRegistration
- cleanup_expired(now) -> int
- Routes: POST /register, GET/POST /register/verify/{id}, POST /register/verify/{id}/resend.

- [ ] **Step 1: Write failing service tests**

~~~python
def test_invite_is_consumed_only_after_correct_code(self):
    pending = service.start_registration(
        "alice", "StrongPass123", "alice@example.com", invite_code, "198.51.100.2", now
    )
    self.assertIsNone(invite.used_at)
    with self.assertRaises(ValidationError):
        service.verify_registration(pending.id, "000000", now)
    user = service.verify_registration(pending.id, delivered_code, now)
    self.assertEqual("alice", user.username)
    self.assertIsNotNone(invite.used_at)
    self.assertIsNotNone(user.email_verified_at)
~~~

Add tests for 10-minute expiry, 5 attempts, 60-second cooldown, resend invalidating the old code, uniqueness races, and all four rate limits.

- [ ] **Step 2: Run RED**

~~~powershell
.\.venv\Scripts\python.exe -m unittest tests.console.test_email_verification -v
~~~

Expected: missing service.

- [ ] **Step 3: Implement pending flow**

Generate with f"{secrets.randbelow(1_000_000):06d}". Store only code_hash. Pass plaintext code directly into the verification-email Outbox payload builder, never to a log or database field. Re-check username/email/invite and consume the invite in the same successful verification transaction.

- [ ] **Step 4: Write failing Web tests**

~~~python
def test_register_redirects_to_verification_without_creating_user(self):
    response = client.post(
        "/register",
        data={
            "username": "alice",
            "password": "StrongPass123",
            "password_confirmation": "StrongPass123",
            "email": "alice@example.com",
            "invite_code": invite_code,
        },
        follow_redirects=False,
    )
    self.assertEqual(303, response.status_code)
    self.assertRegex(response.headers["location"], r"^/register/verify/[0-9a-f-]+$")
    self.assertIsNone(find_user("alice"))
~~~

Assert the page receives only masked_email, expiry, resend time, and remaining attempts. Resend requires CSRF. Public error remains 注册信息或验证码无效.

- [ ] **Step 5: Run Web RED**

Run tests.console.test_registration_web. Expected: old immediate-registration behavior conflicts with new contract.

- [ ] **Step 6: Implement routes/templates**

State-changing success uses 303. Invalid/expired code returns 400, rate limits 429. Verification never creates a login session; redirect to /login?registered=1.

- [ ] **Step 7: Run GREEN**

Run email-verification and registration-Web modules. Expected: all pass.

- [ ] **Step 8: Review checkpoint**

Render HTML and scan for full email, code, invite plaintext, ciphertext, and nonce. Do not commit.

---

### Task 5: Existing-User Email Binding and Preferences

**Files:**
- Create: spark_console/web/email_routes.py
- Extend: spark_console/services/email_verification.py
- Create: spark_console/templates/email_settings.html
- Modify: spark_console/web/app.py
- Modify: spark_console/templates/base.html
- Modify: spark_console/templates/dashboard.html
- Modify: spark_console/static/app.css
- Modify: tests/console/test_web_user.py
- Extend: tests/console/test_email_verification.py

**Interfaces:**
- start_binding(user, email, now)
- verify_binding(user, request_id, code, now)
- resend_binding(user, request_id, now)
- email_projection(user) -> dict
- update_preferences(user_id, values)
- Routes under /settings/email.

NotificationPreference contains douyin_login_expired_email (default true), task_repeated_failure_email, quota_expiring_email, and quota_expired_email. The final three are persisted and editable but have no first-phase event producer.

- [ ] **Step 1: Write failing tests**

~~~python
def test_old_email_remains_until_new_email_is_verified(self):
    old_hash = user.email_lookup_hash
    request = service.start_binding(user, "new@example.com", now)
    self.assertEqual(old_hash, user.email_lookup_hash)
    service.verify_binding(user, request.id, delivered_code, now)
    self.assertNotEqual(old_hash, user.email_lookup_hash)
~~~

Add tests for current password on change, duplicate email, cross-user hiding, default preferences, CSRF, and masked projection.

- [ ] **Step 2: Run RED**

Run email-verification and user-Web modules. Expected: missing routes/methods.

- [ ] **Step 3: Implement binding service/routes**

New email verification atomically replaces email fields. Old email remains until success. Owner-scoped request queries return 404 for another user. Current password is required only when replacing a verified email.

- [ ] **Step 4: Implement safe UI**

Templates receive only state, masked_email, verified_at, cooldown, and preference booleans. No database secret fields enter context.

- [ ] **Step 5: Run GREEN**

Run the Step 2 modules. Expected: all pass.

- [ ] **Step 6: Review checkpoint**

Scan rendered HTML for complete test emails. Only masked values may appear. Do not commit.

---

### Task 6: Resend Transport and Notifier Process

**Files:**
- Create: spark_console/resend.py
- Create: spark_console/notifier.py
- Create: tests/console/test_notifier.py

**Interfaces:**
- ProviderResult(success, provider_id, error_code, retryable)
- ResendTransport.send(event_id, to, subject, html) -> ProviderResult
- Notifier.run_once(now=None) -> NotificationEvent | None

- [ ] **Step 1: Write failing transport tests**

Use httpx.MockTransport. Assert Authorization Bearer is supplied, Idempotency-Key equals event ID, sender matches Settings, 429/5xx/timeouts are retryable, 400/403 are permanent, and ProviderResult/logs contain no key or response body.

- [ ] **Step 2: Run RED**

~~~powershell
.\.venv\Scripts\python.exe -m unittest tests.console.test_notifier -v
~~~

Expected: missing modules.

- [ ] **Step 3: Implement transport**

~~~python
response = self.client.post(
    "https://api.resend.com/emails",
    headers={
        "Authorization": f"Bearer {self.api_key}",
        "Idempotency-Key": event_id,
    },
    json={"from": self.sender, "to": [to], "subject": subject, "html": html},
)
~~~

Only extract provider ID on success and stable provider error names on failure.

- [ ] **Step 4: Implement notifier**

Recover stale sending rows at startup. Claim/commit, decrypt recipient, call network outside transaction, wipe temporary buffers, then mark sent/failed in a new transaction. Sleep only when no event was claimed. Handle SIGTERM/SIGINT. Once per UTC day call cleanup_expired to remove expired pending registrations, consumed/expired verification requests, expired/consumed action tokens, and old template payload bodies while retaining delivery audit fields.

- [ ] **Step 5: Run GREEN**

Run notification and notifier modules. Expected: all pass without real network.

- [ ] **Step 6: Review checkpoint**

Search test logs and diffs for the API key, full email, verification code, and provider response bodies. Do not commit.

---

### Task 7: Douyin Authentication Incident Integration

**Files:**
- Modify: spark_console/worker.py
- Modify: spark_console/services/accounts.py
- Modify: tests/console/test_scheduler_worker.py
- Modify: tests/console/test_services.py
- Modify: tests/console/test_auth_worker.py

**Interfaces:**
- NotificationService.open_auth_incident(account, now)
- NotificationService.resolve_auth_incident(account, now)

- [ ] **Step 1: Write failing Worker tests**

~~~python
def test_expiry_opens_one_incident_pauses_tasks_and_enqueues_once(self):
    worker.run_once(now)
    worker.run_once(now + timedelta(minutes=1))
    account = load_account()
    self.assertIsNotNone(account.auth_incident_id)
    self.assertEqual("invalid", account.validation_state)
    self.assertFalse(load_task().enabled)
    self.assertEqual(1, count_events("douyin_login_expired"))
    self.assertEqual(1, count_notices("douyin_login_expired"))
~~~

Add table-driven tests proving network_unavailable, conversation_not_opened, target_not_found, and execution_timeout create no incident.

- [ ] **Step 2: Run RED**

Run WorkerCredentialTests. Expected: incident/event assertions fail.

- [ ] **Step 3: Implement one-transaction opening**

On transition into invalid, generate incident ID, set reason/time, disable all enabled tasks for the account, create an in-app notice, and enqueue email only if verified/preferred. Repeated failures reuse the incident and do nothing.

- [ ] **Step 4: Write rebind resolution tests**

Extend successful re-login tests: incident fields clear, notice resolves, sent history remains, tasks remain paused for user review.

- [ ] **Step 5: Implement resolution and run GREEN**

Call resolve_auth_incident from the existing successful account rebind transaction. Run scheduler-worker, services, and auth-worker modules. Expected: all pass.

- [ ] **Step 6: Review checkpoint**

Confirm no task auto-enables and no transient error opens an incident. Do not commit.

---

### Task 8: User Notification Center

**Files:**
- Extend: spark_console/web/email_routes.py
- Create: spark_console/templates/notifications.html
- Modify: base.html, dashboard.html, accounts.html, tasks.html, app.css
- Modify: tests/console/test_web_user.py

**Interfaces:**
- GET /notifications
- POST /notifications/{id}/read
- POST /notifications/read-all
- GET /email-actions/{token}; authentication and owner match are required before consuming the single-use token and redirecting to /accounts.

- [ ] **Step 1: Write failing Web tests**

~~~python
response = client.get("/notifications?page=2#notification-list")
self.assertEqual(200, response.status_code)
self.assertEqual(6, response.text.count('class="notification-card"'))
self.assertIn('href="/notifications?page=1#notification-list"', response.text)
self.assertNotIn(other_user_title, response.text)
~~~

Add tests for unread badge, owner-only read actions, fixed expired-account banners, safe relative paths, expired action tokens, cross-user action tokens, and successful one-time redirect to /accounts.

- [ ] **Step 2: Run RED**

Run test_web_user. Expected: routes/templates missing.

- [ ] **Step 3: Implement owner-scoped routes/templates**

Every query filters UserNotification.user_id == user.id. Action path must begin with one slash and not two. Use _page_info(..., 6). Pagination links end in #notification-list.

- [ ] **Step 4: Add consistent expiry banners**

Use fixed copy: 抖音账号登录已失效，相关任务已暂停，请重新绑定后检查并恢复任务。 Action links point only to /accounts.

- [ ] **Step 5: Run GREEN and review**

Run test_web_user. Render desktop/mobile pages, check focus/contrast and anchor retention. Do not commit.

---

### Task 9: Administrator Notification Operations

**Files:**
- Modify: spark_console/web/app.py
- Modify: spark_console/templates/admin.html
- Modify: spark_console/static/app.css
- Modify: tests/console/test_registration_web.py

**Interfaces:**
- Query keys: notification_page, notification_q, notification_status, notification_kind.
- POST /admin/notifications/{id}/retry
- POST /admin/email/pause
- POST /admin/email/resume

- [ ] **Step 1: Write failing admin tests**

Create 7+ events and assert 6-per-page, filters, anchors, masked emails, failed-only retry, sent retry rejection, CSRF, pause/resume audit, and aggregate notifier health.

- [ ] **Step 2: Run RED**

Run test_registration_web. Expected: controls/context missing.

- [ ] **Step 3: Implement safe projections/actions**

Display only masked email, fixed type/status labels, provider ID, timestamps, safe error code and fixed summary. Manual retry changes failed to pending and next_attempt_at=now; it never changes sent. Pause/resume updates AppSetting and audit.

- [ ] **Step 4: Run GREEN**

Run test_registration_web. Expected: all pass.

- [ ] **Step 5: Review checkpoint**

Scan admin HTML for full emails, ciphertext, nonce, code, token, API-key prefix, and provider body. All absent. Do not commit.

---

### Task 10: Compose and Secret Contract

**Files:**
- Modify: .env.console.example
- Modify: .gitignore
- Modify: compose.console.yml
- Modify: docs/console-operations.md
- Modify: tests/console/test_deployment_contract.py
- Modify: tests/console/test_secret_regression.py

**Interfaces:**
- spark-notifier command: [python, -m, spark_console.notifier]
- PII mount: ./secrets/pii.key:/run/secrets/pii.key:ro

- [ ] **Step 1: Write failing deployment tests**

~~~python
def test_notifier_is_private_and_limited(self):
    notifier = self.compose.split("  spark-notifier:", 1)[1].split("\nvolumes:", 1)[0]
    self.assertIn("command: [python, -m, spark_console.notifier]", notifier)
    self.assertNotIn("ports:", notifier)
    self.assertIn("mem_limit: 192m", notifier)
    self.assertIn("cpus: 0.25", notifier)

def test_examples_never_contain_real_resend_key(self):
    self.assertIn("RESEND_API_KEY=", self.example.splitlines())
    self.assertNotRegex(self.example, r"RESEND_API_KEY=re_[A-Za-z0-9_-]+")
~~~

- [ ] **Step 2: Run RED**

Run deployment-contract and secret-regression modules. Expected: missing notifier/PII declarations.

- [ ] **Step 3: Implement Compose/docs**

~~~yaml
  spark-notifier:
    <<: *common
    command: [python, -m, spark_console.notifier]
    mem_limit: 192m
    cpus: 0.25
    tmpfs: ["/tmp:rw,noexec,nosuid,size=32m"]
~~~

Add the PII mount to common volumes. Example variables: SPARK_PII_KEY_FILE, SPARK_PUBLIC_BASE_URL, SPARK_EMAIL_ENABLED, SPARK_EMAIL_POLL_SECONDS, RESEND_API_KEY, RESEND_FROM. Document backup, key generation, disabled-first deployment, notifier-only stop/start, and rollback.

- [ ] **Step 4: Run GREEN**

~~~powershell
.\.venv\Scripts\python.exe -m unittest tests.console.test_deployment_contract tests.console.test_secret_regression -v
docker compose --env-file .env.console -f compose.console.yml config --quiet
~~~

Expected: tests pass and Compose exits 0 without printing secrets.

- [ ] **Step 5: Review checkpoint**

Compare the exact secret from the private setup document against git diff; result false. Confirm .env.console, pii.key, databases, and backups are ignored. Do not commit.

---

### Task 11: Full Verification and Production Deployment

**Files/targets:**
- All task-owned files above.
- Server: /opt/douyin-spark-console.
- Private server files: .env.console, secrets/pii.key, existing Cookie/session keys, SQLite volume.

- [ ] **Step 1: Full local verification**

~~~powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
git diff --check
git check-ignore .env.console secrets/pii.key
docker compose --env-file .env.console -f compose.console.yml config --quiet
docker build -f Dockerfile.console -t douyin-spark-console:email-notifications-test .
~~~

Expected: zero failures, ignored secrets, valid Compose, successful build. Record test count.

- [ ] **Step 2: Image and diff secret gate**

Search tracked diff and image history/layers for the exact Resend key and PII-key digest. Both searches must return false.

- [ ] **Step 3: Server preflight**

Verify pinned SSH identity, exact /opt target, root disk, memory/swap, Spark/BPS containers, Web health, active runs, and active login scans. Stop when task/login is active, BPS changed, or rollback assets cannot fit.

- [ ] **Step 4: Backup**

Run the existing database backup CLI. Copy .env.console with mode 0600. Tag every current Spark image with a timestamped pre-email-notifications rollback tag. Record container IDs and restart counts.

- [ ] **Step 5: Create PII key without disclosure**

~~~bash
sudo install -d -m 0750 /opt/douyin-spark-console/secrets
umask 077
head -c 32 /dev/urandom | sudo tee /opt/douyin-spark-console/secrets/pii.key >/dev/null
sudo chmod 0600 /opt/douyin-spark-console/secrets/pii.key
sudo stat -c %s /opt/douyin-spark-console/secrets/pii.key
~~~

Expected size: 32.

- [ ] **Step 6: Deploy disabled-first**

Set SPARK_EMAIL_ENABLED=false, deploy source/images, run backup and migration, then start Web/Auth/Worker/Notifier. Verify existing user/task/invite/quota/account counts.

- [ ] **Step 7: Validate without delivery**

Check ready health, login, old-user dashboard, admin email state, pending cleanup, notifier queue health, and restart counts. No event may be delivered while disabled.

- [ ] **Step 8: Enable and send one authorized test**

Enable email, recreate affected containers, enqueue one administrator test to 2010039681@qq.com, and verify one provider ID plus one sent row. Reusing the same dedupe key must not deliver a second message.

- [ ] **Step 9: Validate expiry without real Douyin send**

Use controlled executor injection returning login_expired. Verify account invalidation, task pause, one in-app notice, and one Outbox event. Do not send a real Douyin message.

- [ ] **Step 10: Final production checks**

- https://wangze.oilu.cn/login returns 200.
- Web healthy; Auth, Worker, Notifier running with restart count 0.
- Port 8899 loopback-only; notifier has no port.
- BPS and port 8888 unchanged.
- Logs contain no API key, PII key, full email, verification code, Cookie, traceback, or duplicate delivery.
- Root disk retains rollback capacity.

- [ ] **Step 11: Uncommitted/unpushed handoff**

Run git status --short, list task-owned files, retain timestamped backups, remove only verified temporary uploads, and report explicitly that no commit or push occurred.

---

## Execution Checkpoints

Tasks 1–2 establish cryptographic and schema contracts. Tasks 3–5 deliver registration and binding. Task 6 adds isolated delivery. Task 7 is the only first-phase business trigger. Tasks 8–9 expose owner/admin workflows. Task 10 completes deployment contracts. Task 11 is the only production deployment task.

After Tasks 2, 4, 7, and 10, run every affected console test module before continuing. Never deploy partial model or route changes because running code must not observe an unapplied migration.
