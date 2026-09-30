import json
import os
import time

import pytest

from copse import quota
from copse.config import RepoConfig


def limits(**windows):
    """A rollout event line; windows maps slot -> (used, minutes, resets_at)."""
    rl = {slot: {"used_percent": u, "window_minutes": m, "resets_at": r}
          for slot, (u, m, r) in windows.items()}
    return json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": rl}})


def rollout(tmp_path, *lines, name="rollout-2026-09-30T10-00-00-abc.jsonl", day="30"):
    d = tmp_path / "codex-home" / "sessions" / "2026" / "09" / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def test_codex_maps_windows_by_length_not_slot(tmp_path):
    future = time.time() + 3600
    # The weekly window sits in "primary" here and the 5-hour one in "secondary".
    rollout(tmp_path, limits(primary=(82.0, 10080, future), secondary=(10.0, 300, future)))
    q = quota.get("codex")
    by = {w.minutes: w.used for w in q.windows}
    assert by == {10080: 82.0, 300: 10.0}
    assert quota.headroom("codex", RepoConfig()) == pytest.approx(18)
    assert "Codex at 82% of its weekly limit, resets" in quota.note("codex")


@pytest.mark.parametrize("minutes,label", [(300, "5-hour"), (10080, "weekly"), (43200, "monthly")])
def test_codex_window_lengths(tmp_path, minutes, label):
    rollout(tmp_path, limits(primary=(40.0, minutes, time.time() + 600)))
    assert f"40% of its {label} limit" in quota.note("codex")


def test_codex_reads_the_last_event_with_rate_limits(tmp_path):
    future = time.time() + 3600
    rollout(tmp_path,
            limits(primary=(10.0, 300, future)),
            limits(primary=(55.0, 300, future)),
            json.dumps({"type": "event_msg", "payload": {"type": "agent_message"}}),
            json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": None}}))
    assert quota.get("codex").windows[0].used == 55.0


def test_codex_uses_the_newest_rollout(tmp_path):
    future = time.time() + 3600
    old = rollout(tmp_path, limits(primary=(90.0, 300, future)), name="rollout-old.jsonl", day="29")
    rollout(tmp_path, limits(primary=(20.0, 300, future)), name="rollout-new.jsonl")
    os.utime(old, (time.time() - 1000, time.time() - 1000))
    assert quota.get("codex").windows[0].used == 20.0


def test_stale_windows_are_dropped(tmp_path):
    rollout(tmp_path, limits(primary=(95.0, 300, time.time() - 60)))
    assert quota.get("codex") is None
    assert quota.note("codex") is None
    assert quota.headroom("codex", RepoConfig()) == 100


def test_unknown_provider_has_full_headroom():
    assert quota.get("codex") is None
    assert quota.headroom("codex", RepoConfig()) == 100
    assert quota.headroom("antigravity", RepoConfig()) == 100


def test_claude_status_line_goes_through_quota():
    future = time.time() + 3600
    assert quota.record_claude({"rate_limits": {
        "five_hour": {"used_percentage": 93, "resets_at": future},
        "seven_day": {"used_percentage": 20, "resets_at": future}}})
    assert quota.headroom("claude", RepoConfig()) == pytest.approx(7)
    assert "Claude at 93% of its 5-hour limit" in quota.note("claude")
    assert not quota.record_claude({"nothing": 1})


def test_claude_data_goes_stale():
    quota.record_claude({"rate_limits": {"five_hour": {"used_percentage": 93, "resets_at": time.time() + 3600}}})
    data = json.loads(quota.path().read_text())
    data["claude"]["updated_at"] -= quota.CLAUDE_FRESH_SECONDS + 5
    quota.path().write_text(json.dumps(data))
    assert quota.get("claude") is None


def test_limited_until_zeroes_headroom_then_expires():
    until = quota.record_limit("antigravity", RepoConfig())
    assert until == pytest.approx(time.time() + 300 * 60, abs=5)
    assert quota.headroom("antigravity", RepoConfig()) == 0
    assert "limit reached" in quota.note("antigravity")
    data = json.loads(quota.path().read_text())
    data["antigravity"]["limited_until"] = time.time() - 1
    quota.path().write_text(json.dumps(data))
    assert quota.get("antigravity") is None
    assert quota.headroom("antigravity", RepoConfig()) == 100


def test_limit_cooldown_is_configurable():
    until = quota.record_limit("antigravity", RepoConfig(limit_cooldown_minutes=10))
    assert until == pytest.approx(time.time() + 600, abs=5)


def test_native_headroom_follows_reachability(monkeypatch):
    from copse.native import serve

    monkeypatch.setattr(serve, "local_servers", lambda repo_root: [object()])
    monkeypatch.setattr(serve, "reachable", lambda s, timeout=1.0: True)
    assert quota.headroom("native", RepoConfig()) == 100
    assert quota.note("native") is None
    monkeypatch.setattr(serve, "reachable", lambda s, timeout=1.0: False)
    assert quota.headroom("native", RepoConfig()) == 0
    assert quota.note("native") == "local model server not answering"


def test_notes_lists_only_providers_with_data(tmp_path):
    assert quota.notes(native=False) == []
    rollout(tmp_path, limits(primary=(50.0, 300, time.time() + 600)))
    assert len(quota.notes(native=False)) == 1


def test_auth_and_token_files_are_never_read(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    (home / "auth.json").write_text('{"tokens": {"access_token": "secret"}}')
    rollout(tmp_path, limits(primary=(50.0, 300, time.time() + 600)))
    opened = []
    path_cls = type(home)
    real_open, real_read_text = path_cls.open, path_cls.read_text

    def spy_open(self, *a, **kw):
        opened.append(str(self))
        return real_open(self, *a, **kw)

    def spy_read_text(self, *a, **kw):
        opened.append(str(self))
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(path_cls, "open", spy_open)
    monkeypatch.setattr(path_cls, "read_text", spy_read_text)
    quota.get("codex")
    quota.refresh_codex()
    assert opened
    assert not [p for p in opened if "auth" in os.path.basename(p) or "token" in os.path.basename(p)]
    assert all(p.endswith(".jsonl") or os.path.basename(p).startswith("quota.json") for p in opened)
