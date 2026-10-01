"""One ``complete`` call over either chat wire format.

Messages are kept in copse's own shape and converted per request, so the
loop never sees the difference between backends:

    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "...", "tool_calls": [ToolCall, ...]}
    {"role": "tool", "tool_call_id": "...", "name": "...", "content": "...", "is_error": bool}

The system prompt is passed separately (OpenAI wants it as a message,
Anthropic as a top-level field). Only the standard library is used: a
worker's dependencies should stay copse's own.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

ANTHROPIC_VERSION = "2023-06-01"
RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON schema for the arguments object


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    # Set when the model's arguments weren't a JSON object: the loop hands
    # this back as the tool's error so the model can try again.
    parse_error: str | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens,
                     self.output_tokens + other.output_tokens,
                     self.cache_read_tokens + other.cache_read_tokens)


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str  # 'stop' | 'tool_calls' | 'length' | other backend value
    usage: Usage = field(default_factory=Usage)
    model: str | None = None
    # The text ended in an unfinished tool call the backend didn't parse
    # (e.g. a bare "<tool_call>" from Qwen): the loop asks for it again.
    truncated_call: bool = False


TOOL_CALL_BLOCK = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
DANGLING_CALL = re.compile(r"<tool_call>\s*(\{.*)?$", re.S)


def recover_text_tool_calls(text: str, calls: list[ToolCall]) -> tuple[str, list[ToolCall], bool]:
    """Some models write tool calls into their text (Qwen's ``<tool_call>``
    blocks) when the backend's parser misses them. Complete blocks become
    ToolCalls; an unfinished one at the end is cut off and flagged."""
    if "<tool_call>" not in text:
        return text, calls, False
    found: list[ToolCall] = []

    def take(m: re.Match) -> str:
        try:
            data = json.loads(m.group(1))
        except ValueError:
            return m.group(0)  # not JSON: leave it in the text
        if isinstance(data, dict) and data.get("name"):
            args = data.get("arguments", data.get("parameters", {}))
            found.append(_tool_call(f"text_{len(calls) + len(found)}", str(data["name"]), args))
            return ""
        return m.group(0)

    text = TOOL_CALL_BLOCK.sub(take, text)
    truncated = False
    m = DANGLING_CALL.search(text)
    if m:
        text = text[:m.start()]
        truncated = True
    return text.strip(), calls + found, truncated


@dataclass
class Endpoint:
    """Where and how to talk to the model."""

    base_url: str
    model: str
    api: str = "openai"          # 'openai' (chat completions) | 'anthropic' (messages)
    api_key: str | None = None
    max_tokens: int = 8192
    timeout: float = 600.0       # a slow local model can take minutes per reply
    retries: int = 3
    headers: dict[str, str] = field(default_factory=dict)

    def url(self) -> str:
        base = self.base_url.rstrip("/")
        if self.api == "anthropic":
            return base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        return base + "/chat/completions"


class ClientError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class Client:
    def __init__(self, endpoint: Endpoint, sleep=time.sleep,
                 on_text: Callable[[str], None] | None = None):
        self.endpoint = endpoint
        self._sleep = sleep
        self.on_text = on_text

    # -- the one public call ------------------------------------------------

    def complete(self, system: str | None, messages: list[dict], tools: list[ToolSpec],
                 on_text: Callable[[str], None] | None = None) -> Reply:
        """One model call. With an ``on_text`` (here or on the client) the
        reply is streamed and each text delta is handed to it as it arrives;
        the Reply is the same either way. An endpoint that refuses the
        stream request with an HTTP error is asked again without streaming."""
        on_text = on_text or self.on_text
        anthropic = self.endpoint.api == "anthropic"
        payload = (self._anthropic_request if anthropic else self._openai_request)(system, messages, tools)
        parse = self._parse_anthropic if anthropic else self._parse_openai
        if on_text is None:
            return parse(self._post(payload))
        payload["stream"] = True
        if not anthropic:
            payload["stream_options"] = {"include_usage": True}
        try:
            return parse(self._post(payload, lambda resp: self._read_stream(resp, anthropic, on_text)))
        except ClientError as e:
            if e.status is None:
                raise
        payload["stream"] = False
        payload.pop("stream_options", None)
        return parse(self._post(payload))

    # -- transport ------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json",
             "User-Agent": "copse-native"}
        key = self.endpoint.api_key
        if self.endpoint.api == "anthropic":
            h["anthropic-version"] = ANTHROPIC_VERSION
            if key:
                h["x-api-key"] = key
                h["Authorization"] = f"Bearer {key}"  # gateways that want it this way
        elif key:
            h["Authorization"] = f"Bearer {key}"
        h.update(self.endpoint.headers)
        return h

    def _read_stream(self, resp, anthropic: bool, on_text: Callable[[str], None]) -> dict:
        """Read an SSE response to its end and return the body a
        non-streaming request would have had, so the ordinary parsers apply."""
        if "event-stream" not in (resp.headers.get("Content-Type") or ""):
            # The endpoint ignored "stream": a plain JSON body, shown in one piece.
            body = resp.read().decode("utf-8", "replace")
            try:
                data = json.loads(body)
            except ValueError as e:
                raise ClientError(f"the endpoint's reply wasn't JSON: {e}", body=body[:2000]) from None
            try:
                shown = (self._parse_anthropic if anthropic else self._parse_openai)(data).text
            except ClientError:
                shown = ""
            if shown:
                on_text(shown)
            return data
        text: list[str] = []
        finish = None
        model = None
        usage: dict = {}
        oa_calls: dict[int, dict] = {}      # openai: tool calls by index
        blocks: dict[int, dict] = {}        # anthropic: content blocks by index
        stop_reason = None

        def emit(t: str) -> None:
            if t:
                text.append(t)
                on_text(t)

        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue  # blank separators, 'event:' names, comments
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    ev = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("error") or ev.get("type") == "error":
                    raise ClientError(f"error from the endpoint: {_error_text(json.dumps(ev))}")
                if anthropic:
                    kind = ev.get("type")
                    if kind == "message_start":
                        msg = ev.get("message") or {}
                        model = msg.get("model") or model
                        usage.update(msg.get("usage") or {})
                    elif kind == "content_block_start":
                        blocks[ev.get("index", 0)] = dict(ev.get("content_block") or {}, _json=[])
                        first = blocks[ev.get("index", 0)]
                        if first.get("type") == "text":
                            emit(first.get("text") or "")
                    elif kind == "content_block_delta":
                        block = blocks.setdefault(ev.get("index", 0), {"type": "text", "_json": []})
                        delta = ev.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            block["text"] = block.get("text", "") + (delta.get("text") or "")
                            emit(delta.get("text") or "")
                        elif delta.get("type") == "input_json_delta":
                            block["_json"].append(delta.get("partial_json") or "")
                    elif kind == "message_delta":
                        stop_reason = (ev.get("delta") or {}).get("stop_reason") or stop_reason
                        usage.update(ev.get("usage") or {})
                else:
                    model = ev.get("model") or model
                    usage = ev.get("usage") or usage
                    for choice in ev.get("choices") or []:
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if isinstance(content, str):
                            emit(content)
                        for c in delta.get("tool_calls") or []:
                            slot = oa_calls.setdefault(c.get("index", len(oa_calls)),
                                                       {"id": None, "name": "", "arguments": ""})
                            slot["id"] = c.get("id") or slot["id"]
                            fn = c.get("function") or {}
                            slot["name"] += fn.get("name") or ""
                            slot["arguments"] += fn.get("arguments") or ""
                        finish = choice.get("finish_reason") or finish
        except (OSError, TimeoutError) as e:
            raise ClientError(f"the stream from {self.endpoint.url()} broke off: {e}") from None
        joined = "".join(text)
        if anthropic:
            content = []
            for _, b in sorted(blocks.items()):
                raw_json = "".join(b.pop("_json", []))
                if b.get("type") == "tool_use":
                    b["input"] = raw_json if raw_json.strip() else b.get("input") or {}
                content.append(b)
            return {"type": "message", "model": model, "content": content,
                    "stop_reason": stop_reason, "usage": usage}
        message: dict = {"role": "assistant", "content": joined or None}
        if oa_calls:
            message["tool_calls"] = [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                for _, c in sorted(oa_calls.items())]
        return {"model": model, "choices": [{"message": message, "finish_reason": finish}], "usage": usage}

    def _post(self, payload: dict, read: Callable | None = None) -> dict:
        """POST ``payload``; ``read`` (given the open response) turns it into
        the result, by default the JSON body."""
        data = json.dumps(payload).encode("utf-8")
        last: ClientError | None = None
        # Defence in depth for air-gap mode: the profile gate at launch is the
        # first line, this refuses an endpoint that still isn't local (a
        # base_url overridden after the gate, or a profile wrongly marked local).
        from copse import airgap

        try:
            airgap.guard(self.endpoint.url(), "model request")
        except airgap.AirGapError as e:
            raise ClientError(str(e)) from None
        for attempt in range(self.endpoint.retries + 1):
            req = urllib.request.Request(self.endpoint.url(), data=data, headers=self._headers(),
                                         method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.endpoint.timeout) as resp:
                    if read is not None:
                        return read(resp)
                    body = resp.read().decode("utf-8", "replace")
                try:
                    return json.loads(body)
                except ValueError as e:
                    raise ClientError(f"the endpoint's reply wasn't JSON: {e}", body=body[:2000]) from None
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace") if e.fp else ""
                last = ClientError(f"HTTP {e.code} from {self.endpoint.url()}: {_error_text(body)}",
                                   status=e.code, body=body[:2000])
                if e.code not in RETRY_STATUSES:
                    raise last from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = ClientError(f"couldn't reach {self.endpoint.url()}: {e}")
            if attempt < self.endpoint.retries:
                self._sleep(min(2 ** attempt, 20))
        assert last is not None
        raise last

    # -- OpenAI chat completions --------------------------------------------

    def _openai_request(self, system: str | None, messages: list[dict], tools: list[ToolSpec]) -> dict:
        out: list[dict] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            role = m["role"]
            if role == "assistant":
                entry: dict = {"role": "assistant", "content": m.get("content") or None}
                calls = m.get("tool_calls") or []
                if calls:
                    entry["tool_calls"] = [{
                        "id": c.id, "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    } for c in calls]
                out.append(entry)
            elif role == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                out.append({"role": role, "content": m["content"]})
        payload: dict = {"model": self.endpoint.model, "messages": out,
                         "max_tokens": self.endpoint.max_tokens, "stream": False}
        if tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters,
            }} for t in tools]
        return payload

    @staticmethod
    def _parse_openai(data: dict) -> Reply:
        choices = data.get("choices") or []
        if not choices:
            raise ClientError(f"no choices in reply: {_error_text(json.dumps(data))}")
        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):  # some gateways return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        calls = []
        for i, c in enumerate(message.get("tool_calls") or []):
            fn = c.get("function") or {}
            calls.append(_tool_call(c.get("id") or f"call_{i}", fn.get("name") or "", fn.get("arguments")))
        text, calls, truncated = recover_text_tool_calls(text, calls)
        finish = choice.get("finish_reason") or ("tool_calls" if calls else "stop")
        stop = {"stop": "stop", "tool_calls": "tool_calls", "length": "length"}.get(finish, finish)
        if calls and stop == "stop":
            stop = "tool_calls"
        u = data.get("usage") or {}
        details = u.get("prompt_tokens_details") or {}
        usage = Usage(int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0),
                      int(details.get("cached_tokens") or 0))
        return Reply(text, calls, stop, usage, data.get("model"), truncated_call=truncated)

    # -- Anthropic messages ---------------------------------------------------

    def _anthropic_request(self, system: str | None, messages: list[dict], tools: list[ToolSpec]) -> dict:
        out: list[dict] = []
        for m in messages:
            role = m["role"]
            if role == "assistant":
                blocks: list[dict] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for c in m.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            elif role == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                if m.get("is_error"):
                    block["is_error"] = True
                # Consecutive tool results share one user message, as the API requires.
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                        and out[-1]["content"] and out[-1]["content"][-1].get("type") == "tool_result":
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            else:
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append({"type": "text", "text": m["content"]})
                else:
                    out.append({"role": "user", "content": m["content"]})
        payload: dict = {"model": self.endpoint.model, "messages": out,
                         "max_tokens": self.endpoint.max_tokens}
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [{"name": t.name, "description": t.description,
                                 "input_schema": t.parameters} for t in tools]
        return payload

    @staticmethod
    def _parse_anthropic(data: dict) -> Reply:
        if data.get("type") == "error":
            raise ClientError(f"error from the endpoint: {_error_text(json.dumps(data))}")
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for i, block in enumerate(data.get("content") or []):
            kind = block.get("type")
            if kind == "text":
                text_parts.append(block.get("text") or "")
            elif kind == "tool_use":
                calls.append(_tool_call(block.get("id") or f"toolu_{i}", block.get("name") or "",
                                        block.get("input")))
        text, calls, truncated = recover_text_tool_calls("".join(text_parts), calls)
        reason = data.get("stop_reason") or ("tool_use" if calls else "end_turn")
        stop = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length",
                "stop_sequence": "stop"}.get(reason, reason)
        if calls and stop == "stop":
            stop = "tool_calls"
        u = data.get("usage") or {}
        usage = Usage(int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                      int(u.get("cache_read_input_tokens") or 0))
        return Reply(text, calls, stop, usage, data.get("model"), truncated_call=truncated)


def _tool_call(call_id: str, name: str, arguments) -> ToolCall:
    """A ToolCall from whatever the model sent as arguments: a JSON string
    (OpenAI), an object (Anthropic), or something broken."""
    if arguments is None or arguments == "":
        return ToolCall(call_id, name, {})
    if isinstance(arguments, dict):
        return ToolCall(call_id, name, arguments)
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except ValueError as e:
            return ToolCall(call_id, name, {}, parse_error=f"arguments were not valid JSON ({e}): {arguments[:500]}")
        if isinstance(parsed, dict):
            return ToolCall(call_id, name, parsed)
        return ToolCall(call_id, name, {}, parse_error=f"arguments must be a JSON object, got {type(parsed).__name__}")
    return ToolCall(call_id, name, {}, parse_error=f"arguments must be a JSON object, got {type(arguments).__name__}")


def _error_text(body: str) -> str:
    """The message inside an error body, if it has the usual shape."""
    try:
        data = json.loads(body)
    except ValueError:
        return body[:300].strip() or "(empty body)"
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])[:300]
    if isinstance(err, str):
        return err[:300]
    return body[:300].strip() or "(empty body)"
