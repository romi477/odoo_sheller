"""Answer only a request that was addressed to this machine.

The daemon has no authentication, and a page in the user's browser can still
send it requests — to `127.0.0.1` directly, or to a name of the page's own
that its DNS has been pointed at `127.0.0.1` (DNS rebinding). Neither can read
the admin key. What each has to get wrong is something the daemon can see:

- **Host.** A request meant for this daemon names it: `127.0.0.1`,
  `localhost` or `[::1]`, on whatever port a `-p` mapping chose. A rebound
  page names its own domain. `0.0.0.0` is refused too — a page can fetch it,
  and on some systems it reaches a local service — as is a request with no
  Host at all.
- **Origin.** A browser puts the page's origin on every cross-site request,
  WebSocket handshakes included, and a WebSocket is not subject to CORS: for
  one, only this check stands between a page and the daemon's events. When
  present, the Origin must be this daemon's own — the same authority as Host,
  not just *a* loopback one, since a dev server on another local port is a
  different page. The UI, in a browser or in the desktop app's frame, is
  served by the daemon and so always satisfies this. A client that is not a
  browser — the MCP server, curl, the app's supervisor — sends no Origin.

This is independent of every recipe and key the daemon checks after it.
"""

import ipaddress
import re

from starlette.responses import JSONResponse
from starlette.websockets import WebSocketClose

# `name`, `name:port`, `[v6]` or `[v6]:port`, and nothing else: userinfo, a
# path or a fragment in a Host header are not a hostname that happens to
# contain a loopback address, they are a malformed request.
_AUTHORITY = r"(?P<name>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::[0-9]{1,5})?"
AUTHORITY_RE = re.compile(_AUTHORITY)
ORIGIN_RE = re.compile(rf"https?://(?P<authority>{_AUTHORITY})")

FOREIGN_HOST = "foreign_host"
FOREIGN_ORIGIN = "foreign_origin"

MESSAGES = {
    FOREIGN_HOST: (
        "this daemon answers only requests addressed to 127.0.0.1, localhost "
        "or [::1]; this one named another host"
    ),
    FOREIGN_ORIGIN: (
        "this daemon answers a browser only from its own page; this request "
        "came from another origin"
    ),
}


def _is_loopback(name: str) -> bool:
    name = name.lower()
    if name == "localhost":

        return True
    try:

        return ipaddress.ip_address(name.strip("[]")).is_loopback
    except ValueError:

        return False


def refusal(headers) -> str | None:
    """Why a request is refused — `foreign_host` or `foreign_origin` — or None.

    `headers` is an ASGI scope's: pairs of lowercase bytes names and bytes
    values. A header sent twice is ambiguous, and so refused.
    """
    hosts, origins = [], []
    for name, value in headers:
        if name == b"host":
            hosts.append(value.decode("latin-1"))
        elif name == b"origin":
            origins.append(value.decode("latin-1"))
    if len(hosts) != 1:

        return FOREIGN_HOST
    host = AUTHORITY_RE.fullmatch(hosts[0])
    if host is None or not _is_loopback(host["name"]):

        return FOREIGN_HOST
    if not origins:

        return None
    origin = ORIGIN_RE.fullmatch(origins[0]) if len(origins) == 1 else None
    if origin is None or origin["authority"].lower() != hosts[0].lower():

        return FOREIGN_ORIGIN

    return None


class LoopbackOnly:
    """ASGI middleware: HTTP and WebSocket alike, before any route runs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            refused = refusal(scope["headers"])
            if refused:
                if scope["type"] == "websocket":
                    # Closed before accept, which a server answers as a 403
                    # to the handshake; the page learns nothing it can read.
                    await WebSocketClose(code=1008)(scope, receive, send)
                else:
                    response = JSONResponse(
                        {
                            "detail": {
                                "error": refused,
                                "message": MESSAGES[refused],
                                "recovery": (
                                    "open the UI at http://127.0.0.1:<port>/web, "
                                    "and call the API from a client that sends no "
                                    "Origin, or from that page"
                                ),
                            }
                        },
                        status_code=403,
                    )
                    await response(scope, receive, send)

                return
        await self.app(scope, receive, send)
