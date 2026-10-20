# Security

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's
[private vulnerability reporting](https://github.com/danicarr1/BrowEye/security/advisories/new)
and include steps to reproduce, the affected version or commit, and the impact you observed.

## Security model

BrowEye's local API can drive a browser and send arbitrary HTTP requests, so it is locked down:

- It listens only on `127.0.0.1`.
- Every call requires a random per-run token (`X-BrowEye-Token`), compared in constant time.
- The `Host` header must match the app's own loopback address, which blocks DNS rebinding.
- The UI ships a `default-src 'self'` Content Security Policy and inserts captured data with `textContent` only,
  so a malicious response cannot execute code inside the tool.
- Repeater credentials are never forwarded across origins on redirects.

Issues that break any of these guarantees are in scope.

## Data at rest

The project directory (`broweye-project/` by default) stores captured cookies, tokens and bodies **in plain text**,
including `sessions/*.storage.json`. HAR exports are not redacted. Treat these files as secrets.

## Intended use

BrowEye is for debugging your own applications and for security testing you are authorized to perform.
It deliberately does not implement TLS fingerprint impersonation or other evasion features.
