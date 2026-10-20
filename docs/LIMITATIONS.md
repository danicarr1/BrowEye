# Known limitations (v0.1)

## What traffic is observed

- **Only the Chromium that BrowEye launches.** Traffic from other browsers, mobile apps or other programs is not visible
  (that would require a proxy; see ROADMAP).
- **WebSockets**: frames are not captured. The handshake may or may not appear, depending on the browser.
- **Streaming responses** (SSE, long downloads): the body may be unavailable or incomplete.
- **Request bodies not exposed**: for file uploads (`multipart` with files) or streamed bodies,
  Chromium may not provide the body. The UI says so with a note ("request body not exposed by the browser").
- **Redirect (3xx) response bodies**: the browser does not provide them.
- **Resources served from cache or service workers** may appear with partial information.
- Size limits: 10 MB per captured body and 20 MB per Repeater response. Truncation is flagged with a note.
- `data:` and `blob:` URLs are ignored.

## Differences between the browser and the Repeater

- **Protocol**: the browser may use HTTP/2 or HTTP/3; the Repeater uses **HTTP/1.1**. Servers that behave
  differently per protocol will give different results.
- **TLS fingerprint**: differs from Chromium's. Some anti-bot systems respond differently. There are no, and will be no,
  fingerprint-impersonation features: that is not a goal of this project.
- **Header order and casing**: the Repeater respects the draft's order, but `Host` goes first.
  Over HTTP/2 the browser sends header names in lowercase.
- **Cookies in `session` mode**: they are *read* from the browser's jar. `Set-Cookie` headers received by the Repeater
  do **not** update the browser, and within a redirect chain they are not applied to subsequent hops either.
  This is an explicit decision (the Repeater does not alter the browser session), but it breaks login flows
  that rely on cookies set by a 302.
- Basic/Bearer authentication configured on a draft lives in memory: it is lost on restart.

## Persistence and security

- The database stores cookies, tokens and bodies **in plain text**. The `sessions/*.storage.json` files
  (reusable sessions) do too. Protect the project directory.
- cURL export redacts `Cookie` and `Authorization` by default. **HAR export does not redact them**,
  because it is a faithful export; review the file before sharing it.
- Passive checks are hints for a human, not a scanner; expect false positives and false negatives.

## Platform and scale

- Tested on Linux (headless, Chromium 141, Python 3.13). **Windows and macOS have not been tested**.
  There is no platform-specific code other than signal handling (which is handled).
- `--headed` requires a graphical environment.
- A single process with SQLite on the event loop thread: writing very large bodies can pause the UI for a few milliseconds.
  Tested with 150 concurrent browser requests plus 30 concurrent sends; larger volumes have not been measured.
- The traffic table keeps at most 5000 rows in the DOM (filters query the full database).
- No HAR import, no upstream proxy, no client certificates.
