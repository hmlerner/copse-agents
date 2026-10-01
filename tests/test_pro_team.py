"""copse Team: the org policy plugin, the audit-event plugin, and the
``copse account org`` commands. Both plugins are always installed and must do
nothing without a verified team entitlement."""
import io
import json
import os
import re
import stat
import time
from importlib.metadata import entry_points

import pytest

from copse import plugins
from copse.config import RepoConfig
from copse.events import Event
from copse.policy import AssignInfo, MergeInfo
from copse.pro import account, auth, credentials
from copse.pro import team_events
from copse.pro import team_policy
from copse.pro._files import private_dir
from copse.pro.orgkey import OrgKey
from copse.pro.team_events import ProEvents, Spool
from copse.pro.team_policy import ProPolicy
from pro_fixtures import (  # noqa: F401 - fixtures
    BASE, ROOT_SHA, backend, claims, fixed_identity, pro_env, sign, signing_key,
)

ORG = "org_team1"
REPO = "/work/secret-client-project"
BRANCH = "feat/zebra-hotfix"
AGENT = "worker-zebra-7"
SUPERVISOR = "supervisor-zebra-1"
POLICY = {"allowed_providers": ["claude", "codex"], "allowed_models": ["claude-sonnet-4", "gpt-5"],
          "require_human_review": True, "max_parallel_workers": 4}


def team_claims(**over):
    c = dict(org_id=ORG, plan="team", features=["learning", "team"], role="member",
             policy_version=3)
    c.update(over)
    return claims(**c)


@pytest.fixture
def team(backend, fixed_identity):
    """The fake backend with team-org endpoints and a logged-in team member."""
    backend.policy_body = {"org_id": ORG, "version": 3, "policy": dict(POLICY),
                           "updated_at": 1, "updated_by": "u"}
    backend.events = []

    def guarded(fn):
        def route(form, headers):
            if not backend._bearer(headers):
                return 401, {"error": "invalid_token"}
            return fn(form, headers)
        return route

    backend.routes[f"GET /orgs/{ORG}/policy"] = guarded(lambda f, h: (200, backend.policy_body))

    def events(form, headers):
        assert set(form) == {"events"} and 1 <= len(form["events"]) <= 100
        backend.events.extend(form["events"])
        return 200, {"accepted": len(form["events"])}

    backend.routes[f"POST /orgs/{ORG}/events"] = guarded(events)
    backend.routes["GET /orgs"] = guarded(lambda f, h: (200, {"orgs": [
        {"org_id": "org_personal", "name": "me (personal)", "personal": True, "role": "owner",
         "plan": "pro", "status": "active", "seats": 1, "member_count": 1},
        {"org_id": ORG, "name": "Team One", "personal": False, "role": "member", "plan": "team",
         "status": "active", "seats": 5, "member_count": 3}]}))
    backend.routes[f"GET /entitlement?org_id={ORG}"] = guarded(
        lambda f, h: (200, {"entitlement": sign(backend.key, team_claims())}))
    backend.store = credentials.default_store()
    login(backend, team_claims())
    return backend


def login(backend, entitlement_claims, **extra):
    t = backend.issue()
    backend.store.save({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                        "access_expires_at": time.time() + 900, "base_url": BASE,
                        "entitlement": sign(backend.key, entitlement_claims), **extra})


def client(backend):
    return auth.Client(BASE, backend)


def plugin(backend):
    return ProPolicy(REPO, store=backend.store, client=client(backend))


def assign(provider="claude", model="claude-sonnet-4", profile="developer", running=0):
    return AssignInfo(repo_root=REPO, task="t", profile=profile, provider=provider, model=model,
                      actor="user", running_workers=running)


def merge(actor):
    return MergeInfo(repo_root=REPO, workspace_id="ws1", branch=BRANCH, base_branch="main",
                     agent_id=AGENT, profile="developer", provider="claude", actor=actor)


# -- policy: no team entitlement ------------------------------------------------------------------


def test_everything_allowed_when_not_logged_in(backend):
    p = ProPolicy(REPO, store=credentials.default_store(), client=client(backend))
    assert p.check_assign(assign(provider="anything")).allowed
    assert p.check_merge(merge("supervisor")).allowed
    assert backend.calls == []


def test_everything_allowed_without_the_team_feature(team):
    login(team, claims(features=["learning"]))
    p = plugin(team)
    assert p.check_assign(assign(provider="ollama", model=None)).allowed
    assert p.check_merge(merge(SUPERVISOR)).allowed
    assert f"GET /orgs/{ORG}/policy" not in team.paths()


def test_a_broken_credential_store_allows_everything(backend, monkeypatch):
    def boom():
        raise RuntimeError("keychain is having a day")

    monkeypatch.setattr(credentials, "default_store", boom)
    p = ProPolicy(REPO)
    assert p.check_assign(assign()).allowed and p.check_merge(merge(SUPERVISOR)).allowed


# -- policy rules ------------------------------------------------------------------------------------


def test_allowed_provider_and_model_pass(team):
    assert plugin(team).check_assign(assign()).allowed


def test_provider_outside_the_list_is_denied(team):
    d = plugin(team).check_assign(assign(provider="ollama"))
    assert not d.allowed and "'ollama'" in d.reason and "claude, codex" in d.reason


def test_undeclared_provider_is_denied_when_a_list_is_set(team):
    assert not plugin(team).check_assign(assign(provider=None)).allowed


def test_model_outside_the_list_is_denied(team):
    d = plugin(team).check_assign(assign(model="claude-opus-4"))
    assert not d.allowed and "'claude-opus-4'" in d.reason


def test_undeclared_model_is_denied_when_a_list_is_set(team):
    d = plugin(team).check_assign(assign(model=None))
    assert not d.allowed and "no declared model" in d.reason


def test_null_lists_allow_any_provider_and_model(team):
    team.policy_body["policy"].update(allowed_providers=None, allowed_models=None)
    assert plugin(team).check_assign(assign(provider="ollama", model=None)).allowed


def test_empty_lists_allow_nothing(team):
    team.policy_body["policy"].update(allowed_providers=[])
    assert not plugin(team).check_assign(assign()).allowed


@pytest.mark.parametrize("running,allowed", [(0, True), (3, True), (4, False), (9, False), (None, False)])
def test_parallel_worker_cap(team, running, allowed):
    d = plugin(team).check_assign(assign(running=running))
    assert d.allowed is allowed
    if not allowed:
        assert "at most 4 parallel worker" in d.reason


def test_no_cap_means_any_number_of_workers(team):
    team.policy_body["policy"]["max_parallel_workers"] = None
    assert plugin(team).check_assign(assign(running=None)).allowed


@pytest.mark.parametrize("actor,allowed", [("user", True), (SUPERVISOR, False),
                                           ("pipeline", False), (None, False)])
def test_human_review_requires_the_user_to_merge(team, actor, allowed):
    d = plugin(team).check_merge(merge(actor))
    assert d.allowed is allowed
    if not allowed:
        assert "human review" in d.reason


def test_merges_by_agents_are_fine_without_the_rule(team):
    team.policy_body["policy"]["require_human_review"] = False
    assert plugin(team).check_merge(merge(SUPERVISOR)).allowed


def test_flat_policy_shape_is_accepted(team):
    team.policy_body = {**POLICY, "allowed_providers": ["codex"], "version": 3}
    assert not plugin(team).check_assign(assign()).allowed
    assert plugin(team).check_assign(assign(provider="codex", model="gpt-5")).allowed


# -- policy caching and failing closed -----------------------------------------------------------------


def test_policy_is_cached_by_version_in_a_private_file(team):
    p = plugin(team)
    p.check_assign(assign())
    p.check_assign(assign())
    assert team.paths().count(f"GET /orgs/{ORG}/policy") == 1
    path = private_dir() / f"policy-{ORG}.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text())["version"] == 3


def test_newer_policy_version_is_fetched(team):
    plugin(team).check_assign(assign())
    team.policy_body = {**team.policy_body, "version": 4,
                        "policy": {**POLICY, "allowed_providers": ["codex"]}}
    login(team, team_claims(policy_version=4))
    assert not plugin(team).check_assign(assign()).allowed
    assert team.paths().count(f"GET /orgs/{ORG}/policy") == 2


def test_last_good_copy_is_used_when_the_fetch_fails(team):
    plugin(team).check_assign(assign())
    login(team, team_claims(policy_version=9))
    team.routes[f"GET /orgs/{ORG}/policy"] = [auth.TransportError("down")]
    p = plugin(team)
    assert p.check_assign(assign()).allowed
    assert not p.check_assign(assign(provider="ollama")).allowed


def test_fail_closed_when_no_policy_was_ever_fetched(team):
    team.routes[f"GET /orgs/{ORG}/policy"] = [auth.TransportError("down")]
    p = plugin(team)
    for d in (p.check_assign(assign()), p.check_merge(merge("user"))):
        assert not d.allowed
        assert "never been fetched" in d.reason and "copse account org policy" in d.reason


@pytest.mark.parametrize("body", [
    {"version": 3, "policy": {"allowed_providers": "claude"}},
    {"version": "3", "policy": POLICY},
    {"version": 3, "policy": {**POLICY, "require_human_review": "yes"}},
    {"org_id": "org_other", "version": 3, "policy": POLICY},
])
def test_malformed_policy_fails_closed(team, body):
    team.policy_body = body
    assert not plugin(team).check_assign(assign()).allowed


def test_a_loosened_cache_file_is_not_trusted(team):
    plugin(team).check_assign(assign())
    os.chmod(private_dir() / f"policy-{ORG}.json", 0o644)
    team.routes[f"GET /orgs/{ORG}/policy"] = [auth.TransportError("down")]
    assert not plugin(team).check_assign(assign()).allowed


# -- events -------------------------------------------------------------------------------------------


def ev(kind="merge", **over):
    base = dict(kind=kind, repo_root=REPO, agent_id=AGENT, branch=BRANCH, profile="developer",
                provider="claude", model="claude-sonnet-4", actor=SUPERVISOR, at=1_800_000_000.5,
                approved=None, merged=None)
    base.update(over)
    return Event(**base)


def events_plugin(backend, **kw):
    return ProEvents(REPO, store=backend.store, client=client(backend),
                     start_thread=kw.pop("start_thread", False), **kw)


def test_event_payload_has_exactly_the_contract_keys(team):
    p = events_plugin(team)
    p.emit(ev("review", approved=True))
    p.emit(ev("remove", merged=False, actor="user"))
    assert p.flush()
    review, remove = team.events
    keys = {"kind", "agent_ref", "branch_ref", "profile", "provider", "model", "actor_ref", "at",
            "approved", "merged"}
    assert set(review) == set(remove) == keys
    key = OrgKey(ORG, *team.org_key(ORG))
    assert review["agent_ref"] == team_events.agent_ref(key, AGENT) == key.ref("agent\0" + AGENT)
    assert review["branch_ref"] == key.ref("branch\0" + ROOT_SHA + "\0" + BRANCH)
    assert re.fullmatch(r"[0-9a-f]{64}", review["branch_ref"])
    assert review["actor_ref"] == team_events.agent_ref(key, SUPERVISOR)
    assert remove["actor_ref"] == "user"
    assert (review["approved"], review["merged"], remove["merged"]) == (True, None, False)
    assert review["at"] == 1_800_000_000.5
    blob = json.dumps(team.events)
    for s in ("zebra", BRANCH, AGENT, SUPERVISOR, REPO, "secret-client-project"):
        assert s not in blob


def test_unsafe_identifiers_become_null(team):
    p = events_plugin(team)
    p.emit(ev(profile="my profile", provider="Claude Code", model="/Users/me/models/x.gguf"))
    assert p.flush()
    (e,) = team.events
    assert (e["profile"], e["provider"], e["model"]) == (None, None, None)


def test_no_events_without_a_team_entitlement(team):
    login(team, claims(features=["learning"]))
    p = events_plugin(team, start_thread=True)
    p.emit(ev())
    assert p.flush()
    assert team.events == [] and Spool().entries() == []
    assert not any("/events" in c for c in team.paths())
    assert p._thread is None                      # no sender thread was ever started


def test_no_events_when_not_logged_in(team):
    team.store.delete()
    p = events_plugin(team)
    p.emit(ev())
    p.flush()
    assert Spool().entries() == [] and team.events == []


def test_spool_survives_offline_periods_and_restarts(team):
    team.routes[f"POST /orgs/{ORG}/events"] = [auth.TransportError("down")]
    p = events_plugin(team)
    for k in ("assign", "review", "merge"):
        p.emit(ev(k))
    assert not p.flush()
    assert [e["event"]["kind"] for e in Spool().entries()] == ["assign", "review", "merge"]
    spool_file = private_dir() / "events-spool.jsonl"
    assert stat.S_IMODE(os.stat(spool_file).st_mode) == 0o600
    # "restart": a new plugin, backend reachable again
    team.routes[f"POST /orgs/{ORG}/events"] = lambda f, h: (team.events.extend(f["events"]),
                                                             (200, {}))[1]
    again = events_plugin(team)
    assert again.send_pending()
    assert [e["kind"] for e in team.events] == ["assign", "review", "merge"]
    assert Spool().entries() == []


def test_failed_sends_back_off_exponentially(team):
    team.routes[f"POST /orgs/{ORG}/events"] = [(500, {"error": "internal_error"})]
    p = events_plugin(team)
    p.emit(ev())
    delays = []
    for _ in range(4):
        p.flush()
        delays.append(p._delay)
    assert delays == [1.0, 2.0, 4.0, 8.0]
    assert len(Spool().entries()) == 1


def test_rejected_batches_are_dropped_not_retried_forever(team):
    team.routes[f"POST /orgs/{ORG}/events"] = [(422, {"error": "invalid_request"})]
    p = events_plugin(team)
    p.emit(ev())
    assert p.flush()
    assert Spool().entries() == []


def test_batches_are_at_most_100(team):
    sizes = []
    team.routes[f"POST /orgs/{ORG}/events"] = lambda f, h: (sizes.append(len(f["events"])), (200, {}))[1]
    p = events_plugin(team)
    key = OrgKey(ORG, "k1", b"k" * 32)
    Spool().append(ORG, [team_events.event_payload(key, ROOT_SHA, ev(at=1_800_000_000 + i))
                         for i in range(250)])
    assert p.send_pending()
    assert sizes == [100, 100, 50]


def test_spool_caps_drop_the_oldest(tmp_path):
    d = tmp_path / "pro"
    d.mkdir(mode=0o700)
    s = Spool(d, max_events=5)
    s.append(ORG, [{"kind": "merge", "n": i} for i in range(8)])
    assert [e["event"]["n"] for e in s.entries()] == [3, 4, 5, 6, 7] and s.dropped == 3
    small = Spool(d, max_bytes=300)
    small.append(ORG, [{"kind": "merge", "n": i, "pad": "x" * 50} for i in range(20)])
    assert (d / "events-spool.jsonl").stat().st_size <= 300
    assert small.entries()[-1]["event"]["n"] == 19


def test_a_loose_spool_is_refused(tmp_path):
    d = tmp_path / "pro"
    d.mkdir(mode=0o700)
    s = Spool(d)
    s.append(ORG, [{"kind": "merge"}])
    os.chmod(d / "events-spool.jsonl", 0o644)
    with pytest.raises(credentials.CredentialError):
        s.entries()


def test_full_queue_spills_to_the_spool(team, monkeypatch):
    monkeypatch.setattr(team_events, "QUEUE_SIZE", 2)
    team.routes[f"POST /orgs/{ORG}/events"] = [auth.TransportError("down")]
    p = events_plugin(team)
    p.keys.get(ORG)          # the spill path uses only a cached key
    for i in range(5):
        p.emit(ev(at=1_800_000_000 + i))
    assert p.queue.qsize() == 2 and len(Spool().entries()) == 3
    p.flush()
    assert len(Spool().entries()) == 5


def test_background_sender_delivers(team):
    p = events_plugin(team, start_thread=True)
    p.emit(ev())
    assert p.flush(5)
    assert len(team.events) == 1


def test_no_org_key_means_no_team_events(team):
    team.key_status = (403, {"error": "forbidden"})
    p = events_plugin(team)
    p.emit(ev())
    p.flush()
    assert Spool().entries() == [] and team.events == [] and p.dropped_no_key == 1


def test_emit_never_raises(team, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(team_events, "event_payload", boom)
    events_plugin(team).emit(ev())


# -- account org commands and wiring ------------------------------------------------------------------


def run(backend, *args):
    out, err = io.StringIO(), io.StringIO()
    code = account.ProAccount(REPO, store=backend.store, transport=backend, out=out, err=err).run(list(args))
    return code, out.getvalue(), err.getvalue()


def test_org_list_use_and_policy(team):
    login(team, claims())          # personal org first
    code, out, _ = run(team, "org", "list")
    assert code == 0 and ORG in out and "Team One" in out and "team" in out
    code, out, _ = run(team, "org", "use", ORG)
    assert code == 0 and ORG in out and "member" in out
    assert team.store.load()["org_id"] == ORG
    code, out, _ = run(team, "org", "list")
    assert f"* {ORG}" in out
    code, out, _ = run(team, "org", "policy")
    assert code == 0 and "version 3" in out and "claude, codex" in out
    assert "require human review   yes" in out
    code, out, _ = run(team, "status")
    assert f"{ORG} (member)" in out


def test_org_use_rejects_a_bad_org_id(team):
    code, _, err = run(team, "org", "use", "../../etc")
    assert code == 1 and "invalid org id" in err


def test_org_use_keeps_the_old_org_if_the_backend_refuses(team):
    team.routes["GET /entitlement?org_id=org_nope"] = [(404, {"error": "not_found"})]
    code, _, err = run(team, "org", "use", "org_nope")
    assert code == 1 and "not_found" in err
    assert team.store.load().get("org_id") is None


def test_org_policy_without_a_team_plan(team):
    login(team, claims())
    code, out, _ = run(team, "org", "policy")
    assert code == 0 and "nothing is enforced" in out


def test_entry_points_are_registered():
    def value(group):
        return [e.value for e in entry_points(group=group) if e.name == "pro"]

    assert value("copse.policy") == ["copse.pro.team_policy:make"]
    assert value("copse.events") == ["copse.pro.team_events:make"]


def test_the_installed_plugins_are_inert_without_an_entitlement(tmp_path):
    """Out of the box: copse's own policy plugin is selected (the only one
    installed), both of its events plugins hear every event (the group fans
    out), and they allow everything, send nothing, write nothing."""
    from copse import events, policy
    from copse.pro.audit_chain import AuditChain, audit_dir

    plugins.reset()
    try:
        cfg = RepoConfig()
        p = plugins.select(plugins.POLICY, cfg, str(tmp_path))
        assert isinstance(p, ProPolicy)
        found = events.plugins_for(cfg, str(tmp_path))
        assert {type(x) for x in found} == {ProEvents, AuditChain}
        [e] = [x for x in found if isinstance(x, ProEvents)]
        assert policy.check_assign(cfg, str(tmp_path), "developer", "t", "assign").allowed
        for x in found:
            x.emit(ev(repo_root=str(tmp_path)))
        assert e.queue.qsize() == 0 and e._thread is None
        assert not (private_dir() / "events-spool.jsonl").exists()
        assert list(audit_dir().iterdir()) == []
    finally:
        plugins.reset()


# -- self-serve Team: create an org, buy seats, invite, join ---------------------------------------


def test_create_a_team_org_then_check_out_team_seats(team):
    login(team, claims())
    new_org = "org_" + "a" * 24

    def create(form, headers):
        assert form == {"name": "Acme Eng"}
        return 200, {"org_id": new_org, "name": "Acme Eng", "personal": False, "role": "owner"}

    def checkout(form, headers):
        assert form == {"plan": "team", "seats": 5, "org_id": new_org}
        return 200, {"url": "https://checkout.stripe.test/c/team", "id": "cs_1"}

    team.routes["POST /orgs"] = create
    team.routes["POST /billing/checkout"] = checkout
    code, out, _ = run(team, "org", "create", "Acme", "Eng")
    assert code == 0 and new_org in out and f"upgrade --team --seats N --org {new_org}" in out
    code, out, _ = run(team, "upgrade", "--team", "--seats", "5", "--org", new_org)
    assert code == 0 and out.strip() == "https://checkout.stripe.test/c/team"


def test_team_upgrade_defaults_to_the_current_org(team):
    team.store.save({**team.store.load(), "org_id": ORG})
    team.routes["POST /billing/checkout"] = lambda f, h: (
        (200, {"url": "https://checkout.stripe.test/c/t"}) if f == {"plan": "team", "seats": 3, "org_id": ORG}
        else (400, {"error": "invalid_request"}))
    code, out, _ = run(team, "upgrade", "--team", "--seats", "3")
    assert code == 0 and "c/t" in out


def test_team_upgrade_needs_an_org_and_seats(team):
    code, _, err = run(team, "upgrade", "--team", "--seats", "3")
    assert code == 1 and "no team org selected" in err
    assert run(team, "upgrade", "--team")[0] == 2
    assert run(team, "upgrade", "--seats", "3")[0] == 2
    assert run(team, "upgrade", "--team", "--seats", "x", "--org", ORG)[0] == 2
    assert run(team, "upgrade", "--org", ORG)[0] == 2


def test_pro_upgrade_still_sends_no_body(team):
    seen = []
    team.routes["POST /billing/checkout"] = lambda f, h: (seen.append(f), (200, {"url": "https://c.test/x"}))[1]
    assert run(team, "upgrade")[0] == 0 and seen == [{}]


def test_invite_and_join(team):
    code_ = "cpi_" + "b" * 43
    team.routes[f"POST /orgs/{ORG}/invites"] = lambda f, h: (
        200, {"invite_code": code_, "org_id": ORG, "email": f["email"], "role": f["role"]})
    team.routes["POST /invites/accept"] = lambda f, h: (
        (200, {"org_id": ORG, "role": "member"}) if f == {"invite_code": code_} else (400, {}))
    code, out, _ = run(team, "org", "invite", "dev@acme.test", "--admin", "--org", ORG)
    assert code == 0 and "as admin" in out and f"copse account org join {code_}" in out
    code, out, _ = run(team, "org", "join", code_)
    assert code == 0 and f"Joined {ORG} as member" in out


def test_portal_for_a_team_org(team):
    team.routes[f"POST /billing/portal?org_id={ORG}"] = [(200, {"url": "https://billing.stripe.test/p/t"})]
    code, out, _ = run(team, "portal", "--org", ORG)
    assert code == 0 and out.strip() == "https://billing.stripe.test/p/t"


# -- org CI tokens ---------------------------------------------------------------------------------


def test_ci_token_create_list_revoke(team):
    secret = "cpc_" + "c" * 43
    token_id = "ct_" + "a" * 32
    seen = []

    def create(form, headers):
        seen.append(form)
        return 200, {"token": secret, "token_id": token_id, "org_id": ORG, "name": form["name"],
                     "created_at": 1_900_000_000}

    team.routes[f"POST /orgs/{ORG}/ci-tokens"] = create
    team.routes[f"GET /orgs/{ORG}/ci-tokens"] = lambda f, h: (200, {"org_id": ORG, "tokens": [
        {"token_id": token_id, "name": "github actions", "created_by": "user_1",
         "created_at": 1_900_000_000, "last_used_at": None, "status": "active"}]})
    team.routes[f"DELETE /orgs/{ORG}/ci-tokens/{token_id}"] = lambda f, h: (
        200, {"org_id": ORG, "token_id": token_id, "status": "revoked"})

    code, out, err = run(team, "org", "ci-token", "create", "github", "actions", "--org", ORG)
    assert code == 0, err
    assert seen == [{"name": "github actions"}]
    assert secret in out and "COPSE_PRO_TOKEN" in out and "shown once" in out
    assert team.store.load().get("ci_token") is None and secret not in json.dumps(team.store.load())

    code, out, _ = run(team, "org", "ci-token", "list", "--org", ORG)
    assert code == 0 and token_id in out and "github actions" in out and "active" in out
    assert secret not in out

    code, out, _ = run(team, "org", "ci-token", "revoke", token_id, "--org", ORG)
    assert code == 0 and f"Revoked CI token {token_id}" in out
    assert [c[0] for c in team.calls if "ci-tokens" in c[0]] == [
        f"POST /orgs/{ORG}/ci-tokens", f"GET /orgs/{ORG}/ci-tokens", f"DELETE /orgs/{ORG}/ci-tokens/{token_id}"]


def test_ci_token_defaults_to_the_current_org_and_needs_one(team):
    code, _, err = run(team, "org", "ci-token", "list")
    assert code == 1 and "no team org selected" in err
    team.store.save({**team.store.load(), "org_id": ORG})
    team.routes[f"GET /orgs/{ORG}/ci-tokens"] = lambda f, h: (200, {"org_id": ORG, "tokens": []})
    code, out, _ = run(team, "org", "ci-token", "list")
    assert code == 0 and "no CI tokens" in out


def test_ci_token_errors_and_usage(team):
    team.routes[f"POST /orgs/{ORG}/ci-tokens"] = lambda f, h: (
        403, {"error": "forbidden", "error_description": "requires the admin role"})
    code, _, err = run(team, "org", "ci-token", "create", "deploy", "--org", ORG)
    assert code == 1 and "forbidden" in err and "admin" in err
    code, _, err = run(team, "org", "ci-token", "revoke", "not-an-id", "--org", ORG)
    assert code == 1 and "invalid CI token id" in err
    for bad in (("org", "ci-token"), ("org", "ci-token", "create"), ("org", "ci-token", "list", "x"),
                ("org", "ci-token", "revoke"), ("org", "ci-token", "rotate", "x"),
                ("org", "ci-token", "list", "--admin")):
        assert run(team, *bad)[0] == 2, bad


def test_ci_token_create_refuses_a_response_without_a_token(team):
    team.routes[f"POST /orgs/{ORG}/ci-tokens"] = lambda f, h: (200, {"token_id": "ct_1", "name": "x"})
    code, _, err = run(team, "org", "ci-token", "create", "x", "--org", ORG)
    assert code == 1 and "no CI token" in err
