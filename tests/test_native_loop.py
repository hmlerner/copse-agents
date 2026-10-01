"""The native agent loop against a fake chat endpoint: both wire formats,
the core tools, permissions, queued messages, context folding, retries."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from copse.native import (Client, ClientError, Endpoint, LoopConfig, NativeAgent, Permissions,
                          Toolbox, ToolSpec, core_tools)
from copse.native.client import Usage
from copse.native.permissions import bash_matches, split_commands
from copse.native.tools import clip


# -- a scripted endpoint ---------------------------------------------------------

class FakeEndpoint:
    """Serves scripted replies in order, recording every request body. A
    reply is a dict (sent as JSON with 200), an int status (an error), or a
    callable given the request that returns one of those."""

    def __init__(self):
        self.replies: list = []
        self.models: list[str] = ["tiny", "qwen3-coder:30b"]
        self.requests: list[dict] = []
        self.paths: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(json.loads(self.rfile.read(n)))
                outer.paths.append(self.path)
                outer.headers = {k.lower(): v for k, v in self.headers.items()}
                reply = outer.replies.pop(0) if outer.replies else 500
                if callable(reply):
                    reply = reply(outer.requests[-1])
                if isinstance(reply, int):
                    self.send_response(reply)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": {"message": f"scripted {reply}"}}).encode())
                    return
                body = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.paths.append(self.path)
                body = json.dumps({"data": [{"id": m} for m in outer.models]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    ep = FakeEndpoint()
    yield ep
    ep.close()


def openai_reply(text="", calls=(), finish=None, usage=(10, 5)):
    msg = {"role": "assistant", "content": text or None}
    if calls:
        msg["tool_calls"] = [{"id": cid, "type": "function",
                              "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}
                             for cid, name, args in calls]
    return {"model": "fake-1", "choices": [{"message": msg, "finish_reason": finish or ("tool_calls" if calls else "stop")}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}


def anthropic_reply(text="", calls=(), stop=None, usage=(10, 5)):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for cid, name, args in calls:
        content.append({"type": "tool_use", "id": cid, "name": name, "input": args})
    return {"type": "message", "model": "fake-a", "content": content,
            "stop_reason": stop or ("tool_use" if calls else "end_turn"),
            "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}}


def client(fake: FakeEndpoint, api="openai", **kw) -> Client:
    ep = Endpoint(fake.base_url + ("/v1" if api == "openai" else ""), "fake-model", api=api,
                  api_key="secret", retries=kw.pop("retries", 2), **kw)
    return Client(ep, sleep=lambda s: None)


def agent(fake: FakeEndpoint, tmp_path: Path, api="openai", mode="acceptEdits", allowed=None, **kw) -> NativeAgent:
    box = Toolbox().add(*core_tools(str(tmp_path), bash_timeout=5))
    return NativeAgent(client(fake, api), box, Permissions(mode, allowed or ["Bash(echo:*)", "Bash(git:*)"]),
                       "You are a test worker.", **kw)


# -- wire formats ------------------------------------------------------------------

def test_openai_round_trip_with_a_tool_call(fake, tmp_path):
    (tmp_path / "a.txt").write_text("hello\nworld\n")
    fake.replies = [openai_reply(calls=[("c1", "Read", {"path": "a.txt"})]),
                    openai_reply("The file says hello.")]
    a = agent(fake, tmp_path)
    assert a.run("read a.txt") == "The file says hello."

    first, second = fake.requests
    assert fake.paths == ["/v1/chat/completions"] * 2
    assert fake.headers["authorization"] == "Bearer secret"
    assert first["model"] == "fake-model" and first["stream"] is False
    assert first["messages"][0] == {"role": "system", "content": "You are a test worker."}
    assert first["messages"][1] == {"role": "user", "content": "read a.txt"}
    names = [t["function"]["name"] for t in first["tools"]]
    assert names == ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]
    assert all(t["type"] == "function" and "parameters" in t["function"] for t in first["tools"])

    # The second request carries the assistant's call and the tool's result.
    assistant = second["messages"][2]
    assert assistant["tool_calls"][0]["function"] == {"name": "Read", "arguments": '{"path": "a.txt"}'}
    tool = second["messages"][3]
    assert tool == {"role": "tool", "tool_call_id": "c1", "content": tool["content"]}
    assert "1\thello" in tool["content"] and "2\tworld" in tool["content"]
    assert a.usage == Usage(20, 10, 0) and a.model == "fake-1" and a.steps == 2


def test_anthropic_round_trip_with_tool_results_merged(fake, tmp_path):
    (tmp_path / "a.txt").write_text("a\n")
    (tmp_path / "b.txt").write_text("b\n")
    fake.replies = [anthropic_reply("Reading both.", calls=[("t1", "Read", {"path": "a.txt"}),
                                                            ("t2", "Read", {"path": "b.txt"})]),
                    anthropic_reply("Done.")]
    a = agent(fake, tmp_path, api="anthropic")
    assert a.run("read both") == "Done."

    assert fake.paths == ["/v1/messages"] * 2
    assert fake.headers["x-api-key"] == "secret" and fake.headers["anthropic-version"] == "2023-06-01"
    first, second = fake.requests
    assert first["system"] == "You are a test worker."
    assert first["messages"] == [{"role": "user", "content": "read both"}]
    assert first["tools"][0]["name"] == "Read" and "input_schema" in first["tools"][0]
    assert "stream" not in first
    assistant = second["messages"][1]
    assert assistant["content"][0] == {"type": "text", "text": "Reading both."}
    assert assistant["content"][1]["type"] == "tool_use" and assistant["content"][1]["input"] == {"path": "a.txt"}
    results = second["messages"][2]
    assert results["role"] == "user" and [b["tool_use_id"] for b in results["content"]] == ["t1", "t2"]
    assert all(b["type"] == "tool_result" for b in results["content"])
    assert a.model == "fake-a"


def test_anthropic_url_handles_a_v1_base(fake):
    assert Endpoint("http://h/api/anthropic", "m", api="anthropic").url() == "http://h/api/anthropic/v1/messages"
    assert Endpoint("http://h/v1/", "m", api="anthropic").url() == "http://h/v1/messages"
    assert Endpoint("http://h/v1", "m").url() == "http://h/v1/chat/completions"


def test_bad_tool_arguments_go_back_to_the_model_as_an_error(fake, tmp_path):
    fake.replies = [openai_reply(calls=[("c1", "Read", "{not json")]),
                    openai_reply(calls=[("c2", "Read", "[1, 2]")]),
                    openai_reply("gave up")]
    a = agent(fake, tmp_path)
    assert a.run("go") == "gave up"
    r1 = fake.requests[1]["messages"][-1]
    assert r1["role"] == "tool" and "not valid JSON" in r1["content"]
    r2 = fake.requests[2]["messages"][-1]
    assert "must be a JSON object" in r2["content"]
    # Both were recorded as errors in the conversation itself.
    assert [m["is_error"] for m in a.messages if m["role"] == "tool"] == [True, True]


def test_unknown_tool_is_an_error_not_a_crash(fake, tmp_path):
    fake.replies = [openai_reply(calls=[("c1", "Teleport", {})]), openai_reply("ok")]
    a = agent(fake, tmp_path)
    assert a.run("go") == "ok"
    assert "unknown tool 'Teleport'" in fake.requests[1]["messages"][-1]["content"]


def test_retries_transient_errors_then_gives_up_on_client_errors(fake, tmp_path):
    fake.replies = [503, 500, openai_reply("third time")]
    a = agent(fake, tmp_path)
    assert a.run("go") == "third time"
    assert len(fake.requests) == 3

    fake.replies = [401]
    with pytest.raises(ClientError) as e:
        a.run("again")
    assert e.value.status == 401 and "scripted 401" in str(e.value)

    fake.replies = [503, 503, 503]
    with pytest.raises(ClientError) as e:
        a.run("again")
    assert e.value.status == 503


def test_unreachable_endpoint_raises_client_error():
    ep = Endpoint("http://127.0.0.1:1", "m", retries=0, timeout=1)
    with pytest.raises(ClientError) as e:
        Client(ep, sleep=lambda s: None).complete(None, [{"role": "user", "content": "hi"}], [])
    assert "couldn't reach" in str(e.value)


def test_reply_cut_off_at_max_tokens_is_continued(fake, tmp_path):
    fake.replies = [openai_reply("first half", finish="length"), openai_reply("second half")]
    a = agent(fake, tmp_path)
    assert a.run("write a lot") == "second half"
    nudge = fake.requests[1]["messages"][-1]
    assert nudge["role"] == "user" and "cut off" in nudge["content"]


# -- the loop -----------------------------------------------------------------------

def test_queued_messages_are_delivered_between_model_calls(fake, tmp_path):
    queue = ["please also bump the version"]
    fake.replies = [openai_reply(calls=[("c1", "Bash", {"command": "echo hi"})]), openai_reply("done")]
    seen = []
    a = agent(fake, tmp_path, inbox=lambda: [queue.pop()] if queue else [], on_status=seen.append)
    assert a.run("start") == "done"
    # Drained before the first call: task, then the queued message.
    first = fake.requests[0]["messages"]
    assert first[1]["content"] == "start"
    assert first[2]["role"] == "user" and first[2]["content"].startswith("copse delivered this message")
    assert "bump the version" in first[2]["content"]
    assert seen == ["processing", "idle"]


def test_status_goes_idle_even_when_the_endpoint_fails(fake, tmp_path):
    fake.replies = [400]
    seen = []
    a = agent(fake, tmp_path, on_status=seen.append)
    with pytest.raises(ClientError):
        a.run("go")
    assert seen == ["processing", "idle"]


def test_denied_tool_returns_an_error_result_and_no_one_is_asked(fake, tmp_path):
    fake.replies = [openai_reply(calls=[("c1", "Bash", {"command": "rm -rf /"})]), openai_reply("ok")]
    a = agent(fake, tmp_path, mode="dontAsk")
    a.run("go")
    result = fake.requests[1]["messages"][-1]["content"]
    assert "not permitted" in result and "rm -rf /" in result
    assert not (tmp_path / "gone").exists()


def test_ask_verdict_uses_the_ask_callback_and_reports_waiting(fake, tmp_path):
    fake.replies = [openai_reply(calls=[("c1", "Bash", {"command": "touch made"})]), openai_reply("ok")]
    seen, asked = [], []

    def ask(tool, args):
        asked.append((tool, args["command"]))
        return True

    a = agent(fake, tmp_path, mode="default", on_status=seen.append, ask=ask)
    a.run("go")
    assert asked == [("Bash", "touch made")]
    assert (tmp_path / "made").exists()
    assert seen == ["processing", "waiting", "processing", "idle"]


def test_ask_without_a_callback_is_a_refusal(fake, tmp_path):
    fake.replies = [openai_reply(calls=[("c1", "Write", {"path": "x", "content": "1"})]), openai_reply("ok")]
    a = agent(fake, tmp_path, mode="default")
    a.run("go")
    assert "not permitted" in fake.requests[1]["messages"][-1]["content"]
    assert not (tmp_path / "x").exists()


def test_transcript_records_every_event(fake, tmp_path):
    fake.replies = [openai_reply(calls=[("c1", "Bash", {"command": "echo hi"})]), openai_reply("done")]
    log = tmp_path / "log" / "t.jsonl"
    a = agent(fake, tmp_path, transcript=log)
    a.run("go")
    entries = [json.loads(l) for l in log.read_text().splitlines()]
    assert [e["type"] for e in entries] == ["user", "assistant", "tool", "assistant"]
    assert entries[1]["tool_calls"][0]["name"] == "Bash" and entries[1]["message"]["usage"]["input_tokens"] == 10
    assert entries[2]["content"] == "hi" and all("ts" in e for e in entries)


def test_max_steps_ends_a_runaway_loop(fake, tmp_path):
    fake.replies = [openai_reply(calls=[("c", "Bash", {"command": "echo x"})])] * 5
    a = agent(fake, tmp_path, config=LoopConfig(max_steps=3))
    a.run("loop forever")
    assert a.steps == 3 and len(fake.requests) == 3


# -- context ------------------------------------------------------------------------

def test_context_is_trimmed_then_folded_to_fit(fake, tmp_path):
    big = "x" * 4000
    (tmp_path / "big.txt").write_text(big)
    # Twelve reads of a 4k file, then an answer: ~50k chars, budget 2k tokens.
    fake.replies = [openai_reply(calls=[(f"c{i}", "Read", {"path": "big.txt"})]) for i in range(12)]
    fake.replies.append(openai_reply("done"))
    a = agent(fake, tmp_path, config=LoopConfig(context_tokens=2000, keep_recent=4, old_result_chars=100))
    a._model_summary = lambda head, folded: None  # the deterministic fold; test_native_fold covers the model's
    assert a.run("the task") == "done"
    # Every request stayed near the budget: the recent messages kept verbatim
    # (two 4k results) are what's left over it.
    for req in fake.requests:
        assert sum(len(json.dumps(m)) for m in req["messages"]) // 4 < 2000 + 1500
    # Folds accumulate into one summary rather than summarizing the summary.
    assert a.messages[1]["content"].count("summarized by copse") == 1
    assert a.messages[1]["content"].count("you ran Read big.txt") >= 6
    # The task survived at the front, and the fold left a readable summary.
    assert a.messages[0]["content"] == "the task"
    assert a.folded > 0
    assert a.messages[1]["role"] == "user" and "summarized by copse" in a.messages[1]["content"]
    assert "you ran Read big.txt" in a.messages[1]["content"]
    assert a.messages[2]["role"] == "assistant"
    assert a.messages[3]["role"] != "tool"  # no orphaned tool result after the fold
    # The conversation is still well-formed: every tool result follows its call.
    ids = set()
    for m in a.messages:
        if m["role"] == "assistant":
            ids |= {c.id for c in m["tool_calls"]}
        elif m["role"] == "tool":
            assert m["tool_call_id"] in ids


def test_small_conversations_are_left_alone(fake, tmp_path):
    fake.replies = [openai_reply("hi")]
    a = agent(fake, tmp_path)
    a.run("hello")
    assert a.folded == 0 and [m["role"] for m in a.messages] == ["user", "assistant"]


# -- tools ----------------------------------------------------------------------------

@pytest.fixture
def box(tmp_path):
    return Toolbox().add(*core_tools(str(tmp_path), bash_timeout=2))


def test_read_pages_and_reports_missing_files(box, tmp_path):
    (tmp_path / "f.txt").write_text("\n".join(f"line {i}" for i in range(1, 11)))
    r = box.call("Read", {"path": "f.txt", "offset": 3, "limit": 2})
    assert r.content.splitlines() == ["     3\tline 3", "     4\tline 4", "[6 more lines; read with offset=5]"]
    assert box.call("Read", {"path": "nope"}).is_error
    assert box.call("Read", {"path": "f.txt", "offset": 99}).is_error
    (tmp_path / "bin").write_bytes(b"\xff\xfe\x00")
    assert "isn't a text file" in box.call("Read", {"path": "bin"}).content


def test_write_creates_parents_and_edit_replaces_exactly_once(box, tmp_path):
    r = box.call("Write", {"path": "pkg/mod.py", "content": "a = 1\nb = 2\na = 1\n"})
    assert not r.is_error and (tmp_path / "pkg/mod.py").read_text().startswith("a = 1")
    r = box.call("Edit", {"path": "pkg/mod.py", "old_string": "a = 1", "new_string": "a = 9"})
    assert r.is_error and "appears 2 times" in r.content
    r = box.call("Edit", {"path": "pkg/mod.py", "old_string": "a = 1", "new_string": "a = 9", "replace_all": True})
    assert not r.is_error and (tmp_path / "pkg/mod.py").read_text() == "a = 9\nb = 2\na = 9\n"
    r = box.call("Edit", {"path": "pkg/mod.py", "old_string": "    b = 2", "new_string": "b = 3"})
    assert r.is_error and "Closest line: 'b = 2'" in r.content
    r = box.call("Edit", {"path": "pkg/mod.py", "old_string": "b = 2", "new_string": "b = 3"})
    assert not r.is_error and "b = 3" in (tmp_path / "pkg/mod.py").read_text()
    assert box.call("Edit", {"path": "pkg/mod.py", "old_string": "", "new_string": "x"}).is_error


def test_write_refuses_to_replace_a_file_the_model_has_not_read(box, tmp_path):
    """A model that has lost the thread "creates" README.md over the real one
    (seen on qwen3-coder: 661 lines replaced by a 3-line stub). An existing
    file is only replaced whole once the model has looked at it, by Read or
    by an Edit of it; a file it wrote itself counts as seen too."""
    (tmp_path / "README.md").write_text("# real\n" * 100)
    r = box.call("Write", {"path": "README.md", "content": "# stub\n"})
    assert r.is_error and "haven't read it" in r.content
    assert (tmp_path / "README.md").read_text() == "# real\n" * 100
    box.call("Read", {"path": "README.md", "limit": 1})
    r = box.call("Write", {"path": "README.md", "content": "# rewritten\n"})
    assert not r.is_error and (tmp_path / "README.md").read_text() == "# rewritten\n"
    # Edit reads the file to match, so it counts as having seen it.
    (tmp_path / "other.txt").write_text("x = 1\n")
    box.call("Edit", {"path": "other.txt", "old_string": "x = 1", "new_string": "x = 2"})
    assert not box.call("Write", {"path": "other.txt", "content": "x = 3\n"}).is_error
    # A file the model created itself can be rewritten freely.
    box.call("Write", {"path": "new.txt", "content": "a\n"})
    assert not box.call("Write", {"path": "new.txt", "content": "b\n"}).is_error


def test_file_tools_stay_inside_the_working_directory(box, tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    for args in ({"path": "../outside.txt"}, {"path": str(outside)}):
        r = box.call("Read", args)
        assert r.is_error and "outside the working directory" in r.content
    r = box.call("Write", {"path": "../escaped.txt", "content": "x"})
    assert r.is_error and not (tmp_path.parent / "escaped.txt").exists()


def test_glob_and_grep(box, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("def alpha():\n    return 1\n")
    (tmp_path / "src/b.py").write_text("def beta():\n    return alpha()\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/c.py").write_text("alpha")
    assert box.call("Glob", {"pattern": "**/*.py"}).content.splitlines() == ["src/a.py", "src/b.py"]
    r = box.call("Grep", {"pattern": r"alpha\(\)", "path": "src"})
    assert r.content.splitlines() == ["src/a.py:1:def alpha():", "src/b.py:2:return alpha()"]
    assert box.call("Grep", {"pattern": "ALPHA", "glob": "*.py", "ignore_case": True}).content.count("\n") == 1
    assert box.call("Grep", {"pattern": "zzz"}).content == "no matches"
    assert box.call("Grep", {"pattern": "("}).is_error


def test_bash_reports_exit_codes_timeouts_and_clips_output(box, tmp_path):
    r = box.call("Bash", {"command": "echo out; echo err >&2"})
    assert not r.is_error and r.content == "out\nerr"
    r = box.call("Bash", {"command": "echo boom; exit 3"})
    assert r.is_error and r.content == "boom\n[exit code 3]"
    r = box.call("Bash", {"command": "sleep 5", "timeout": 60})  # capped at the box's 2s
    assert r.is_error and "timed out after 2s" in r.content
    r = box.call("Bash", {"command": "cat"})  # no stdin to hang on
    assert not r.is_error
    box.max_output = 1000
    r = box.call("Bash", {"command": "python3 -c 'print(\"y\" * 5000)'"})
    assert len(r.content) < 1100 and "characters cut" in r.content


def test_clip_keeps_both_ends():
    text = "A" * 100 + "B" * 100
    out = clip(text, 60)
    assert out.startswith("A" * 40) and out.endswith("B" * 20) and "140 characters cut" in out
    assert clip("short", 60) == "short"


def test_tool_exceptions_become_error_results(box):
    from copse.native.tools import Tool

    def boom(args):
        raise RuntimeError("kaput")

    box.add(Tool("Boom", "explodes", {"type": "object"}, boom))
    r = box.call("Boom", {})
    assert r.is_error and "RuntimeError: kaput" in r.content
    assert isinstance(box.specs()[-1], ToolSpec)


# -- permissions --------------------------------------------------------------------------

def test_bash_prefix_and_exact_rules():
    assert bash_matches("git add:*", "git add -A")
    assert bash_matches("git add:*", "git add")
    assert not bash_matches("git add:*", "git addx")
    assert not bash_matches("git add:*", "git push")
    assert bash_matches("pytest", "pytest")
    assert not bash_matches("pytest", "pytest -x")
    # Every part of a compound command must be covered.
    assert bash_matches("git:*", "git add -A && git commit -m x; git status | git log")
    assert not bash_matches("git:*", "git add -A && rm -rf x")
    assert not bash_matches("git:*", "git log $(evil)")
    assert not bash_matches("git:*", "git log `evil`")
    assert not bash_matches("git:*", "git log 'unclosed")
    assert split_commands("a b&&c  d ||e;f|g") == ["a b", "c d", "e", "f", "g"]


def test_each_part_of_a_compound_command_may_match_a_different_rule():
    p = Permissions("acceptEdits", ["Bash(git add:*)", "Bash(git commit:*)", "Bash(pytest:*)"])
    assert p.decide("Bash", {"command": "git add -A && git commit -m 'fix add'"}) == "allow"
    assert p.decide("Bash", {"command": "git add -A && git push"}) == "ask"
    assert p.reason("Bash", {"command": "git add -A && git push"}) == \
        "`git push` isn't covered by the allowed commands (each part of a compound command must be)"
    assert p.reason("Bash", {"command": "rm -rf x"}) == "it isn't covered by the allowed commands"
    assert "substitution" in p.reason("Bash", {"command": "git add $(ls)"})
    assert p.reason("Edit", {"path": "x"}) == ""


def test_tool_calls_written_as_text_are_recovered(fake, tmp_path):
    (tmp_path / "a.txt").write_text("x\n")
    fake.replies = [
        openai_reply('Let me look.<tool_call>\n{"name": "Read", "arguments": {"path": "a.txt"}}\n</tool_call>'),
        openai_reply("Now the second.<tool_call>\n{\"name\": \"Bash\", \"arg"),  # cut off mid-call
        openai_reply(calls=[("c3", "Bash", {"command": "echo ok"})]),
        openai_reply("done<tool_call>"),  # a bare tag: asked again, once more
        openai_reply("really done"),
    ]
    a = agent(fake, tmp_path)
    assert a.run("go") == "really done"
    msgs = fake.requests[1]["messages"]
    assert msgs[-2]["tool_calls"][0]["function"]["name"] == "Read" and msgs[-2]["content"] == "Let me look."
    assert "1\tx" in msgs[-1]["content"]
    nudge = fake.requests[2]["messages"][-1]
    assert nudge["role"] == "user" and "didn't come through" in nudge["content"]
    assert fake.requests[2]["messages"][-2]["content"] == "Now the second."
    assert "didn't come through" in fake.requests[4]["messages"][-1]["content"]


def test_modes_and_rules():
    p = Permissions("acceptEdits", ["Bash(uv run:*)", "Bash(pytest)"])
    assert p.decide("Read", {"path": "x"}) == "allow"
    assert p.decide("Edit", {"path": "x"}) == "allow"
    assert p.decide("Bash", {"command": "uv run pytest -q"}) == "allow"
    assert p.decide("Bash", {"command": "pytest"}) == "allow"
    assert p.decide("Bash", {"command": "pytest -x"}) == "ask"
    assert p.decide("mcp__copse__report_result", {}) == "allow"

    assert Permissions("dontAsk", []).decide("Write", {"path": "x"}) == "deny"
    assert Permissions("dont-ask", []).decide("Bash", {"command": "ls"}) == "deny"
    assert Permissions("plan", []).decide("Edit", {"path": "x"}) == "deny"
    assert Permissions(None, []).decide("Edit", {"path": "x"}) == "ask"
    assert Permissions("auto", []).decide("Edit", {"path": "x"}) == "allow"
    assert Permissions("auto", []).decide("Bash", {"command": "ls"}) == "ask"
    assert Permissions("bypassPermissions", []).decide("Bash", {"command": "anything"}) == "allow"

    p = Permissions("dontAsk", ["Edit(src/*)", "Write", "Bash"])
    assert p.decide("Edit", {"path": "src/a.py"}) == "allow"
    assert p.decide("Edit", {"path": "tests/a.py"}) == "deny"
    assert p.decide("Write", {"path": "anywhere"}) == "allow"
    assert p.decide("Bash", {"command": "rm -rf x"}) == "allow"  # a bare Bash rule allows everything
