"""Settings sync: ~/.copse/config.json's user-wide keys follow a Pro person
across machines through /me/settings."""

import io
import json
import time

import pytest

from copse import config
from copse.pro import account, auth, credentials, settings_sync
from pro_fixtures import (  # noqa: F401 -- fixtures
    BASE, backend, claims, pro_env, sign, signing_key, token,
)


@pytest.fixture
def store(backend, copse_home):
    s = credentials.FileStore(copse_home / "pro")
    login(backend, s)
    return s


def login(backend, store, features=("settings_sync",)):
    t = backend.issue()
    store.save({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                "access_expires_at": time.time() + 900, "base_url": BASE,
                "entitlement": sign(backend.key, claims(features=list(features)))})


def client(backend):
    return auth.Client(BASE, backend)


def write_cfg(**kw):
    p = config.user_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(kw))


def read_cfg():
    return json.loads(config.user_config_path().read_text())


def puts(backend):
    return [c for c in backend.calls if c[0] == "PUT /me/settings"]


def test_only_allowlisted_keys_are_sent(backend, store):
    write_cfg(delegation="fast", pr_footer=False, max_agents=3, learning="off",
              plugins={"x": "y"}, sidebar="x" * 33, secret_token="hunter2")
    r = settings_sync.push(client(backend), store)
    assert r.action == "pushed"
    (_, form, _), = puts(backend)
    assert form["settings"] == {"delegation": "fast", "pr_footer": False, "max_agents": 3}
    assert backend.settings == {"delegation": "fast", "pr_footer": False, "max_agents": 3}


def test_pull_applies_newer_server_values_and_keeps_local_keys(backend, store):
    write_cfg(delegation="balanced", learning="off")
    settings_sync.push(client(backend), store)
    backend.settings = {"delegation": "fast", "sidebar": "bottom", "evil": "x"}
    backend.settings_at += 10
    r = settings_sync.pull(client(backend), store)
    assert r.action == "pulled"
    assert read_cfg() == {"delegation": "fast", "learning": "off", "sidebar": "bottom"}
    assert r.changes["delegation"] == ("balanced", "fast")
    assert settings_sync.pull(client(backend), store).action == "unchanged"


def test_first_pull_on_a_new_machine_adopts_the_account_settings(backend, store):
    backend.settings, backend.settings_at = {"sidebar": "bottom"}, time.time()
    assert settings_sync.pull(client(backend), store).action == "pulled"
    assert read_cfg() == {"sidebar": "bottom"}
    assert not puts(backend)


def test_local_change_since_last_sync_is_pushed_not_overwritten(backend, store):
    write_cfg(delegation="balanced")
    settings_sync.push(client(backend), store)
    write_cfg(delegation="conservative")                 # changed here since
    backend.settings_at += 10                            # server also moved on
    backend.settings = {"delegation": "fast"}
    r = settings_sync.pull(client(backend), store)
    assert r.action == "pushed"
    assert backend.settings == {"delegation": "conservative"}
    assert read_cfg() == {"delegation": "conservative"}


def test_push_from_a_fresh_machine_keeps_the_servers_other_keys(backend, store):
    backend.settings, backend.settings_at = {"sidebar": "bottom"}, time.time()
    write_cfg(delegation="fast")
    settings_sync.push(client(backend), store)
    assert backend.settings == {"sidebar": "bottom", "delegation": "fast"}
    assert read_cfg() == {"sidebar": "bottom", "delegation": "fast"}


def test_state_file_is_private(backend, store, copse_home):
    write_cfg(delegation="fast")
    settings_sync.push(client(backend), store)
    f = copse_home / "pro" / "settings-sync.json"
    assert f.stat().st_mode & 0o777 == 0o600


def test_unentitled_sends_nothing(backend, store):
    login(backend, store, features=("learning",))
    write_cfg(delegation="fast")
    for fn in (settings_sync.push, settings_sync.pull):
        assert fn(client(backend), store).action == "skipped"
    assert not [c for c in backend.calls if "settings" in c[0]]


def test_server_403_is_skipped_quietly(backend, store):
    backend.settings_allowed = False
    write_cfg(delegation="fast")
    r = settings_sync.pull(client(backend), store)
    assert r.action == "skipped"
    assert read_cfg() == {"delegation": "fast"}


def test_airgap_sends_nothing(backend, store, monkeypatch):
    from copse import airgap

    monkeypatch.setattr(airgap, "enabled", lambda: True)
    write_cfg(delegation="fast")
    assert settings_sync.push(client(backend), store).action == "skipped"
    assert settings_sync.pull(client(backend), store).action == "skipped"
    assert not [c for c in backend.calls if "settings" in c[0]]


def test_offline_never_raises_and_catches_up_later(backend, store):
    write_cfg(delegation="fast")
    backend.routes["PUT /me/settings"] = [auth.TransportError("down")]
    backend.routes["GET /me/settings"] = [auth.TransportError("down")]
    assert settings_sync.push(client(backend), store).action == "skipped"
    assert settings_sync.pull(client(backend), store).action == "skipped"
    assert read_cfg() == {"delegation": "fast"}


def test_not_logged_in_never_raises(copse_home):
    write_cfg(delegation="fast")
    assert settings_sync.pull().action == "skipped"
    assert settings_sync.push().action == "skipped"
    settings_sync.push_soon()


def test_set_user_pushes_when_entitled(backend, store, monkeypatch):
    sent = []
    monkeypatch.setattr(settings_sync, "push_soon", lambda: sent.append(1))
    config.set_user("delegation", "fast")
    config.set_user("learning", "off")                   # not synced
    assert sent == [1]


def test_set_user_survives_a_failing_sync(copse_home, monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(settings_sync, "push_soon", boom)
    config.set_user("delegation", "fast")
    assert read_cfg() == {"delegation": "fast"}


def test_account_sync_prints_what_changed(backend, store):
    backend.settings, backend.settings_at = {"delegation": "fast"}, time.time()
    out = io.StringIO()
    rc = account.ProAccount(store=store, transport=backend, out=out).run(["sync"])
    assert rc == 0
    assert "delegation: - -> fast" in out.getvalue()
    assert read_cfg() == {"delegation": "fast"}
