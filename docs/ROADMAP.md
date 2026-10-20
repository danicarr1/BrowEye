# Roadmap: what version 2 would include

Ordered by value and risk. Each item is independent.

1. **Cookie propagation within an execution**: apply one hop's `Set-Cookie` headers to the following hops
   (an ephemeral per-execution jar), with an explicit "write back to the browser" option.
   This is the limitation that most affects real login flows.
2. **HTTP/2 in the Repeater** (`httpx[http2]`), selectable per draft, to reproduce what the browser does.
3. **HAR import**, to reopen work done with other tools.
4. **Second capture source: a proxy** (for example, mitmproxy as a library) producing the same `Flow` objects.
   This captures any client without touching the Repeater, store or UI. Requires managing a local CA securely.
5. **Minimal extension system**: registered Python functions for three specific hook points
   (analyze a flow, transform a draft before sending, and add a read-only panel).
   Not before there are at least two real extensions that justify it.
6. **Authorization comparison** (for authorized pentesting): replay the same draft with the cookies
   of several sessions or identities and compare the responses. A natural extension of the cookie modes
   and `diff.py`, with mandatory rate limiting and an in-scope host list.
7. Per-project **in-scope host list**: the Repeater refuses to send outside it.
8. A separate thread for large SQLite writes, and virtual scrolling in the traffic table.
9. Optional encryption of the project directory (or at least of the `storage_state` files).
10. Windows and macOS testing in CI (GitHub Actions with an OS matrix) — the workflow exists;
    those jobs are allowed to fail until they are green.
