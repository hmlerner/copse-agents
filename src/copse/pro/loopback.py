"""Browser sign-in for ``copse account login``: a loopback redirect
(RFC 8252) with PKCE (RFC 7636).

The CLI binds 127.0.0.1 on a free port, opens
``<base>/cli/authorize?client_id=copse-cli&redirect_uri=http://127.0.0.1:<port>/callback
&state=...&code_challenge=...&code_challenge_method=S256`` in the browser and
waits for the one redirect back. :class:`CallbackServer` answers exactly one
``GET /callback`` whose ``state`` matches (anything else gets a 4xx page and
is ignored), shows a small HTML page and hands the ``code`` (or the
``error``) back to :func:`copse.pro.auth.browser_login`, which exchanges it
at ``POST /cli/token`` together with the PKCE verifier.

:func:`can_open_browser` decides whether the browser flow is even worth
trying: stdout must be a terminal, the session must not be SSH, and on
Linux a display must be set. Otherwise (and whenever the callback doesn't
arrive in time) ``login`` falls back to the device-code flow.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import os
import re
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

CALLBACK_PATH = "/callback"
HOST = "127.0.0.1"
TIMEOUT = 300.0                                  # how long to wait for the redirect
STATE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


def can_open_browser(out=None, env: dict | None = None, platform: str | None = None) -> bool:
    """Whether a browser on this machine can plausibly be opened and reach
    us back: ``out`` (default stdout) is a terminal, this isn't an SSH
    session, and on Linux ``DISPLAY`` or ``WAYLAND_DISPLAY`` is set."""
    out = sys.stdout if out is None else out
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    try:
        if not out.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return False
    if platform.startswith("linux") and not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return False
    return True


def open_browser(url: str) -> bool:
    """Open ``url`` in the user's browser; False when that didn't work."""
    return bool(webbrowser.open(url))


# -- PKCE and the authorize URL ------------------------------------------------------------------


def pkce_pair() -> tuple[str, str]:
    """A fresh ``(code_verifier, code_challenge)``: 43 base64url chars each,
    the challenge being S256 of the verifier (RFC 7636 4.1-4.2)."""
    verifier = secrets.token_urlsafe(32)
    return verifier, pkce_challenge(verifier)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_state() -> str:
    return secrets.token_urlsafe(24)


def authorize_url(base: str, redirect_uri: str, state: str, challenge: str, client_id: str) -> str:
    query = urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    return f"{base}/cli/authorize?{query}"


# -- the callback server -------------------------------------------------------------------------


@dataclass(frozen=True)
class CallbackResult:
    code: str | None = None
    error: str | None = None
    description: str | None = None


def _sanitize(text: str | None, limit: int = 200) -> str | None:
    if text is None:
        return None
    return re.sub(r"[^\x20-\x7e]", "?", text)[:limit]


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font:16px/1.5 system-ui,sans-serif;margin:0;display:flex;min-height:100vh;
align-items:center;justify-content:center;background:#f6f7f4;color:#1f2a1f}}
main{{max-width:28rem;padding:2rem;text-align:center}}h1{{font-size:1.25rem;margin:0 0 .5rem}}
p{{margin:0;color:#4b574b}}</style></head>
<body><main><h1>{title}</h1><p>{text}</p></main></body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    server: "CallbackServer"
    protocol_version = "HTTP/1.0"

    def log_message(self, *a) -> None:      # never log query strings (they carry the code)
        pass

    def _reply(self, status: int, title: str, text: str) -> None:
        body = PAGE.format(title=html.escape(title), text=html.escape(text)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        u = urllib.parse.urlsplit(self.path)
        if u.path != CALLBACK_PATH:
            self._reply(404, "Not found", "There's nothing here.")
            return
        q = urllib.parse.parse_qs(u.query, keep_blank_values=True)

        def one(key: str) -> str | None:
            vals = q.get(key, [])
            return vals[0] if len(vals) == 1 else None

        state = one("state") or ""
        if not hmac.compare_digest(state.encode(), self.server.state.encode()) or self.server.done:
            self._reply(400, "copse sign-in failed",
                        "This isn't the sign-in copse is waiting for. Run `copse account login` again.")
            return
        error, code = one("error"), one("code")
        if error is not None:
            result = CallbackResult(error=_sanitize(error, 64), description=_sanitize(one("error_description")))
            title, text = ("copse sign-in cancelled" if error == "access_denied" else "copse sign-in failed",
                           "You can close this tab and run `copse account login` again.")
        elif code is not None and CODE_RE.match(code):
            result = CallbackResult(code=code)
            title, text = "copse is signed in.", "You can close this tab."
        else:
            self._reply(400, "copse sign-in failed", "The sign-in response was incomplete.")
            return
        self.server.finish(result)
        self._reply(200, title, text)


class CallbackServer(HTTPServer):
    """Listens on 127.0.0.1:<free port> for the single ``GET /callback`` of a
    browser sign-in. Use as a context manager: ``start`` then ``wait``."""

    allow_reuse_address = False

    def __init__(self, state: str) -> None:
        if not STATE_RE.match(state):
            raise ValueError("bad state")
        super().__init__((HOST, 0), _Handler)
        self.state = state
        self.result: CallbackResult | None = None
        self._event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def redirect_uri(self) -> str:
        return f"http://{HOST}:{self.port}{CALLBACK_PATH}"

    @property
    def done(self) -> bool:
        return self._event.is_set()

    def finish(self, result: CallbackResult) -> None:
        if not self._event.is_set():
            self.result = result
            self._event.set()

    def start(self) -> "CallbackServer":
        self._thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.1},
                                        daemon=True, name="copse-login-callback")
        self._thread.start()
        return self

    def wait(self, timeout: float = TIMEOUT) -> CallbackResult | None:
        """The callback's result, or None if none arrived within ``timeout``."""
        self._event.wait(timeout)
        return self.result

    def close(self) -> None:
        if self._thread is not None:
            self.shutdown()
            self._thread.join(5)
            self._thread = None
        self.server_close()

    def __enter__(self) -> "CallbackServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()
