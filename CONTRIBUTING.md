# Contributing

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
pytest -q
```

Set `BROWEYE_CHROMIUM=/path/to/chrome` to use an existing Chromium.

## Principles

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before larger changes. In short:

- **Keep the core small.** The capture → inspect → edit → replay → compare loop is the product.
  New features grow around it; `repeater`, `diff`, `analysis` and `interop` stay independent of the browser and UI.
- **Captures are immutable.** `RequestSpec` is frozen; editing happens on a `RequestDraft` copy.
- **Send exactly what the user sees.** No hidden default headers in the Repeater.
- **Test against the oracle.** Assert on what the lab server (`src/broweye/lab.py`) received, not on what the app reports.
  Use a real browser, not Playwright mocks.
- **No `sleep` in tests.** Use `drain()` to wait for in-flight captures.
- **UI safety.** Insert captured data with `textContent` only; keep the CSP free of inline scripts and `eval`.
- **Security rules get their own tests** (credentials across origins, API token, `Host` checks).

## Pull requests

- Keep each PR focused on one change, with tests.
- Run `pytest -q` locally before pushing.
- Update `docs/LIMITATIONS.md` or `docs/ROADMAP.md` when a change affects them.
- Code, comments and documentation are written in English.
