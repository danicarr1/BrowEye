import json

import pytest

from broweye import diff, interop
from broweye.models import Flow, RequestDraft, RequestSpec, new_id
from broweye.repeater import Repeater
from broweye.lab import start_lab


@pytest.fixture
async def other_lab():
    """A second lab on a different port = a different origin."""
    lab, runner, base = await start_lab()
    lab.base = base
    yield lab
    await runner.cleanup()


def draft(**kw) -> RequestDraft:
    return RequestDraft(id=new_id(), name="t", **kw)


async def test_cross_origin_redirect_strips_credentials(lab, other_lab):
    d = draft(url=f"{lab.base}/redirect?to={other_lab.base}/echo", follow_redirects=True,
              headers=[("Cookie", "sid=secret"), ("Authorization", "Bearer secret"), ("X-Keep", "1")])
    ex = await Repeater().execute(d)
    assert [h.response.status for h in ex.hops] == [302, 200]
    # first origin got the credentials ...
    assert lab.last("/redirect").header("cookie") == "sid=secret"
    # ... the other origin did not
    got = other_lab.last("/echo")
    assert got.header("cookie") is None and got.header("authorization") is None
    assert got.header("x-keep") == "1"
    assert "Cookie header removed" in ex.hops[1].note


async def test_same_origin_redirect_keeps_cookie(lab):
    d = draft(url=f"{lab.base}/redirect?to=/echo", follow_redirects=True, headers=[("Cookie", "sid=1")])
    await Repeater().execute(d)
    assert lab.last("/echo").header("cookie") == "sid=1"


async def test_configured_auth_scoped_to_origin(lab, other_lab):
    d = draft(url=f"{lab.base}/redirect?to={other_lab.base}/echo", follow_redirects=True,
              auth_type="bearer", auth_secret="tok")
    ex = await Repeater().execute(d)
    assert lab.last("/redirect").header("authorization") == "Bearer tok"
    assert other_lab.last("/echo").header("authorization") is None
    assert "tok" not in json.dumps(ex.draft)  # secret redacted in stored snapshot


async def test_redirect_method_semantics(lab):
    d = draft(method="POST", url=f"{lab.base}/redirect-post?to=/echo", follow_redirects=True,
              headers=[("Content-Type", "text/plain")], body=b"keep me")
    ex = await Repeater().execute(d)
    assert lab.last("/echo").method == "POST" and lab.last("/echo").body == b"keep me"  # 307
    assert ex.hops[0].response.status == 307


async def test_redirects_not_followed_by_default(lab):
    ex = await Repeater().execute(draft(url=f"{lab.base}/redirect?to=/echo"))
    assert len(ex.hops) == 1 and ex.hops[0].response.status == 302


async def test_connection_error_is_reported_not_raised():
    ex = await Repeater().execute(draft(url="http://127.0.0.1:9/", timeout_s=2))
    assert ex.error and ex.hops[0].response is None


async def test_binary_response_preserved(lab):
    ex = await Repeater().execute(draft(url=f"{lab.base}/binary"))
    assert ex.final.response.body == bytes(range(256))


async def test_draft_from_flow_is_a_copy():
    req = RequestSpec("POST", "http://x/a", [("A", "1")], b"body")
    flow = Flow(id="f", session_id="s", request=req, response=None, started_at=0)
    d = RequestDraft.from_flow(flow)
    d.headers.append(("B", "2"))
    d.body = b"changed"
    assert flow.request.headers == [("A", "1")] and flow.request.body == b"body"


def test_curl_roundtrip():
    d = draft(method="POST", url="https://example.test/a?b=1",
              headers=[("Content-Type", "application/json"), ("X-It", "it's")], body=b'{"a": "b c"}')
    cmd = interop.to_curl(d, redact=False)
    back = interop.from_curl(cmd)
    assert (back.method, back.url, back.body) == (d.method, d.url, d.body)
    assert back.headers == d.headers


def test_curl_export_redacts_credentials():
    d = draft(url="https://x.test/", headers=[("Cookie", "sid=1"), ("Authorization", "Basic abc")])
    cmd = interop.to_curl(d)
    assert "sid=1" not in cmd and "abc" not in cmd


def test_curl_import_options():
    d = interop.from_curl("curl -k -L -u bob:pw -b 'a=1' -d 'x=1' https://x.test/p")
    assert d.method == "POST" and not d.verify_tls and d.follow_redirects
    assert (d.auth_type, d.auth_user, d.auth_secret) == ("basic", "bob", "pw")
    assert ("Cookie", "a=1") in d.headers
    with pytest.raises(interop.CurlParseError):
        interop.from_curl("curl --proxy http://p https://x.test/")


def test_har_export_shape():
    req = RequestSpec("GET", "http://x.test/a?q=1", [("Accept", "*/*")], None)
    flow = Flow(id="f", session_id="s", request=req, response=None, started_at=0, duration_ms=5)
    har = interop.to_har([flow])
    e = har["log"]["entries"][0]
    assert har["log"]["version"] == "1.2"
    assert e["request"]["queryString"] == [{"name": "q", "value": "1"}]


def test_json_diff_ignores_key_order():
    r = diff.diff_bodies(b'{"a":1,"b":[1,2]}', b'{"b":[1,2], "a":1}')
    assert r["kind"] == "json" and r["semantically_equal"]
    r = diff.diff_bodies(b'{"a":1}', b'{"a":"1"}')
    assert r["changes"] == [{"path": "a", "kind": "changed", "a": 1, "b": "1"}]


def test_text_diff():
    r = diff.diff_bodies(b"line1\nline2\n", b"line1\nLINE2\n")
    assert r["kind"] == "text" and "-line2" in r["diff"] and "+LINE2" in r["diff"]


@pytest.mark.parametrize("enc", ["gzip", "br", "zstd"])
async def test_compressed_responses_are_decoded(lab, enc):
    ex = await Repeater().execute(draft(url=f"{lab.base}/compressed?enc={enc}",
                                        headers=[("Accept-Encoding", "gzip, deflate, br, zstd")]))
    assert json.loads(ex.final.response.body) == {"compressed": True, "enc": enc}



async def test_post_302_becomes_get_without_body(lab):
    d = draft(method="POST", url=f"{lab.base}/redirect-post-302?to=/echo", follow_redirects=True,
              headers=[("Content-Type", "text/plain")], body=b"dropped")
    ex = await Repeater().execute(d)
    assert [h.request.method for h in ex.hops] == ["POST", "GET"]
    got = lab.last("/echo")
    assert got.method == "GET" and got.body == b"" and got.header("content-type") is None
