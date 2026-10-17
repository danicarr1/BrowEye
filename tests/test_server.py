import json

import httpx
import pytest
from playwright.async_api import async_playwright

from broweye.capture import find_chromium
from broweye.server import serve


@pytest.fixture
async def ui(app):
    runner, url = await serve(app, port=0)
    base, token = url.split("/#token=")
    yield base, token, url
    await runner.cleanup()


async def test_api_requires_token(ui):
    base, token, _ = ui
    async with httpx.AsyncClient() as c:
        assert (await c.get(f"{base}/api/state")).status_code == 401
        assert (await c.get(f"{base}/api/state", headers={"X-BrowEye-Token": "nope"})).status_code == 401
        assert (await c.get(f"{base}/api/state", headers={"X-BrowEye-Token": token})).status_code == 200


async def test_api_rejects_foreign_host_header(ui):
    """DNS rebinding: a page on evil.test resolving to 127.0.0.1 sends Host: evil.test."""
    base, token, _ = ui
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{base}/api/state", headers={"X-BrowEye-Token": token, "Host": "evil.test"})
        assert r.status_code == 403


async def test_api_flow_to_repeater_roundtrip(ui, app, lab):
    base, token, _ = ui
    h = {"X-BrowEye-Token": token}
    async with httpx.AsyncClient(base_url=base, headers=h, timeout=30) as c:
        s = (await c.post("/api/sessions", json={"name": "api"})).json()
        assert (await c.post(f"/api/sessions/{s['id']}/navigate", json={"url": lab.base + "/echo?x=1"})).json()["status"] == 200
        await app.browser.drain()
        rows = (await c.get("/api/flows", params={"text": "/echo"})).json()
        d = (await c.post("/api/drafts", json={"from_flow": rows[0]["id"]})).json()
        d = (await c.put(f"/api/drafts/{d['id']}", json={"method": "POST", "body_text": "edited=1"})).json()
        ex = (await c.post(f"/api/drafts/{d['id']}/send")).json()
        assert ex["status"] == 200 and lab.last("/echo", "POST").body == b"edited=1"
        cmp = (await c.get("/api/compare", params={"a": f"flow:{rows[0]['id']}", "b": f"exec:{ex['id']}"})).json()
        assert cmp["body"]["kind"] == "json" and cmp["summary"]["body_changed"]
        har = (await c.get("/api/export/har")).json()
        assert any(e["request"]["url"].endswith("/echo?x=1") for e in har["log"]["entries"])
        bad = await c.put(f"/api/drafts/{d['id']}", json={"cookie_mode": "bogus"})
        assert bad.status_code == 400
        assert (await c.get("/api/flows/doesnotexist")).status_code == 404


async def test_ui_end_to_end(ui, app, lab):
    """Drive the real UI with a second browser: capture -> repeater -> edit -> send."""
    _, _, url = ui
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=find_chromium())
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(url)
        # "+ Session" asks for a name (prompt) and whether to reuse state (confirm).
        page.on("dialog", lambda d: d.accept("ui-test") if d.type == "prompt" else d.dismiss())
        await page.click("#new-session")
        # (wait_for_function would need eval, which the UI's CSP correctly forbids)
        await page.locator("#session option", has_text="ui-test").wait_for(state="attached")
        await page.fill("#nav-url", lab.base + "/")
        await page.click("#nav button")
        row = page.locator("#flows tbody tr", has_text="POST").first
        await row.wait_for(timeout=15000)
        await row.click()
        await page.locator("#flow-detail", has_text="widget").wait_for()
        await page.click("text=Send to Repeater")
        body = page.locator("#draft-form textarea[name=body]")
        await body.wait_for()
        await body.fill(json.dumps({"name": "from-ui", "qty": 42}))
        await page.click("#draft-form button.primary")
        await page.locator("#result", has_text="201").first.wait_for()
        assert json.loads(lab.last("/api/items", "POST").body) == {"name": "from-ui", "qty": 42}
        await page.click("text=Compare with original")
        await page.locator("#cmp-out", has_text="qty").wait_for()
        assert not errors, errors
        await browser.close()
