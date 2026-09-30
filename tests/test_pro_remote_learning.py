"""The cloud learner: what it sends, what it never sends, and how it falls
back (to an installed local learner, else to no suggestion)."""
import base64
import hashlib
import io
import json
import re
import time
from importlib.metadata import entry_points

import pytest

from copse import learning as copse_learning
from copse import plugins
from copse.config import RepoConfig
from copse.learning import Outcome, TaskInfo
from copse.pro import auth, credentials
from copse.pro import learning as cloud
from copse.pro.learning import CloudLearner
from copse.pro.orgkey import OrgKey
from pro_fixtures import (  # noqa: F401 - fixtures
    BASE, SpyLocal, backend, claims, fixed_identity, pro_env, sign, signing_key, token,
)

REPO = "/work/secret-client-project"
TEXT = "Fix the crash in src/zebra_module.py on branch feat/zebra-hotfix"
FILES = ("src/zebra_module.py", "tests/test_zebra.py")
AGENT = "worker-zebra-7"
FORBIDDEN = ("zebra", "src/", "tests/", "secret-client-project", "/work", "feat/", AGENT,
             "claude", "opus", "Fix the crash")
COSTS = {"developer": 2, "reviewer": 3, "cheap": 0}


def task(agent_id=AGENT, profile="developer", weight="heavy", text=TEXT, files=FILES):
    return TaskInfo(repo_root=REPO, task=text, files=tuple(files), weight=weight,
                    agent_id=agent_id, profile=profile, provider="claude", model="claude-opus-4",
                    started_at=time.time() - 60)


@pytest.fixture
def remote(backend):
    """The fake backend plus the hosted-learning endpoints."""
    backend.records, backend.suggests = [], []

    def record(form, headers):
        if not backend._bearer(headers):
            return 401, {"error": "invalid_token"}
        backend.records.append(dict(form))
        return 200, {"recorded": True, "applied": form["event"] == "merged"}

    def suggest(form, headers):
        if not backend._bearer(headers):
            return 401, {"error": "invalid_token"}
        backend.suggests.append(dict(form))
        return 200, {"profile": form["candidates"][0]}

    backend.routes["POST /learning/record"] = record
    backend.routes["POST /learning/suggest"] = suggest
    return backend


def login_as(backend, store, features=("learning",)):
    t = backend.issue()
    store.save({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                "access_expires_at": time.time() + 900, "base_url": BASE,
                "entitlement": sign(backend.key, claims(features=list(features)))})


@pytest.fixture
def parts(tmp_path, remote, fixed_identity):
    store = credentials.FileStore(tmp_path / "pro")
    secrets_ = credentials.FileStore(tmp_path / "pro", account="learning-keys")
    local = SpyLocal()
    login_as(remote, store)
    return store, secrets_, local


def make(parts, backend, **kw):
    store, secrets_, local = parts
    kw.setdefault("cost", lambda n: COSTS.get(n, 2))
    return CloudLearner(REPO, kw.pop("local", local), client=auth.Client(BASE, backend), store=store,
                        key_store=secrets_, start_thread=kw.pop("start_thread", False), **kw)


def no_forbidden(body):
    blob = json.dumps(body)
    for s in FORBIDDEN:
        assert s not in blob, s


# -- payloads ---------------------------------------------------------------------------------


def test_record_payload_keys_and_contents(parts, remote):
    lr = make(parts, remote)
    lr.record(task(), Outcome("review", approved=False))
    lr.record(task(), Outcome("escalated"))
    lr.record(task(), Outcome("merged", checks_passed=True, tokens=1234, wall_seconds=61.5))
    lr.flush()
    review, escalated, merged = remote.records
    assert set(review) == {"org_id", "key_id", "repo_key", "agent_ref", "profile", "weight", "features", "event",
                           "approved", "checks_passed", "review_rounds", "escalations",
                           "tokens", "wall_seconds"}
    assert set(escalated) == set(merged) == set(review) - {"approved"}
    assert review["approved"] is False and review["event"] == "review"
    assert (review["org_id"], review["key_id"]) == ("org_1", "k1")
    assert merged == {**merged, "profile": "developer", "weight": "heavy", "event": "merged",
                      "checks_passed": True, "review_rounds": 1, "escalations": 1,
                      "tokens": 1234, "wall_seconds": 61.5}
    for body in remote.records:
        no_forbidden(body)
        assert set(body) <= cloud.RECORD_KEYS


def test_review_rounds_come_from_the_local_ledger_when_there_is_one(parts, remote):
    class Row:
        review_rounds, escalations = 4, 2

    class Ledger:
        def get(self, agent_id):
            return Row() if agent_id == AGENT else None

    parts[2].store = Ledger()
    lr = make(parts, remote)
    lr.record(task(), Outcome("merged"))
    lr.flush()
    assert (remote.records[0]["review_rounds"], remote.records[0]["escalations"]) == (4, 2)


def test_features_are_only_kind_and_size_one_hots(parts, remote):
    lr = make(parts, remote)
    lr.record(task(), Outcome("merged"))
    lr.flush()
    f = remote.records[0]["features"]
    assert set(f) == {"kind_bugfix", "kind_refactor", "kind_docs", "kind_feature", "kind_test",
                      "size_small", "size_medium", "size_large"}
    assert all(isinstance(v, bool) for v in f.values())
    assert f["kind_bugfix"] and f["size_small"] and sum(f.values()) == 2
    other = cloud.one_hot(task(text="Do the thing"))
    assert not any(v for k, v in other.items() if k.startswith("kind_"))


def test_suggest_payload_keys_and_costs(parts, remote):
    lr = make(parts, remote)
    assert lr.suggest(task(), ["developer", "reviewer", "cheap"]) == "developer"
    (body,) = remote.suggests
    assert set(body) == {"org_id", "key_id", "repo_key", "weight", "features", "candidates",
                         "candidate_cost"}
    assert (body["org_id"], body["key_id"]) == ("org_1", "k1")
    assert body["candidates"] == ["developer", "reviewer", "cheap"]
    assert body["candidate_cost"] == {"developer": 2, "reviewer": 3, "cheap": 0}
    assert body["weight"] == "heavy"
    no_forbidden(body)
    assert parts[2].suggested == 0


def test_undeclared_or_unknown_weight_is_null(parts, remote):
    lr = make(parts, remote)
    lr.suggest(task(weight=None), ["a", "b"])
    lr.suggest(task(weight="enormous"), ["a", "b"])
    assert [b["weight"] for b in remote.suggests] == [None, None]


def test_unsafe_profile_names_are_never_sent(parts, remote):
    lr = make(parts, remote)
    lr.record(task(profile="my profile/../x"), Outcome("merged"))
    lr.flush()
    assert remote.records == []
    assert lr.suggest(task(), ["developer", "/etc/passwd"]) == "/etc/passwd"   # local answered
    assert remote.suggests == []


def test_both_local_and_remote_record(parts, remote):
    lr = make(parts, remote)
    lr.record(task(), Outcome("merged", checks_passed=True))
    lr.flush()
    assert parts[2].done(AGENT)
    assert len(remote.records) == 1


def test_records_are_sent_by_the_background_thread(parts, remote):
    lr = make(parts, remote, start_thread=True)
    lr.record(task(), Outcome("merged"))
    assert lr.flush(5)
    assert len(remote.records) == 1


def test_record_does_not_block_on_a_slow_backend(parts, remote):
    remote.routes["POST /learning/record"] = lambda f, h: (time.sleep(1), (200, {}))[1]
    lr = make(parts, remote, start_thread=True)
    t0 = time.monotonic()
    for _ in range(3):
        lr.record(task(), Outcome("review", approved=True))
    assert time.monotonic() - t0 < 0.5


def test_the_record_queue_is_bounded(parts, remote, monkeypatch):
    monkeypatch.setattr(cloud, "QUEUE_SIZE", 3)
    lr = make(parts, remote)
    for i in range(5):
        lr.record(task(agent_id=f"w{i}"), Outcome("merged"))
    assert lr.queue.qsize() == 3 and lr.dropped == 2
    assert parts[2].done("w4")      # still recorded locally


def test_cost_rank_guesses_from_the_profile(tmp_path):
    from copse.profiles import load_profile

    assert cloud.cost_rank("no-such-profile", str(tmp_path)) == 2
    for name in ("developer", "developer-heavy", "developer-local"):
        try:
            p = load_profile(name, str(tmp_path))
        except Exception:  # noqa: BLE001 - not a built-in on this copse
            continue
        rank = cloud.cost_rank(name, str(tmp_path))
        assert 0 <= rank <= 3
        if p.base_url and "localhost" in p.base_url:
            assert rank == 0


# -- repo_key and agent_ref ---------------------------------------------------------------------------


def test_repo_key_is_the_org_keyed_hmac_of_the_repo_identity(parts, remote, fixed_identity):
    lr = make(parts, remote)
    lr.record(task(), Outcome("merged"))
    lr.suggest(task(), ["a", "b"])
    lr.flush()
    key = remote.records[0]["repo_key"]
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert remote.suggests[0]["repo_key"] == key
    org_key = OrgKey("org_1", *remote.org_key("org_1"))
    assert key == org_key.repo_key(fixed_identity)
    assert key != hashlib.sha256(REPO.encode()).hexdigest()
    # the key is fetched once and cached (by org and key_id) in the credential store
    assert remote.key_fetches == ["org_1"]
    assert parts[1].load()["keys"]["org_1"]["key_id"] == "k1"
    again = make(parts, remote)
    again.record(task(), Outcome("merged"))
    again.flush()
    assert remote.key_fetches == ["org_1"]
    # the key itself never leaves
    raw = base64.b64encode(remote.org_key("org_1")[1]).decode()
    assert raw not in json.dumps(remote.records + remote.suggests)


def test_agent_ref_is_a_truncated_org_hmac(parts, remote):
    lr = make(parts, remote)
    lr.record(task(), Outcome("merged"))
    lr.flush()
    ref = remote.records[0]["agent_ref"]
    assert re.fullmatch(r"[0-9a-f]{32}", ref)
    assert ref == OrgKey("org_1", *remote.org_key("org_1")).agent_ref(AGENT)


def test_the_legacy_install_secret_is_never_used(parts, remote, tmp_path):
    legacy = credentials.FileStore(tmp_path / "pro", account=credentials.LEARNING_SECRET)
    legacy.save({"secret": "ab" * 32})
    assert cloud.install_secret(legacy) == bytes.fromhex("ab" * 32)
    remote.key_status = (403, {"error": "entitlement_required"})
    lr = make(parts, remote)
    assert lr.suggest(task(), ["developer", "reviewer"]) == "reviewer"
    lr.record(task(), Outcome("merged"))
    lr.flush()
    assert remote.records == [] and remote.suggests == []


def test_no_org_key_means_local_only(parts, remote):
    remote.key_status = (403, {"error": "forbidden"})
    fallback_case(parts, remote)
    assert remote.records == [] and remote.suggests == []


# -- fallbacks ----------------------------------------------------------------------------------------


def fallback_case(parts, remote):
    lr = make(parts, remote)
    pick = lr.suggest(task(), ["developer", "reviewer"])
    lr.record(task(), Outcome("merged"))
    lr.flush()
    assert pick == "reviewer" and parts[2].suggested == 1        # local answered
    assert parts[2].done(AGENT)                                   # local recorded
    return lr


def test_unentitled_stays_local_without_calling_the_backend(parts, remote):
    login_as(remote, parts[0], features=("autopilot",))
    calls = len(remote.calls)
    fallback_case(parts, remote)
    assert len(remote.calls) == calls


def test_not_logged_in_stays_local(parts, remote):
    parts[0].delete()
    fallback_case(parts, remote)
    assert remote.records == [] and remote.suggests == []


def test_offline_falls_back_to_local(parts, remote):
    remote.routes["POST /learning/suggest"] = [auth.TransportError("down")]
    lr = fallback_case(parts, remote)
    assert not lr.active()     # backs off instead of retrying every call
    assert remote.records == []


@pytest.mark.parametrize("status,error", [(429, "rate_limited"), (429, "daily_limit"),
                                          (403, "entitlement_required"), (500, "internal_error"),
                                          (422, "invalid_request")])
def test_server_refusals_fall_back_to_local(parts, remote, status, error):
    remote.routes["POST /learning/suggest"] = [(status, {"error": error})]
    lr = fallback_case(parts, remote)
    if status in (429, 403):
        assert not lr.active()


def test_slow_suggest_falls_back_within_the_timeout(parts, remote, monkeypatch):
    monkeypatch.setattr(cloud, "SUGGEST_TIMEOUT", 0.3)
    remote.routes["POST /learning/suggest"] = lambda f, h: (time.sleep(2), (200, {"profile": "developer"}))[1]
    lr = make(parts, remote)
    t0 = time.monotonic()
    assert lr.suggest(task(), ["developer", "reviewer"]) == "reviewer"
    assert time.monotonic() - t0 < 1.0


def test_suggest_timeout_is_at_most_two_seconds():
    assert cloud.SUGGEST_TIMEOUT <= 2.0


def test_a_profile_outside_the_candidates_is_ignored(parts, remote):
    remote.routes["POST /learning/suggest"] = [(200, {"profile": "rogue"})]
    assert make(parts, remote).suggest(task(), ["developer", "reviewer"]) == "reviewer"


def test_errors_never_reach_copse(parts, remote, monkeypatch):
    lr = make(parts, remote)

    def boom(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(lr.local, "record", boom)
    monkeypatch.setattr(cloud, "record_payload", boom)
    monkeypatch.setattr(cloud, "suggest_payload", boom)
    lr.record(task(), Outcome("merged"))
    assert lr.suggest(task(), ["developer", "reviewer"]) == "reviewer"
    monkeypatch.setattr(lr.local, "suggest", boom)
    assert lr.suggest(task(), ["developer", "reviewer"]) is None
    assert "cloud learning" in lr.report()


def test_report_says_whether_cloud_is_active(parts, remote):
    assert make(parts, remote).report().startswith("cloud learning: active")
    login_as(remote, parts[0], features=())
    assert "inactive" in make(parts, remote).report()


# -- without a local learner (copse without copse-pro) -----------------------------------------------


def test_no_local_learner_means_no_suggestion_when_hosted_is_unavailable(parts, remote):
    remote.routes["POST /learning/suggest"] = [auth.TransportError("down")]
    lr = make(parts, remote, local=None)
    assert lr.local is None
    assert lr.suggest(task(), ["developer", "reviewer"]) is None
    lr.record(task(), Outcome("review", approved=True))
    lr.record(task(), Outcome("merged"))
    assert "no local learner" in lr.report()


def test_no_local_learner_still_records_and_suggests_remotely(parts, remote):
    lr = make(parts, remote, local=None)
    assert lr.suggest(task(), ["developer", "reviewer"]) == "developer"
    lr.record(task(), Outcome("review", approved=True))
    lr.record(task(), Outcome("merged"))
    lr.flush()
    assert [r["event"] for r in remote.records] == ["review", "merged"]
    assert remote.records[-1]["review_rounds"] == 1


def test_the_local_fallback_is_the_installed_local_plugin(monkeypatch, tmp_path):
    spy = SpyLocal()

    class EP:
        name, value = "local", "copse_pro.learning:make"

        def load(self):
            return lambda repo_root: spy

    assert cloud.local_plugin(str(tmp_path)) is None      # nothing named "local" installed here
    assert CloudLearner(str(tmp_path), start_thread=False).local is None
    real = plugins.entry_points
    monkeypatch.setattr(plugins, "entry_points",
                        lambda group: [EP()] if group == plugins.LEARNING else real(group=group))
    plugins.reset()
    assert cloud.local_plugin(str(tmp_path)) is spy
    assert CloudLearner(str(tmp_path), start_thread=False).local is spy


# -- wiring --------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_plugins():
    plugins.reset()
    yield
    plugins.reset()


def test_cloud_entry_point_loads_through_copse():
    assert [e.value for e in entry_points(group="copse.learning") if e.name == "cloud"] == \
        ["copse.pro.learning:make"]
    p = copse_learning.plugin(RepoConfig(learning="cloud"), REPO)
    assert isinstance(p, CloudLearner) and p.local is None


def test_learning_defaults_to_auto_which_is_off_until_entitled(remote, tmp_path):
    cfg = RepoConfig()
    assert cfg.learning == "auto"
    assert plugins.learning_name(cfg) == "off"
    assert copse_learning.plugin(cfg, str(tmp_path)) is None
    from copse.pro import license

    store = credentials.default_store()
    login_as(remote, store, features=("autopilot",))
    license.clear_cache()          # as `copse account login` does
    assert plugins.learning_name(cfg) == "off"           # logged in, but no hosted learning
    login_as(remote, store)
    license.clear_cache()
    assert plugins.learning_name(cfg) == "cloud"
    p = copse_learning.plugin(cfg, str(tmp_path))
    assert isinstance(p, CloudLearner)
    assert copse_learning.plugin(RepoConfig(learning="off"), str(tmp_path)) is None
    assert plugins.learning_name(RepoConfig(learning="other")) == "other"


def test_copse_learning_command_explains_auto(repo, monkeypatch):
    from typer.testing import CliRunner

    from copse.cli import app

    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["learning"])
    assert res.exit_code == 0, res.output
    assert "copse Pro" in res.output and "copse account status" in res.output


def test_account_status_shows_cloud_learning(parts, remote):
    from copse.pro import account

    out = io.StringIO()
    account.ProAccount(REPO, store=parts[0], transport=remote, out=out).run(["status"])
    assert "cloud (hosted learning active)" in out.getvalue()
    login_as(remote, parts[0], features=())
    out = io.StringIO()
    account.ProAccount(REPO, store=parts[0], transport=remote, out=out).run(["status"])
    assert "local only" in out.getvalue()
