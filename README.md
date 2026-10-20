# BrowEye

**See what your browser actually sends — then change it, replay it, and compare.**

BrowEye is a local tool for observing, editing, replaying and comparing HTTP/HTTPS traffic produced by a real browser.
It drives Chromium through Playwright, records every request and response into SQLite, and gives you a
Repeater, a diff view and passive checks in a small web UI that runs on your machine.

Use it to debug web applications, understand APIs you are permitted to work with, and support **authorized** security testing.

```
Chromium (Playwright) ──events──▶ capture ──▶ SQLite ◀── Repeater (httpx)
                                                 ▲              ▲
                                 local API 127.0.0.1 + token ───┘
                                                 ▲
                                      web UI (no-build JS)
```

## Why BrowEye

- **No CA, no proxy.** Traffic is captured by instrumenting the browser, HTTPS included. Nothing to install in your trust store.
- **Identities are first-class.** Each session is an isolated browser context with its own cookies and storage — open `admin` and `user` side by side.
- **What you see is what is sent.** The Repeater builds requests by hand: no hidden default headers, and every redirect hop shows the request that actually went out.
- **Credentials stay where they belong.** `Cookie`, `Authorization` and configured auth are never forwarded across origins on redirects, and each hop says so.
- **History you can query.** Captures and Repeater executions persist in a single SQLite file, searchable by URL, method, status, host and body.
- **Small and readable.** ~3k lines of Python and framework-free JS. Every acceptance test runs against a real browser and a server that records what it received.

## Installation

Requires Python 3.11+.

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
playwright install chromium                         # downloads the browser (once)
```

If Chromium is already installed elsewhere: `export BROWEYE_CHROMIUM=/path/to/chrome`.

## Usage

```bash
broweye lab --port 8081                          # (optional) local lab server to practice against
broweye --headed --start-url http://127.0.0.1:8081/
```

The command prints a URL such as `http://127.0.0.1:8765/#token=…`. Open it in your everyday browser.
The token protects the local API — do not share it.

Typical workflow:

1. **Traffic** — browse in the Chromium window (`--headed`) or from the UI's URL bar. Requests appear live.
   Filter by URL, method, status (`200`, `4xx`), host or body content.
2. Select a request to see its URL, query, headers, body (formatted as JSON or form data), response and passive findings.
3. **Send to Repeater** creates a *draft* (`RequestDraft`): an editable copy. The original capture never changes.
4. Edit method, URL, headers (one per line; order and duplicates are preserved), body (text or base64/file),
   cookie mode, authentication, timeout and redirect handling. Press **Send**.
5. The right-hand pane shows **the request that was actually sent** on every redirect hop, and its response.
   Every send is kept in the draft's history.
6. **Compare with original**, or the A/B buttons, compare any capture (`flow:`) or execution (`exec:`).
7. **+ New** or **Import cURL** create requests from scratch. **Export HAR** and **Copy cURL** export them.
8. **+ Session** opens another isolated identity (its own cookies and storage).

Without a browser (request builder and Repeater only): `broweye --no-browser`. Run `broweye --help` for all options.

### Repeater cookie modes

| Mode | What is sent |
|---|---|
| `as_is` (default) | Exactly the draft's `Cookie` header, copied from the capture and editable |
| `session` | The **current** cookies from the chosen browser session's jar, filtered by domain, path and secure for the target URL |
| `none` | No cookies |

In every mode, the `Cookie` and `Authorization` headers and configured authentication are **not sent to another origin**
when a redirect leaves the initial origin. Each hop states this with a note.

## Tests

```bash
pytest -q
```

31 tests. They use a local lab server (`src/broweye/lab.py`) that **records what it receives**,
so assertions check what reached the server, not what the app claims to have sent.
They include an end-to-end test that drives the UI with a second browser.

| Acceptance scenario | Test |
|---|---|
| Browse a local server, traffic appears | `test_acceptance.py::test_browse_is_captured` |
| POST with JSON, the server receives the correct body | `test_post_json_capture_matches_server` |
| Edit a captured body, the modified one is sent (with correct Content-Length) | `test_edited_body_is_sent` |
| Change a header, the server receives the new value | `test_changed_header_is_sent` |
| New request without a capture (text and binary, no hidden headers) | `test_new_request_without_capture` |
| Cookies follow the chosen mode, sessions are isolated | `test_cookie_modes` |
| Compare responses, relevant differences surface | `test_compare_responses` |
| 150 browser requests plus 30 concurrent sends, consistent history | `test_many_requests_history_consistent` |
| Full flow from the UI | `test_server.py::test_ui_end_to_end` |

Also covered: credentials on cross-origin redirects, 302/303/307 semantics, gzip/br/zstd,
the API token, rejection of foreign `Host` headers (DNS rebinding), cURL round-trip, HAR and JSON diff.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — design decisions, rejected alternatives and the data model.
- [docs/LIMITATIONS.md](docs/LIMITATIONS.md) — what this version cannot see or do.
- [docs/ROADMAP.md](docs/ROADMAP.md) — what version 2 would include.
- [CONTRIBUTING.md](CONTRIBUTING.md) — development setup and project principles.
- [SECURITY.md](SECURITY.md) — security model and how to report vulnerabilities.

## Responsible use

Use BrowEye only against systems you own, lab environments, or systems you have explicit, scoped authorization to test.
The project database (`broweye-project/`) holds captured cookies and tokens in plain text: treat it as a secret.

## License

[MIT](LICENSE)
