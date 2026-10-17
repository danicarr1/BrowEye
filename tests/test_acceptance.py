"""Acceptance scenarios. Every assertion about "what was sent" is checked
against the lab server's own log of what it received."""

import asyncio
import json


async def browse(app, lab, name="main"):
    s = await app.open_session(name)
    await app.navigate(s["id"], lab.base + "/")
    page = app.browser.sessions[s["id"]].page
    await page.wait_for_function("document.getElementById('out').textContent.startsWith('done')")
    await app.browser.drain()
    return s


def find(app, method, path):
    rows = [r for r in app.query_flows(method=method) if r["path"] == path]
    assert rows, f"no captured {method} {path}"
    return rows[-1]


# 1. Navigating to a local HTTP server shows requests and responses
async def test_browse_is_captured(app, lab):
    s = await browse(app, lab)
    rows = app.query_flows(session_id=s["id"])
    seen = {(r["method"], r["path"], r["status"]) for r in rows}
    assert ("GET", "/", 200) in seen
    assert ("GET", "/api/items", 200) in seen
    assert ("POST", "/api/items", 201) in seen

    detail = app.flow_detail(find(app, "GET", "/api/items")["id"])
    assert json.loads(detail["response"]["body_text"])["page"] == 1
    assert detail["response"]["status"] == 200
    assert app.query_flows(status="2xx") and app.query_flows(text="items")


# 2. POST with JSON: the captured body is what the server got
async def test_post_json_capture_matches_server(app, lab):
    await browse(app, lab)
    flow = app.flow_detail(find(app, "POST", "/api/items")["id"])
    got = lab.last("/api/items", "POST")
    assert json.loads(flow["request"]["body_text"]) == {"name": "widget", "qty": 3}
    assert got.body.decode() == flow["request"]["body_text"]
    assert json.loads(got.body) == {"name": "widget", "qty": 3}


# 3. Editing a captured body sends the edited body (and a correct Content-Length)
async def test_edited_body_is_sent(app, lab):
    await browse(app, lab)
    original = app.flow_detail(find(app, "POST", "/api/items")["id"])
    d = app.create_draft(from_flow=original["id"])
    new_body = json.dumps({"name": "widget", "qty": 300, "extra": "longer body than before"})
    app.update_draft(d["id"], {"body_text": new_body})

    ex = await app.send(d["id"])
    got = lab.last("/api/items", "POST")
    assert got.body == new_body.encode()
    assert got.header("content-length") == str(len(new_body))
    assert ex["status"] == 201
    # The original captured record is untouched.
    assert json.loads(app.flow_detail(original["id"])["request"]["body_text"])["qty"] == 3
    # Header captured from the browser was preserved through the edit.
    assert got.header("x-lab") == "browser"


# 4. Changing a header: the server receives the new value
async def test_changed_header_is_sent(app, lab):
    await browse(app, lab)
    d = app.create_draft(from_flow=find(app, "POST", "/api/items")["id"])
    headers = [[k, "edited" if k.lower() == "x-lab" else v] for k, v in d["headers"]]
    headers.append(["X-Added", "yes"])
    app.update_draft(d["id"], {"headers": headers})
    ex = await app.send(d["id"])
    got = lab.last("/api/items", "POST")
    assert got.header("x-lab") == "edited"
    assert got.header("x-added") == "yes"
    assert json.loads(ex["hops"][0]["response"]["body_text"])["x_lab"] == "edited"


# 5. A brand-new request works without any capture
async def test_new_request_without_capture(app_nobrowser, lab):
    app = app_nobrowser
    d = app.create_draft(fields={
        "method": "PUT", "url": lab.base + "/echo?a=1&b=two",
        "headers": [["Content-Type", "application/x-www-form-urlencoded"]],
        "body_text": "x=1&y=2",
    })
    ex = await app.send(d["id"])
    got = lab.last("/echo")
    assert (got.method, got.query, got.body) == ("PUT", "a=1&b=two", b"x=1&y=2")
    # No hidden default headers: only what we set plus what HTTP requires.
    names = {k.lower() for k, _ in got.headers}
    assert names == {"host", "content-type", "content-length"}
    assert ex["status"] == 200
    # Binary body
    app.update_draft(d["id"], {"body_b64": "AAEC/w==", "headers": [["Content-Type", "application/octet-stream"]]})
    await app.send(d["id"])
    assert lab.last("/echo").body == b"\x00\x01\x02\xff"


# 6. Cookies follow the mode the user picked
async def test_cookie_modes(app, lab):
    s = await app.open_session("alice")
    await app.navigate(s["id"], lab.base + "/login?user=alice")
    await app.navigate(s["id"], lab.base + "/me")
    await app.browser.drain()
    me_flow = find(app, "GET", "/me")
    assert app.flow_detail(me_flow["id"])["response"]["status"] == 200

    d = app.create_draft(from_flow=me_flow["id"])

    # as_is: exactly the captured Cookie header
    ex = await app.send(d["id"])
    assert ex["status"] == 200 and "sid=token-alice" in lab.last("/me").header("cookie")

    # none: no cookie at all
    app.update_draft(d["id"], {"cookie_mode": "none"})
    ex = await app.send(d["id"])
    assert ex["status"] == 401 and lab.last("/me").header("cookie") is None

    # session: the *current* jar of the chosen browser session. Log in as bob
    # in a second, isolated session and point the draft at it.
    s2 = await app.open_session("bob")
    await app.navigate(s2["id"], lab.base + "/login?user=bob")
    app.update_draft(d["id"], {"cookie_mode": "session", "session_id": s2["id"]})
    ex = await app.send(d["id"])
    assert json.loads(ex["hops"][0]["response"]["body_text"]) == {"user": "bob"}
    assert "sid=token-alice" not in lab.last("/me").header("cookie")

    # Sessions are isolated: alice is still alice.
    alice = {c["name"]: c["value"] for c in await app.browser.all_cookies(s["id"])}
    assert alice["sid"] == "token-alice"


# 7. Comparing two responses identifies the relevant differences
async def test_compare_responses(app, lab):
    s = await browse(app, lab)
    flow = find(app, "GET", "/api/items")
    d = app.create_draft(from_flow=flow["id"])
    app.update_draft(d["id"], {"url": lab.base + "/api/items?page=2"})
    ex = await app.send(d["id"])

    c = app.compare(f"flow:{flow['id']}", f"exec:{ex['id']}")
    assert c["summary"]["body_changed"] and not c["summary"]["status_changed"]
    paths = {ch["path"]: ch for ch in c["body"]["changes"]}
    assert paths["page"]["a"] == 1 and paths["page"]["b"] == 2
    assert "items[0].id" in paths
    # Date and similar headers are flagged as volatile, not as relevant changes.
    assert all(ch["volatile"] for ch in c["headers"]["changes"] if ch["name"] == "date")

    same = app.compare(f"flow:{flow['id']}", f"flow:{flow['id']}")
    assert same["body"]["identical"]


# 8. Many requests: the app keeps working and history is consistent
async def test_many_requests_history_consistent(app, lab):
    s = await app.open_session("load")
    page = app.browser.sessions[s["id"]].page
    await app.navigate(s["id"], lab.base + "/echo")
    await page.evaluate("""async () => {
        await Promise.all([...Array(150).keys()].map(i =>
            fetch('/echo?i=' + i, {method: 'POST', body: 'n=' + i})));
    }""")
    d = app.create_draft(fields={"url": lab.base + "/slow?s=0.05"})
    results = await asyncio.gather(*[app.send(d["id"]) for _ in range(30)])
    await app.browser.drain()

    posts = [r for r in app.query_flows(method="POST", limit=1000) if r["path"] == "/echo"]
    assert len(posts) == 150
    bodies = {app.flow(r["id"]).request.body for r in posts}
    assert bodies == {f"n={i}".encode() for i in range(150)}
    assert len(app.executions(d["id"])) == 30
    assert all(r["status"] == 200 for r in results)
    # Incremental fetch used by the UI returns nothing twice.
    first = app.query_flows(limit=50)
    rest = app.query_flows(after_seq=first[-1]["seq"], limit=1000)
    assert not {r["id"] for r in first} & {r["id"] for r in rest}


# Extra: compressed responses captured from the browser are stored decoded
async def test_browser_capture_of_compressed_responses(app, lab):
    s = await app.open_session("enc")
    for enc in ("gzip", "br", "zstd"):
        await app.navigate(s["id"], f"{lab.base}/compressed?enc={enc}")
    await app.browser.drain()
    rows = [r for r in app.query_flows(text="/compressed")]
    bodies = {json.loads(app.flow(r["id"]).response.body)["enc"] for r in rows}
    assert bodies == {"gzip", "br", "zstd"}
