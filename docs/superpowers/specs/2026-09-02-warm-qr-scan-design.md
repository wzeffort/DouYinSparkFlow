# Warm QR Scan Design

## Goal

Reduce the normal wait for a Douyin binding QR code without weakening per-user
session isolation. Desktop users keep the full remote-browser screenshot; mobile
users see a clear QR crop until the QR is consumed, then switch to the full
remote-browser view for confirmation or SMS-code interaction.

## Current constraints

- The authentication worker polls for work every 10 seconds.
- Every scan starts and closes Playwright, Chromium, a browser context, and a
  Douyin chat page.
- The database intentionally allows only one global active binding session.
- Browser credentials, QR images, interactions, and scan status are owner-bound.
- Current page-load preloading starts a real global session and only runs for a
  user with no existing accounts.

## Selected architecture

### Single warm slot

The authentication worker owns one long-lived Playwright runtime and Chromium
browser. While idle it prepares one anonymous browser context and chat-login
page. The prepared context remains private worker memory: it has no user owner,
no public scan identifier, and no QR image is written to the database.

When the worker claims a queued database session, it atomically consumes the
prepared context for that session. It immediately publishes both:

- a full-page screenshot for desktop and later remote interaction; and
- a QR-element crop for the mobile scan stage.

After success, cancellation, expiry, browser failure, or worker shutdown, that
context is always destroyed. A context is never returned to the warm pool or
assigned to another user. The worker then prepares a new anonymous context.

### Expiry and recovery

The worker detects when the prepared QR element changes or is no longer usable
and rebuilds the anonymous context. A configurable maximum warm age provides a
second safety boundary. If Chromium crashes, the worker recreates the browser.
If no warm context is ready when a request arrives, the same request falls back
to cold preparation rather than failing.

The global active-session limit remains one. This version does not create a
multi-browser pool because concurrent Douyin login pages increase memory use,
account-risk signals, and cross-session complexity.

### Dispatch latency

Reduce the authentication worker's idle claim delay from the current maximum of
10 seconds to approximately one second. Do not make the web process own browser
state and do not couple this console to the unrelated Redis service.

### Frontend behavior

Remove page-load creation of a real scan session. Merely opening the accounts
page must not reserve the global channel for five minutes.

On desktop, clicking bind opens the dialog and displays the full cloud-browser
screenshot, preserving the existing click forwarding and SMS-code workflow.

On mobile, while status is `awaiting_scan`, display the QR crop at a readable
size with actions to open the image and save it. Explain the same-device flow:
save or long-press the image, open Douyin Scan, then choose it from the photo
album. Also show the alternative of scanning from another device. Once the QR is
consumed or the session enters confirmation, switch to the full cloud-browser
screenshot so remote clicks and SMS-code input still use the same coordinate
space as the server screenshot.

CSS media queries select the presentation; the server's authorization rules do
not depend on the reported device type. Both image endpoints require the scan
owner and return `Cache-Control: no-store`.

## Data and API changes

- Store a separate nullable QR-crop PNG on the scan session in addition to the
  existing full-page PNG.
- Keep the existing full screenshot endpoint and add an owner-authorized endpoint
  for the QR crop.
- Enforce PNG signature and size limits for both images.
- Clear both images when the session becomes terminal or is cleaned up.
- Do not persist prepared anonymous QR images, browser cookies, or contexts.

## Observability

Record structured timing events for queue wait, browser readiness, page load,
QR readiness, assignment, and terminal outcome. Logs contain session identifiers
and durations but no QR contents, cookies, phone numbers, or login credentials.
This makes cold-start and Douyin-side regressions distinguishable.

## Security and failure boundaries

- One prepared context can be consumed only once.
- A QR crop and full screenshot are readable only by the owning authenticated
  user.
- Cancellation and page exit invalidate the assigned context.
- No context or cookie state survives assignment completion.
- Rate limits and CSRF validation remain in place.
- Browser preparation has bounded timeouts and exponential restart backoff so a
  Douyin outage cannot create a tight restart loop.

## Verification

- Unit tests for warm-context one-time consumption, expiry, cleanup, crash
  recovery, and cold fallback.
- Worker tests proving owner isolation and preservation of the global slot.
- Web tests proving owner-only access to both image variants.
- JavaScript tests for desktop full screenshot and mobile QR-to-full-view switch.
- Regression tests for cancellation, page exit, SMS input, and successful account
  creation.
- Container smoke test plus a production timing check after deployment.

## Deployment

Run focused tests, then the full console test suite. Deploy only to the existing
cloud server, preserve production data and secrets, restart the affected console
containers, verify health and scan logs, and do not push to GitHub.

## Registration validation UX

Replace the single `注册信息或邀请码无效` response with field-scoped validation.
The server remains authoritative and returns a general form message plus an error
next to the responsible field:

- username format: `用户名须为 3–32 位字母、数字、下划线或短横线`;
- unavailable username: `该用户名不可用，请更换`;
- invalid email syntax: `请输入有效的邮箱地址`;
- unavailable email: `该邮箱不可用，请更换`;
- weak password: state the missing requirement (length, letter, or number);
- password mismatch: `两次输入的密码不一致`;
- invitation state: distinguish invalid, expired, already used, and revoked;
- rate limiting: `尝试次数过多，请稍后再试` rather than blaming the invite.

The registration page performs matching client-side validation as the user types
or leaves a field. Invalid controls use `aria-invalid`, a visible red boundary,
and a nearby text explanation linked with `aria-describedby`. Password rules use
a live checklist. Invite validity is checked only during form submission—there
is no public invite-probing endpoint.

On a rejected submission, preserve username, email, and invite input so the user
can correct one field, but never echo either password. Server messages are mapped
from known validation codes instead of displaying raw exception strings. The
invite consume operation remains atomic, so two simultaneous registrations cannot
use the same invite even when its pre-submit state looked valid.

Verification adds service and route tests for every field code, invite-state race
handling, non-sensitive value preservation, password clearing, HTML escaping,
accessible markup, and browser-side validation behavior.
