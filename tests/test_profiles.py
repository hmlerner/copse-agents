from copse.profiles import _parse, list_profiles, load_profile


def test_frontmatter_values_drop_trailing_comments():
    p = _parse(
        "---\n"
        "# a whole-line comment\n"
        "name: cheap  # a cheap worker\n"
        "description: Fixes issue#12 # but not this\n"
        "provider: claude #inline\n"
        "model: haiku\t# tab before the hash\n"
        "effort: low # keep it short\n"
        "permission_mode: '#literal' # quoted values stay whole\n"
        "---\nbody\n",
        "fallback",
    )
    assert p.name == "cheap"
    assert p.description == "Fixes issue#12"  # a '#' inside a word isn't a comment
    assert p.provider == "claude"
    assert p.model == "haiku"
    assert p.effort == "low"
    assert p.permission_mode == "#literal"
    assert p.prompt == "body"


def test_lightweight_fields_parse_and_default_off():
    p = _parse(
        "---\nname: x\nstrict_mcp: true\nsetting_sources: project, local # skip user\n"
        "headless: yes\nadd_dirs: /a, /b # shared cache\n"
        "allowed_tools: [Edit, Bash(git commit:*)]\n---\n",
        "x",
    )
    assert p.strict_mcp is True and p.headless is True
    assert p.setting_sources == ["project", "local"]
    assert p.add_dirs == ["/a", "/b"]
    assert p.allowed_tools == ["Edit", "Bash(git commit:*)"]

    plain = _parse("---\nname: y\nstrict_mcp: false\n---\n", "y")
    assert plain.strict_mcp is False and plain.headless is False
    assert plain.setting_sources is None and plain.effort is None
    assert plain.add_dirs is None


def test_builtin_profiles_keep_their_defaults():
    # reviewer is deliberately a cheap profile: strict MCP, lean settings, a
    # moderate effort (see tests/test_review_efficiency.py). developer-heavy
    # deliberately sets a model and high effort (weight routing).
    for p in list_profiles():
        if p.provider == "claude" and p.name != "reviewer":
            assert not p.strict_mcp and not p.headless
            assert p.setting_sources is None and p.add_dirs is None
            if p.name == "developer-heavy":
                assert p.model == "claude-fable-5-1" and p.effort == "high"
            else:
                assert p.effort is None
    assert load_profile("developer").permission_mode == "auto"


def test_repo_add_dirs_apply_to_every_profile_and_a_profile_adds_to_them(tmp_path, capsys):
    """The repo config is the primary home; a profile adds to it and never removes.

    Relative entries resolve against the repo root, not the worktree the process
    happens to be in, and a directory that is not there is reported rather than
    dropped in silence, which is what Claude Code does with it. Reporting is
    launch's job, once; loading a profile, which happens several times per
    launch and on every resume, says nothing.
    """
    from copse.profiles import load_profile, missing_add_dirs

    repo = tmp_path / "proj"
    (repo / ".copse" / "agents").mkdir(parents=True)
    (repo / "cache").mkdir()
    (repo / "refs").mkdir()
    (repo / ".copse" / "config.json").write_text('{"add_dirs": ["cache", "/opt/shared"]}')
    (repo / ".copse" / "agents" / "worker.md").write_text(
        "---\nname: worker\ndescription: d\nprovider: claude\nadd_dirs: refs\n---\nbody\n"
    )

    p = load_profile("worker", str(repo))
    assert p.add_dirs == [str(repo / "cache"), "/opt/shared", str(repo / "refs")]
    assert capsys.readouterr().err == ""
    assert missing_add_dirs(p) == ["/opt/shared"]

    # A profile with none of its own still gets the repo's, and so does a built-in.
    (repo / ".copse" / "agents" / "plain.md").write_text(
        "---\nname: plain\ndescription: d\nprovider: claude\n---\nbody\n"
    )
    assert load_profile("plain", str(repo)).add_dirs[0] == str(repo / "cache")
    assert load_profile("developer", str(repo)).add_dirs[0] == str(repo / "cache")


def test_repo_add_dirs_expand_a_leading_tilde(tmp_path, monkeypatch):
    """``~/cache`` means the home directory's cache, not ``<repo>/~/cache``."""
    from copse.profiles import load_profile

    home = tmp_path / "home"
    (home / "cache").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "proj"
    (repo / ".copse").mkdir(parents=True)
    (repo / ".copse" / "config.json").write_text('{"add_dirs": ["~/cache"]}')

    assert load_profile("developer", str(repo)).add_dirs == [str(home / "cache")]


def test_an_unknown_user_in_add_dirs_is_reported_not_raised(tmp_path):
    """``~typo/cache`` makes expanduser raise. Kept as written instead, so
    loading the profile (and every launch with it) still works and launch
    reports the entry as missing."""
    from copse.profiles import load_profile, missing_add_dirs

    repo = tmp_path / "proj"
    (repo / ".copse").mkdir(parents=True)
    (repo / ".copse" / "config.json").write_text('{"add_dirs": ["~no-such-user-copse/cache"]}')

    p = load_profile("developer", str(repo))
    assert p.add_dirs == ["~no-such-user-copse/cache"]
    assert missing_add_dirs(p) == ["~no-such-user-copse/cache"]
