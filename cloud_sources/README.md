# Cloud source checkpoint — 2026-09-18

This checkpoint imports application source from the four running Spark Console
services, not from the older server Git working tree. The server tree and running
images differ. No production configuration, environment, database, cookies, keys,
logs, screenshots, backups or user data are included. No cloud changes were made
while collecting this checkpoint.

Captured image IDs (source provenance, not portable registry references):

| Role | Image ID |
| --- | --- |
| web | `sha256:2090b06e28dff3daf19cc466be8a0d97415d4cb2a712dcc85781dc19ef18a0b7` |
| worker | `sha256:5b71488121d9f92cbdfc572aff152f36bfae8b348edfeca50bb3f5a6823969c6` |
| auth | `sha256:a2619453d77d8396c327b8e0a7a3453bbe44c49ce09acb159a33fc17a80c168d` |
| notifier | `sha256:0c8610977d999a8f26fce2e6d0f3559e7d008bb3591e8f377ec3508a4bcaf433` |

## Source layout

The top-level application is the web source with these worker modules retained:
`core/web_chat.py`, `spark_console/auth_worker.py`,
`spark_console/browser_runtime.py`, `spark_console/executor.py`,
`spark_console/worker.py`, `spark_console/services/batch_execution.py`.
`main.py` and `web_chat_probe.py` come from the server project directory.
The recipient review regression tests are the locally authored tests for the
deployed change. Existing repository-only files and documentation are retained.

The root is an integration workspace, **not a claim that every service runs the
same code**. The `web.json`, `worker.json`, `auth.json`, and `notifier.json` manifests
record the SHA256 of every collected source file in each role. Files differing
from the root live in `overrides/<role>/`. This preserves role differences rather
than silently replacing the auth/notifier code with the newer web/worker code.

The collection allowlist is `core/`, `spark_console/`, `utils/`, `tests/` with
`.py`, `.js`, `.css`, `.html` extensions, plus the two requirements files and
`Dockerfile.console`. These are source snapshots, not full container backups.

## Verify or export (local only)

```sh
python scripts/export_cloud_source.py web
python scripts/export_cloud_source.py worker
python scripts/export_cloud_source.py auth
python scripts/export_cloud_source.py notifier
python scripts/export_cloud_source.py worker /path/to/new/empty-parent/worker-source
```

The destination must not exist. All hashes are checked before export. Each role's
export contains only its manifest files, not files belonging only to another role.
The export commands do not build, connect, send messages or deploy.

The repository's existing Compose/environment examples are **templates**, not a
copy of the live configuration. Do not use the mixed root to replace all four
production services. A later deployment requires a separately reviewed build from
the appropriate role export, explicit configuration review, database/schema
compatibility checks, task-drain checks, and authorization. Auth/notifier currently
use older models than web/worker; preserving that fact is not a migration plan.

## Current recipient policy and known issues

After selecting the task's target, a nonempty differing chat title is diagnostic
only; it does not require a shared character or manual approval. Target selection
is not a one-character fuzzy search. Page readiness and unique target checks still
apply. Diagnostic logs and historical approval/revocation support are preserved.

The batch-timeout retry budget and historical static "retrying" label are known
unresolved issues. This source checkpoint does not claim to fix them. No real
message-send tests or historical retries are performed during publication.

## Publication validation

Local validation on Windows / Python 3.14:

- Targeted suite: **89 passed** (snapshot exports, recipient review/policy,
  web-chat selection, scheduler/worker, database/config, crypto and secret tests).
- Wider `tests/console` suite: **383 passed, 48 failed, 2 skipped** (132 subtests
  passed). This is **not** an all-green release. Failures include missing local
  Playwright Chromium and the `openai` package; other failures involve old
  recipient-policy expectations, service interface differences, password rules,
  and UI/API assertions. Not every failure has been independently classified as
  pre-existing. They are retained for later reconciliation, not hidden or changed
  merely to make this source checkpoint pass.
- Source hashes verified for all four role snapshots; targeted credential-pattern
  scan found no matches in imported application/source files. This is not a formal
  security audit.
- Diff whitespace checks report pre-existing whitespace in byte-exact cloud
  sources. Those source bytes are intentionally preserved, including mixed line
  endings, rather than silently formatted during archival.
- No browser installation, production login, actual send, deployment, restart,
  or production data migration was performed for this publication.
