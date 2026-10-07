"""The daemon answers a request addressed to this machine, and nothing else.

It has no authentication, so what it can ask of a caller is where the request
says it is going. A page in the user's browser cannot read the admin key, but
it can send requests: to 127.0.0.1 directly, or — after its own domain is
re-pointed at 127.0.0.1 — to a name the browser believes is its own.
"""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from odoo_sheller.api import create_app
from odoo_sheller.guard import refusal
from odoo_sheller.registry import Registry

LOCAL = "http://127.0.0.1:8765"
WS = "ws://127.0.0.1:8765"


@pytest.fixture
def app(tmp_path):
    return create_app(registry=Registry(journal_root=tmp_path))


def headers(host=None, origin=None):
    raw = []
    if host is not None:
        raw.append((b"host", host.encode()))
    if origin is not None:
        raw.append((b"origin", origin.encode()))

    return raw


# --- the Host header ----------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1:8765",
        "127.0.0.1",
        "localhost:8765",
        "LOCALHOST:8765",
        "[::1]:8765",
        # `-p 127.0.0.1:9000:8765` moves the port, never the name.
        "127.0.0.1:9000",
        "127.5.5.5:8765",
    ],
)
def test_a_loopback_host_is_served(host):
    assert refusal(headers(host)) is None


@pytest.mark.parametrize(
    "host",
    [
        "evil.example.com",
        "evil.example.com:8765",
        "192.168.1.5:8765",
        "127.0.0.1.evil.example.com:8765",
        "localhost.evil.example.com",
        "evil.example.com#127.0.0.1",
        "evil@127.0.0.1:8765",
        "127.0.0.1:8765/path",
        # A page can fetch 0.0.0.0 and have it reach a local service.
        "0.0.0.0:8765",
        "[::]:8765",
        "2130706433",
        "127.1",
        "",
    ],
)
def test_a_host_that_is_not_this_machine_is_refused(host):
    assert refusal(headers(host)) == "foreign_host"


def test_a_request_with_no_host_is_refused():
    assert refusal(headers()) == "foreign_host"


# --- the Origin header --------------------------------------------------


def test_no_origin_is_served():
    """A client that is not a browser — the MCP server, curl — sends none."""
    assert refusal(headers("127.0.0.1:8765")) is None


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("127.0.0.1:8765", "http://127.0.0.1:8765"),
        ("localhost:8765", "http://localhost:8765"),
        ("LOCALHOST:8765", "http://localhost:8765"),
        ("[::1]:8765", "http://[::1]:8765"),
        ("127.0.0.1:9000", "http://127.0.0.1:9000"),
    ],
)
def test_the_page_the_daemon_served_may_call_it(host, origin):
    assert refusal(headers(host, origin)) is None


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example.com",
        "https://evil.example.com:8765",
        "null",
        "tauri://localhost",
        "file://",
        "http://127.0.0.1.evil.example.com:8765",
        # Loopback, but another page: a dev server, or anything else the user
        # has open on this machine, is not the daemon's own UI.
        "http://127.0.0.1:3000",
        "http://localhost:8765",
        "ftp://127.0.0.1:8765",
        "",
    ],
)
def test_a_page_from_anywhere_else_is_refused(origin):
    assert refusal(headers("127.0.0.1:8765", origin)) == "foreign_origin"


# --- through the app ----------------------------------------------------


@pytest.mark.parametrize("path", ["/health", "/api/sessions", "/api/journals", "/web", "/openapi.json"])
def test_every_route_answers_a_loopback_request(app, path):
    with TestClient(app, base_url=LOCAL) as client:
        assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", ["/health", "/api/sessions", "/api/journals", "/web", "/static/logo.svg", "/openapi.json"])
def test_every_route_refuses_a_foreign_host(app, path):
    with TestClient(app, base_url="http://evil.example.com:8765") as client:
        response = client.get(path)
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "foreign_host"


def test_a_foreign_host_cannot_open_a_session(app):
    with TestClient(app, base_url="http://evil.example.com:8765") as client:
        response = client.post("/api/sessions", json={"container": "c", "database": "d"})
    assert response.status_code == 403


def test_a_foreign_origin_is_refused_whatever_the_method(app):
    with TestClient(app, base_url=LOCAL, headers={"Origin": "http://evil.example.com"}) as client:
        for method, path in (("get", "/health"), ("post", "/api/probe"), ("delete", "/api/sessions/x")):
            response = getattr(client, method)(path)
            assert response.status_code == 403
            assert response.json()["detail"]["error"] == "foreign_origin"


def test_the_refusal_says_what_to_do(app):
    with TestClient(app, base_url="http://evil.example.com:8765") as client:
        detail = client.get("/health").json()["detail"]
    assert "127.0.0.1" in detail["recovery"]


# --- the WebSocket ------------------------------------------------------


@pytest.mark.parametrize("path", ["/ws/sessions", "/ws/sessions/s1"])
def test_a_page_on_another_site_cannot_listen(app, path):
    """A WebSocket is not subject to CORS: the handshake carries an Origin and
    nothing but the server decides whether to honour it."""
    page = {"Origin": "http://evil.example.com"}
    with (
        TestClient(app, base_url=LOCAL, headers=page) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(WS + path),
    ):
        pass


def test_a_websocket_to_a_foreign_host_is_refused(app):
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("ws://evil.example.com:8765/ws/sessions"),
    ):
        pass


def test_the_ui_can_listen(app):
    """Entering the context is the assertion: a refused handshake raises."""
    own_page = {"Origin": LOCAL}
    with (
        TestClient(app, base_url=LOCAL, headers=own_page) as client,
        client.websocket_connect(WS + "/ws/sessions"),
    ):
        pass
