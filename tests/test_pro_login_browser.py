"""Browser sign-in for ``copse account login``: loopback redirect + PKCE,
with the device flow as the fallback. The backend is mocked; the "browser"
is a function that GETs the loopback callback the way a redirect would."""
import base64
import hashlib
import io
import re
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

import pytest

from copse.pro import account, auth, credentials, loopback
from copse.pro.auth import AuthError
from pro_fixtures import BASE, backend, pro_env, signing_key, token  # noqa: F401 - fixtures
from test_pro_auth import Clock, device_backend

GOOD_CODE = "cac_" + "g" * 43


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@pytest.fixture
def store(tmp_path):
    return credentials.FileStore(tmp_path / "pro")


def cli_token(backend, code=GOOD_CODE):
    """Script ``POST /cli/token``: checks the PKCE proof against the challenge
    the fake browser saw in the authorize URL, then issues tokens."""
    def handler(form, headers):
        assert form["grant_type"] == "authorization_code" and form["client_id"] == "copse-cli"
        assert form["code"] == code
        assert form["redirect_uri"] == backend.redirect_uri
        verifier = form["code_verifier"]
        assert 43 <= len(verifier) <= 128 and re.fullmatch(r"[A-Za-z0-9_-]+", verifier)
        assert b64url(hashlib.sha256(verifier.encode()).digest()) == backend.challenge
        return 200, backend.issue()

    backend.routes["POST /cli/token"] = handler
    return backend


def get(url: str) -> int:
    """GET ``url`` like a browser following the redirect; the HTTP status."""
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def fake_browser(backend, *redirects, callback=True):
    """An ``open_url`` that checks the authorize URL and then "redirects":
    each item in ``redirects`` is a dict of callback query parameters (its
    ``state`` defaults to the right one) or a path to GET instead. With no
    items, one successful redirect carrying ``GOOD_CODE``."""
    backend.opened, backend.statuses = [], []
    redirects = list(redirects) or [{"code": GOOD_CODE}]

    def open_url(url):
        backend.opened.append(url)
        u = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        assert f"{u.scheme}://{u.netloc}{u.path}" == BASE + "/cli/authorize"
        assert q["client_id"] == "copse-cli" and q["code_challenge_method"] == "S256"
        assert re.fullmatch(r"http://127\.0\.0\.1:\d+/callback", q["redirect_uri"])
        assert loopback.STATE_RE.match(q["state"]) and len(q["code_challenge"]) == 43
        backend.challenge, backend.redirect_uri = q["code_challenge"], q["redirect_uri"]
        if not callback:
            return True
        origin = q["redirect_uri"].rsplit("/callback", 1)[0]
        for r in redirects:
            if isinstance(r, str):
                backend.statuses.append(get(origin + r))
                continue
            params = {"state": q["state"], **r}
            backend.statuses.append(get(q["redirect_uri"] + "?" + urllib.parse.urlencode(params)))
        return True

    return open_url


def login(backend, store, open_url, **kw):
    clock = Clock()
    shown = []
    ent = auth.login(auth.Client(BASE, backend), store, show=shown.append, sleep=clock.sleep,
                     clock=clock, browser=True, open_url=open_url, **kw)
    return ent, shown


# -- the browser flow ---------------------------------------------------------------------------


def test_browser_flow_logs_in_with_pkce(store, backend):
    cli_token(backend)
    ent, shown = login(backend, store, fake_browser(backend))
    assert ent.plan == "pro" and backend.statuses == [200]
    assert "didn't open, visit: " + backend.opened[0] in "\n".join(shown)
    creds = store.load()
    assert creds["refresh_token"] == "cpr_1" and creds["access_token"] == "at_1"
    assert creds["base_url"] == BASE and creds["entitlement"]
    assert backend.paths() == ["POST /cli/token", "GET /entitlement"]
    assert backend.calls[-1][2] == {"Authorization": "Bearer at_1"}


def test_state_mismatch_is_rejected_without_consuming_the_callback(store, backend):
    cli_token(backend)
    browser = fake_browser(backend, {"state": "x" * 20, "code": "cac_evil"},
                           {"state": "", "code": "cac_nostate"}, "/elsewhere", {"code": GOOD_CODE})
    ent, _ = login(backend, store, browser)
    assert ent.plan == "pro"
    assert backend.statuses == [400, 400, 404, 200]
    exchanges = [c for c in backend.calls if c[0] == "POST /cli/token"]
    assert len(exchanges) == 1 and exchanges[0][1]["code"] == GOOD_CODE


def test_access_denied_fails_without_a_token_exchange(store, backend):
    cli_token(backend)
    with pytest.raises(AuthError) as e:
        login(backend, store, fake_browser(backend, {"error": "access_denied"}))
    assert e.value.code == "access_denied" and backend.statuses == [200]
    assert backend.paths() == [] and store.load() is None


def test_other_callback_errors_are_sanitized(store, backend):
    cli_token(backend)
    with pytest.raises(AuthError) as e:
        login(backend, store, fake_browser(backend, {"error": "server_error",
                                                      "error_description": "boom\x1b[31m" + "y" * 500}))
    assert e.value.code == "server_error" and "\x1b" not in str(e.value) and len(str(e.value)) < 300


def test_refused_code_exchange_fails_the_login(store, backend):
    backend.routes["POST /cli/token"] = [(400, {"error": "invalid_grant"})]
    with pytest.raises(AuthError, match="refused") as e:
        login(backend, store, fake_browser(backend))
    assert e.value.code == "invalid_grant" and store.load() is None


def test_pkce_pair_and_state():
    verifier, challenge = loopback.pkce_pair()
    assert len(verifier) == 43 and re.fullmatch(r"[A-Za-z0-9_-]+", verifier)
    assert challenge == b64url(hashlib.sha256(verifier.encode()).digest()) and len(challenge) == 43
    assert loopback.pkce_pair()[0] != verifier
    assert loopback.STATE_RE.match(loopback.new_state())
    url = loopback.authorize_url(BASE, "http://127.0.0.1:4321/callback", "s" * 20, challenge, "copse-cli")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert url.startswith(BASE + "/cli/authorize?")
    assert q == {"client_id": "copse-cli", "redirect_uri": "http://127.0.0.1:4321/callback",
                 "state": "s" * 20, "code_challenge": challenge, "code_challenge_method": "S256"}


def test_callback_page_is_plain_html_with_no_script_allowed():
    with loopback.CallbackServer("s" * 20) as srv:
        req = urllib.request.Request(srv.redirect_uri + "?state=" + "s" * 20 + "&code=cac_x")
        with urllib.request.urlopen(req, timeout=5) as r:
            body, headers = r.read().decode(), r.headers
        assert "copse is signed in" in body and "<script" not in body
        assert headers["Content-Security-Policy"].startswith("default-src 'none'")
        assert headers["Cache-Control"] == "no-store"
        assert srv.wait(1) == loopback.CallbackResult(code="cac_x")
        # one-shot: a second valid-looking callback is refused
        assert get(srv.redirect_uri + "?state=" + "s" * 20 + "&code=cac_y") == 400
        assert srv.result.code == "cac_x"


# -- falling back to the device flow --------------------------------------------------------------


def test_timeout_falls_back_to_the_device_flow(store, backend):
    cli_token(backend)
    device_backend(backend, ["ISSUE"])
    ent, shown = login(backend, store, fake_browser(backend, callback=False), browser_timeout=0.05)
    assert ent.plan == "pro"
    assert any("time limit" in s and "device code" in s for s in shown)
    assert any("BCDF-GHJK" in s for s in shown)
    assert "POST /cli/token" not in backend.paths()
    assert backend.paths()[:2] == ["POST /device/authorize", "POST /device/token"]


def test_browser_that_cannot_open_falls_back_to_the_device_flow(store, backend):
    device_backend(backend, ["ISSUE"])
    for open_url in (lambda url: False, lambda url: 1 / 0):
        store.delete()
        backend.calls.clear()
        ent, shown = login(backend, store, open_url)
        assert ent.plan == "pro" and any("Couldn't open a browser" in s for s in shown)
        assert backend.paths()[0] == "POST /device/authorize"


def test_cannot_listen_falls_back_to_the_device_flow(store, backend, monkeypatch):
    def boom(state):
        raise OSError("no sockets")

    monkeypatch.setattr(loopback, "CallbackServer", boom)
    device_backend(backend, ["ISSUE"])
    ent, shown = login(backend, store, lambda url: pytest.fail("the browser must not be opened"))
    assert ent.plan == "pro" and any("Couldn't listen" in s for s in shown)


def test_login_without_browser_is_the_device_flow(store, backend):
    device_backend(backend, ["ISSUE"])
    shown = []
    clock = Clock()
    auth.login(auth.Client(BASE, backend), store, show=shown.append, sleep=clock.sleep, clock=clock,
               open_url=lambda url: pytest.fail("the browser must not be opened"))
    assert "BCDF-GHJK" in shown[0] and backend.paths()[0] == "POST /device/authorize"


# -- when a browser can open ------------------------------------------------------------------------


class TTY(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("out, env, platform, expected", [
    (TTY(), {}, "darwin", True),
    (io.StringIO(), {}, "darwin", False),
    (TTY(), {"SSH_CONNECTION": "1.2.3.4 1 5.6.7.8 22"}, "darwin", False),
    (TTY(), {"SSH_TTY": "/dev/pts/0"}, "darwin", False),
    (TTY(), {}, "linux", False),
    (TTY(), {"DISPLAY": ":0"}, "linux", True),
    (TTY(), {"WAYLAND_DISPLAY": "wayland-0"}, "linux", True),
    (TTY(), {"DISPLAY": ":0", "SSH_CONNECTION": "x"}, "linux", False),
    (TTY(), {}, "win32", True),
])
def test_can_open_browser(out, env, platform, expected):
    assert loopback.can_open_browser(out, env, platform) is expected


# -- `copse account login` ----------------------------------------------------------------------


def run(args, out=None, **kw):
    out, err = out or io.StringIO(), io.StringIO()
    acct = account.ProAccount("/repo", out=out, err=err, **kw)
    return acct.run(args), out.getvalue(), err.getvalue()


@pytest.fixture
def local_terminal(monkeypatch):
    """A terminal on this machine: no SSH, a display, no real sleeping."""
    for var in ("SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)


def test_account_login_opens_the_browser(store, backend, monkeypatch, local_terminal):
    cli_token(backend)
    monkeypatch.setattr(webbrowser, "open", fake_browser(backend))
    code, out, err = run(["login"], out=TTY(), store=store, transport=backend)
    assert code == 0, err
    assert "Opening your browser" in out and "visit: " + backend.opened[0] in out
    assert "plan pro" in out and "BCDF-GHJK" not in out
    assert store.load()["refresh_token"] == "cpr_1"


def test_account_login_over_ssh_uses_the_device_flow(store, backend, monkeypatch, local_terminal):
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 1 5.6.7.8 22")
    monkeypatch.setattr(webbrowser, "open", lambda url: pytest.fail("the browser must not be opened"))
    device_backend(backend, ["ISSUE"])
    code, out, err = run(["login"], out=TTY(), store=store, transport=backend)
    assert code == 0, err
    assert "BCDF-GHJK" in out and "Opening your browser" not in out and "plan pro" in out


def test_account_login_device_flag_forces_the_device_flow(store, backend, monkeypatch, local_terminal):
    monkeypatch.setattr(webbrowser, "open", lambda url: pytest.fail("the browser must not be opened"))
    device_backend(backend, ["ISSUE"])
    code, out, err = run(["login", "--device"], out=TTY(), store=store, transport=backend)
    assert code == 0, err
    assert "BCDF-GHJK" in out and "Opening your browser" not in out
    assert run(["status", "--device"], store=store, transport=backend)[0] == 2


def test_account_login_without_a_terminal_uses_the_device_flow(store, backend, monkeypatch, local_terminal):
    monkeypatch.setattr(webbrowser, "open", lambda url: pytest.fail("the browser must not be opened"))
    device_backend(backend, ["ISSUE"])
    code, out, _ = run(["login"], store=store, transport=backend)
    assert code == 0 and "BCDF-GHJK" in out


def test_account_login_denied_in_the_browser(store, backend, monkeypatch, local_terminal):
    cli_token(backend)
    monkeypatch.setattr(webbrowser, "open", fake_browser(backend, {"error": "access_denied"}))
    code, _, err = run(["login"], out=TTY(), store=store, transport=backend)
    assert code == 1 and "denied" in err and store.load() is None


def test_help_says_the_browser_opens():
    code, out, _ = run(["--help"])
    assert code == 0 and "opens your browser" in out and "--device" in out
    assert "device code" not in out.split("login", 1)[1].split("\n")[0]


def test_browser_timeout_default_is_five_minutes():
    assert auth.BROWSER_TIMEOUT == loopback.TIMEOUT == 300
