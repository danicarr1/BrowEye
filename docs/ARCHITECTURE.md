# Architecture and design decisions

## Design goal

A small, verifiable core for the **capture → inspect → edit → replay → compare** loop.
Everything else (plugins, scanners, automation) can grow around it without touching that core.

## Modules

| Module | Responsibility | Depends on |
|---|---|---|
| `models.py` | `RequestSpec`, `ResponseSpec`, `Flow`, `RequestDraft`, `Execution`, `Hop` | nothing |
| `capture.py` | Browser sessions (one `BrowserContext` per identity) and traffic recording | Playwright, store |
| `store.py` | SQLite persistence (WAL) and queries | models |
| `repeater.py` | Executes a draft and records what was sent | httpx, models |
| `diff.py` | Response comparison | models |
| `analysis.py` | Passive checks on already-captured traffic | models |
| `interop.py` | cURL (import and export) and HAR (export) | models |
| `app.py` | Service that orchestrates everything; used by both the API and the tests | all of the above |
| `server.py` | Local HTTP API, SSE and static files | app, aiohttp |
| `lab.py` | Local lab server that records what it receives; the test oracle | aiohttp |
| `static/` | UI (HTML, CSS and JS, no framework, no build step) | API |

`repeater`, `diff`, `analysis` and `interop` do not depend on the browser or the UI. They are tested in isolation
and can be reused from scripts.

## The request model

There are three distinct kinds of "request", and they never mix:

- **`RequestSpec` (immutable)**: what was captured or sent. It is `frozen`, so it cannot be edited by accident.
- **`RequestDraft` (mutable)**: what the user edits. `from_flow()` **copies** headers and body;
  modifying the draft cannot alter the capture (verified by `test_draft_from_flow_is_a_copy`).
  On creation, `Host`, `Content-Length` and `Transfer-Encoding` are dropped, because they are derived from the URL and body at send time.
  Keeping a stale copy is the classic cause of "I edited the body and it wasn't sent".
- **`Execution`**: a snapshot of the draft **at the moment it was sent**, plus a list of `Hop`s.
  Each hop stores the request that actually went out (with the real headers httpx set) and the response.

This lets the UI show unambiguously: the original (flow), the edited version (draft) and what was sent (hop.request).

Headers are ordered lists of pairs, not dictionaries. HTTP allows duplicates, and order can matter.

## Key decisions and rejected alternatives

### Capture: browser instrumentation (Playwright) instead of a MITM proxy

- **Chosen**: Chromium launched by Playwright. Every request and response arrives as an event, HTTPS included.
  No CA certificate to install, no system proxy to configure.
- **Rejected: MITM proxy (mitmproxy or custom)**. It sees traffic from any application, but requires installing
  and trusting a CA. That is installation friction, and a risk if the CA leaks. It also adds a large component.
  It is worth adding as a **second capture source** later (see ROADMAP), behind the same `Flow` interface.
- **Rejected: browser extension**. It cannot read response bodies reliably without the debugging protocol,
  and it means maintaining per-browser packaging.
- **Rejected: Selenium**. No native access to network bodies; Playwright has it.

Problem found during development: if the body is requested while handling `requestfinished`,
a fast navigation makes Chromium discard the previous document and the body is lost
("No resource with given identifier found"). The body is now requested in the `response` event callback.
This is covered by `test_browser_capture_of_compressed_responses`, which failed before the fix.

### Execution: httpx with hand-built requests

- `httpx.Request(...)` + `client.send()` instead of `client.get()`, so that **no** default headers are added
  (`User-Agent: python-httpx`, `Accept`…). The server receives what the user sees plus what HTTP requires
  (`Host`, `Content-Length`). Verified by `test_new_request_without_capture`.
- Redirects are followed **manually**, to record every hop and decide explicitly which credentials travel.
  On a change of origin, `Cookie`, `Authorization` and any configured authentication are removed.
- `requests` was rejected (not async), and so was `aiohttp` as a client: it normalizes and adds headers with less control.
- `brotli` and `zstd` extras: the browser advertises `Accept-Encoding: br, zstd`. Without those decoders,
  replaying a request captured from a real site would show compressed garbage.

### Persistence: SQLite

Ships with Python, a project is a single file, works on every platform and allows searching inside bodies
(stored as BLOBs, not base64). Uses WAL mode. All access happens on the event loop thread:
operations are short, and this avoids a connection pool.

Repeater authentication secrets are **not** stored in SQLite; they live in memory
(they must be re-entered after a restart). Captured cookies are stored, because they are part of the traffic.

### UI: local web page, framework-free JS

- **Chosen**: a page served by the app itself. Works on Linux, Windows and macOS without native packaging.
  It is roughly 540 lines of JS with no build step: anyone on the team can read and change it.
- **Rejected: TUI (Textual)**. Viewing and editing large bodies, side-by-side comparison and long tables
  are much worse in a terminal.
- **Rejected: Electron or Tauri**. They add packaging, updates and a second runtime without solving a current problem.
- **Rejected (for now): React or similar**. UI state is simple. If it grows, migrating is possible
  because the API is the boundary.

All captured data is inserted with `textContent`. The UI has a `default-src 'self'` CSP (no inline, no eval),
so a malicious response cannot execute code inside the tool.

### Local API security

The API can drive a browser and send arbitrary requests, so an arbitrary website must not be able to use it:

- it listens only on `127.0.0.1`;
- a random per-run token is required on every call (`X-BrowEye-Token`), compared in constant time;
- the `Host` header must be the app's own loopback address, which blocks DNS rebinding (`test_api_rejects_foreign_host_header`).

### Non-blocking live updates

SSE events only say "there is something new". The UI then requests `GET /api/flows?after_seq=N` in batches.
Bursts are coalesced, a slow client loses notifications but never data, and the table keeps
at most 5000 rows in the DOM.

## How the tests are designed

1. **The lab server is the oracle**: it records method, path, query, raw headers and body.
   Assertions of the form "X was sent" are made against that record.
2. **A real browser** in the acceptance tests (headless Chromium), no Playwright mocks.
3. **`drain()`** waits for in-flight captures to be written, without `sleep`, to avoid flaky tests.
   The suite was run 3 times in a row: 31/31 on each run.
4. Security rules (cross-origin credentials, token, Host) have their own tests.

## How BrowEye differs from existing tools (honestly)

- **vs. Burp and ZAP**: no CA or proxy to configure; isolated browser contexts are first-class sessions;
  Repeater cookie modes are explicit; credential handling on redirects is visible;
  open source, lightweight and Python-based. **It lacks a lot**: Intruder, a scanner, mature extensions,
  HTTP/2 in the Repeater, capture from any client.
- **vs. mitmproxy**: a UI built around the edit-and-compare loop, with integrated browser sessions.
  mitmproxy captures any client and is far more mature as a proxy.
- **vs. Chrome DevTools**: persistent, queryable history; a real Repeater with history;
  structural comparison and multiple sessions. DevTools has better JS integration, performance tooling and WebSockets.
- **vs. Postman or Insomnia**: requests originate from real browser traffic, with its cookies and identity.
