"""Repo identity (root commit, else origin URL), org keys, and the refs
derived from them, end to end through the cloud learner."""
import hashlib
import hmac
import subprocess
import time

import pytest

from copse.learning import Outcome, TaskInfo
from copse.pro import auth, credentials, orgkey
from copse.pro.learning import CloudLearner
from copse.pro.orgkey import OrgKey, OrgKeys, normalize_remote, repo_identity
from pro_fixtures import (  # noqa: F401 - fixtures
    BASE, SpyLocal, backend, claims, pro_env, sign, signing_key,
)

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false",
       "-c", "core.hooksPath=/dev/null", "-c", "init.defaultBranch=main"]


def git(cwd, *args):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def new_repo(path, *, commit=True, origin=None):
    path.mkdir(parents=True)
    git(path, "init", "-q")
    if commit:
        (path / "README").write_text("hi\n")
        git(path, "add", "README")
        git(path, "commit", "-q", "-m", "root")
    if origin:
        git(path, "remote", "add", "origin", origin)
    return path


# -- repo identity -----------------------------------------------------------------------------


def test_two_clones_at_different_paths_share_an_identity_and_key(tmp_path):
    a = new_repo(tmp_path / "a")
    git(tmp_path, "clone", "-q", str(a), str(tmp_path / "elsewhere" / "b"))
    b = tmp_path / "elsewhere" / "b"
    ia, ib = repo_identity(str(a)), repo_identity(str(b))
    root = git(a, "rev-list", "--max-parents=0", "HEAD").strip()
    assert ia == ib == root
    key = OrgKey("org_1", "k1", b"\x01" * 32)
    assert key.repo_key(ia) == key.repo_key(ib)


def test_several_roots_use_the_smallest(tmp_path):
    r = new_repo(tmp_path / "r")
    git(r, "checkout", "-q", "--orphan", "other")
    (r / "OTHER").write_text("x\n")
    git(r, "add", "OTHER")
    git(r, "commit", "-q", "-m", "second root")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--allow-unrelated-histories", "-m", "join", "other")
    roots = git(r, "rev-list", "--max-parents=0", "HEAD").split()
    assert len(roots) == 2
    assert repo_identity(str(r)) == min(roots)


@pytest.mark.parametrize("url", [
    "git@github.com:Owner/Repo.git",
    "ssh://git@GitHub.com/Owner/Repo.git",
    "ssh://git@github.com:22/Owner/Repo",
    "https://github.com/Owner/Repo.git",
    "https://user:tok@GITHUB.COM/Owner/Repo/",
    "http://github.com/Owner/Repo",
])
def test_ssh_and_https_origins_normalize_equal(url):
    assert normalize_remote(url) == "github.com/Owner/Repo"


def test_empty_repos_fall_back_to_the_origin_url(tmp_path):
    a = new_repo(tmp_path / "a", commit=False, origin="git@github.com:Owner/Repo.git")
    b = new_repo(tmp_path / "b", commit=False, origin="https://github.com/Owner/Repo")
    assert repo_identity(str(a)) == repo_identity(str(b)) == "github.com/Owner/Repo"


@pytest.mark.parametrize("url", ["", "not a url", "file:///srv/repo.git", "/srv/repo.git"])
def test_unusable_origins_are_no_identity(url):
    assert normalize_remote(url) is None


def test_no_commits_and_no_origin_is_no_identity(tmp_path):
    assert repo_identity(str(new_repo(tmp_path / "e", commit=False))) is None
    (tmp_path / "plain").mkdir()
    assert repo_identity(str(tmp_path / "plain")) is None
    assert repo_identity(str(tmp_path / "missing")) is None


# -- domain separation -------------------------------------------------------------------------------


def test_repo_agent_and_ref_keys_are_domain_separated():
    k = OrgKey("org_1", "k1", b"\x02" * 32)
    x = "a" * 40

    def mac(prefix):
        return hmac.new(k.key, prefix + x.encode(), hashlib.sha256).hexdigest()

    assert k.repo_key(x) == mac(b"copse-repo-v1:")
    assert k.agent_ref(x) == mac(b"copse-agent-v1:")[:32]
    assert k.ref(x) == mac(b"copse-ref-v1:")
    assert len({k.repo_key(x), k.agent_ref(x), k.ref(x), k.ref(x)[:32], k.repo_key(x)[:32]}) == 5


# -- end to end through the cloud learner ---------------------------------------------------------------


@pytest.fixture
def learning_backend(backend):
    backend.records, backend.suggests = [], []

    def record(form, headers):
        backend.records.append(dict(form))
        return 200, {"recorded": True}

    def suggest(form, headers):
        backend.suggests.append(dict(form))
        extra = {"key_id": backend.announce} if getattr(backend, "announce", None) else {}
        return 200, {"profile": form["candidates"][0], **extra}

    backend.routes["POST /learning/record"] = record
    backend.routes["POST /learning/suggest"] = suggest
    return backend


def logged_in(backend, tmp_path, org="org_1"):
    store = credentials.FileStore(tmp_path / "pro")
    t = backend.issue()
    store.save({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                "access_expires_at": time.time() + 900, "base_url": BASE,
                "entitlement": sign(backend.key, claims(org_id=org, features=["learning"]))})
    return store


def learner(backend, store, tmp_path, repo, local=None):
    return CloudLearner(str(repo), local, client=auth.Client(BASE, backend), store=store,
                        key_store=credentials.FileStore(tmp_path / "pro", account="learning-keys"),
                        cost=lambda n: 1, start_thread=False)


def task(repo, agent="w1"):
    return TaskInfo(repo_root=str(repo), task="Fix the crash", agent_id=agent, profile="developer")


def test_clones_send_the_same_repo_key(learning_backend, tmp_path):
    a = new_repo(tmp_path / "a")
    git(tmp_path, "clone", "-q", str(a), str(tmp_path / "b"))
    store = logged_in(learning_backend, tmp_path)
    for repo in (a, tmp_path / "b"):
        lr = learner(learning_backend, store, tmp_path, repo)
        lr.record(task(repo), Outcome("merged"))
        lr.flush()
    ka, kb = (r["repo_key"] for r in learning_backend.records)
    assert ka == kb


def test_different_orgs_give_different_keys(learning_backend, tmp_path):
    repo = new_repo(tmp_path / "r")
    keys = []
    for org in ("org_1", "org_2"):
        store = logged_in(learning_backend, tmp_path, org)
        lr = learner(learning_backend, store, tmp_path, repo)
        lr.record(task(repo), Outcome("merged"))
        lr.flush()
        keys.append(learning_backend.records[-1]["repo_key"])
    assert keys[0] != keys[1]
    assert learning_backend.key_fetches == ["org_1", "org_2"]     # org change -> refetch


def test_no_identity_means_nothing_is_sent(learning_backend, tmp_path):
    repo = new_repo(tmp_path / "empty", commit=False)
    store = logged_in(learning_backend, tmp_path)
    local = SpyLocal()
    lr = learner(learning_backend, store, tmp_path, repo, local)
    lr.record(task(repo), Outcome("merged"))
    lr.flush()
    assert lr.suggest(task(repo), ["developer", "reviewer"]) == "reviewer"   # the local learner's pick
    assert learning_backend.records == [] and learning_backend.suggests == []
    assert learning_backend.key_fetches == []
    assert local.done("w1")                          # still learned locally


def test_a_key_id_change_triggers_a_refetch(learning_backend, tmp_path):
    repo = new_repo(tmp_path / "r")
    store = logged_in(learning_backend, tmp_path)
    lr = learner(learning_backend, store, tmp_path, repo)
    lr.suggest(task(repo), ["developer", "reviewer"])
    old = learning_backend.suggests[-1]["repo_key"]
    # the server rotates the org key and says so
    learning_backend.org_keys["org_1"] = ("k2", b"\x07" * 32)
    learning_backend.announce = "k2"
    lr.suggest(task(repo), ["developer", "reviewer"])
    assert learning_backend.key_fetches == ["org_1"]
    lr.suggest(task(repo), ["developer", "reviewer"])
    assert learning_backend.key_fetches == ["org_1", "org_1"]
    new = learning_backend.suggests[-1]["repo_key"]
    assert new != old and new == OrgKey("org_1", "k2", b"\x07" * 32).repo_key(repo_identity(str(repo)))


def test_the_same_key_id_does_not_refetch(learning_backend, tmp_path):
    repo = new_repo(tmp_path / "r")
    store = logged_in(learning_backend, tmp_path)
    learning_backend.announce = "k1"
    lr = learner(learning_backend, store, tmp_path, repo)
    for _ in range(3):
        lr.suggest(task(repo), ["developer", "reviewer"])
    assert learning_backend.key_fetches == ["org_1"]


def test_malformed_keys_are_refused(backend, tmp_path):
    store = logged_in(backend, tmp_path)
    ks = credentials.FileStore(tmp_path / "pro", account="learning-keys")
    for body in ({"org_id": "org_1", "key_id": "k1", "key": "c2hvcnQ="},
                 {"org_id": "org_other", "key_id": "k1", "key": "AA" * 22},
                 {"org_id": "org_1", "key_id": "bad id!", "key": "A" * 43 + "="}):
        backend.routes["GET /orgs/org_1/learning-key"] = [(200, body)]
        keys = OrgKeys(store=store, client=auth.Client(BASE, backend), key_store=ks)
        with pytest.raises(orgkey.OrgKeyUnavailable):
            keys.get("org_1")


def test_key_rotated_409_refetches_and_payload_names_the_key(learning_backend, tmp_path):
    repo = new_repo(tmp_path / "r")
    store = logged_in(learning_backend, tmp_path, org="org_team")
    lr = learner(learning_backend, store, tmp_path, repo, SpyLocal())
    lr.suggest(task(repo), ["developer", "reviewer"])
    assert (learning_backend.suggests[-1]["org_id"], learning_backend.suggests[-1]["key_id"]) == \
        ("org_team", "k1")
    learning_backend.org_keys["org_team"] = ("k2", b"\x09" * 32)
    ok = learning_backend.routes["POST /learning/suggest"]
    learning_backend.routes["POST /learning/suggest"] = \
        lambda f, h: (409, {"error": "key_rotated", "key_id": "k2"})
    assert lr.suggest(task(repo), ["developer", "reviewer"]) == "reviewer"     # local fallback
    learning_backend.routes["POST /learning/suggest"] = ok
    lr.suggest(task(repo), ["developer", "reviewer"])
    assert learning_backend.key_fetches == ["org_team", "org_team"]
    assert learning_backend.suggests[-1]["key_id"] == "k2"
