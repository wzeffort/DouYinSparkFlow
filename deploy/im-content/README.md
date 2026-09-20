# IM receipts and per-recipient content

This release layers committed files onto the individually verified web/worker
images. It does not rebuild auth/notifier from the mixed root source.

Features:
- Prefer available exact `sec_uid` conversation identity; recheck the conversation
  before sending. Names with ordinary versus nonbreaking spaces compare equally.
  Name differences remain diagnostic only; unknown identity keeps the existing
  exact-name selection fallback. Unknown identity cannot prove a server receipt.
- Passive HTTP send monitoring, correlated to a new request object containing
  the exact conversation ID and full message. Accept only recognized successful
  envelopes with a message ID. No WebSocket decoder, no DOM-only success claim,
  no automatic resend after an unknown/rejected acknowledgement.
- Fixed content remains default. Per-recipient one-liner categories, random
  custom lists and Shanghai-date rotation; preview is labelled illustrative.
  The only external content destination is `https://v1.hitokoto.cn/`, without
  credentials or account information. Errors use a configured backup message.
- The additive `run_message_content` table stores actual text once and reuses it
  for retries. Fetching external content occurs outside SQLite write transactions.
  It also stores bounded acknowledgement diagnostics and a durable retry counter.
  Batch interruptions stop automatic retries after three total attempts.
- Run history shows actual content and diagnostics; retry badges consult the
  current retry queue rather than a historical stage alone.

Protocol fields are based on v3.2.1 source analysis, not an official stable API.
Reference: https://github.com/2061360308/DouYinSparkFlow/tree/v3.2.1
An unrecognized packet remains submitted/unknown. Offline fixtures test the
parsing and request association; no real-send acceptance claim is made.

Local verification before publication: 130 focused tests passed, including the
two existing mobile/desktop checks and the new content-picker browser check,
request-object correlation, strict payload parsing, retry text persistence,
durable retry limits, task forms and snapshot export integrity. No real messages
were sent. Broader pre-existing unrelated failures recorded in cloud_sources/
remain outside this release's verified scope.

## Deployment

Only use a `git archive` of a committed revision. Include `COMMITTED_REVISION`
as archive metadata containing the full commit hash. Extract into the previously
nonexistent `/opt/douyin-spark-console/releases/im-content-<commit>` directory.
The archive includes core/, spark_console/, deploy/im-content/, .dockerignore.
Run `sudo python3 deploy/im-content/activate.py --revision <commit>` there.
`--verify-only` builds the candidates and runs isolated, offline smoke checks.

Activation validates the known base image IDs and rejects container /app changes.
Only build/image settings may differ in the layered Compose configuration.
It acquires the existing cross-service browser lock, checks no running or due
tasks within five minutes, makes a consistent SQLite backup and saves the
existing Compose, creates only the additive table, and replaces web/worker.
The original dirty project Git tree and Compose are not overwritten.

The active Compose command is the original compose **plus**
`deploy/im-content/compose.release.yml` in this committed release directory,
with `SPARK_REVISION=<commit>` and `SPARK_RELEASE_ROOT=<release-directory>`.
Future maintenance must include this override to retain the active version.

Rollback: during an idle window, use the original Compose alone with
`up -d --no-deps --no-build spark-web spark-worker` to restore the previous images.
The activation script does this automatically if its readiness/revision checks
fail. Leave the additive table intact; never restore an old DB over later sends.
New dynamic tasks should be paused/converted to fixed content before a later
manual rollback, since old workers do not render content specifications.
