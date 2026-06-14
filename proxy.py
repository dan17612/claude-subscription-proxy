"""claude-subscription-proxy.

A tiny local HTTP server that speaks the Anthropic Messages API (and an
OpenAI-compatible /v1/chat/completions endpoint) but, instead of calling
api.anthropic.com with an API key, spawns the official `claude` CLI in
`--print` mode. The CLI is already authenticated against your Claude
subscription (OAuth, stored in your OS keychain), so any app that talks the
Anthropic API can run on your subscription instead of a metered API key.

    your app ──HTTP──▶ proxy (this file) ──spawn──▶ claude --print
                                                       │
                                                       ▼
                                              api.anthropic.com
                                              (subscription auth)

Auth: relies on the `claude` CLI being logged in (`claude` then `/login`).
No API key is read or required by this proxy; whatever ANTHROPIC_API_KEY your
client sends is ignored — the CLI's own OAuth session is what's used.

Run: `./run.sh` (creates a venv if missing, then listens on 127.0.0.1:3456).

Notes:
  * Loopback only by default. There is NO authentication on these endpoints —
    do not expose the port to a network you don't trust.
  * This is personal tooling. Use it within Anthropic's terms for your plan.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import tempfile
import time
import uuid
from typing import Any

from aiohttp import web

LOG = logging.getLogger("claude-subscription-proxy")

PORT = int(os.environ.get("PROXY_PORT", "3456"))
HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
DEFAULT_MODEL = os.environ.get("PROXY_DEFAULT_MODEL", "sonnet")
SUBPROCESS_TIMEOUT = int(os.environ.get("PROXY_SUBPROCESS_TIMEOUT", "1800"))

# Where small bits of state (quota tracking) live.
STATE_DIR = os.path.expanduser(os.environ.get("PROXY_STATE_DIR", "~/.claude-subscription-proxy"))
QUOTA_STATE_FILE = os.path.join(STATE_DIR, "quota.json")

# Quota tracking — surfaces a warning when the subscription's rolling usage
# window is filling up, so the model can wrap up gracefully instead of getting
# cut off mid-task. We rely on Anthropic's authoritative `rate_limit_info`
# fields (status flips "allowed" -> "throttled"/"denied"; resetsAt tells us how
# long until the window resets). We DON'T threshold on `total_cost_usd`: on a
# flat-rate subscription that figure reflects the equivalent API price, not what
# you actually pay. It's recorded for visibility (`/quota`) only.
QUOTA_WARN_RESET_MIN = int(os.environ.get("PROXY_QUOTA_WARN_RESET_MIN", "20"))
QUOTA_HEAVY_TURNS = int(os.environ.get("PROXY_QUOTA_HEAVY_TURNS", "120"))
QUOTA_SESSION_WINDOW_S = 5 * 3600


class QuotaTracker:
    """Watches `claude --print` rate_limit + result events and exposes a
    `<proxy_quota_warning>` block when the subscription window is filling up.

    Signals (in order of authority):
      1. `status != "allowed"`                       -> hard signal from Anthropic
      2. `resets_at - now < QUOTA_WARN_RESET_MIN`    -> wrap up before reset
      3. `turns_in_session > QUOTA_HEAVY_TURNS`      -> soft heuristic for heavy use

    State persisted to STATE_DIR/quota.json (atomic write). Cumulative
    `session_cost_usd` is recorded for visibility but NOT used for thresholds.
    """

    def __init__(self, state_file: str):
        self.state_file = state_file
        self._cache: dict = {
            "status": "allowed",
            "resets_at": 0,
            "rate_limit_type": None,
            "overage_status": None,
            "is_using_overage": False,
            "session_cost_usd": 0.0,
            "session_turns": 0,
            "session_started_at": time.time(),
            "last_event_at": 0,
        }
        self._load()

    def _load(self) -> None:
        try:
            with open(self.state_file) as f:
                stored = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._save()
            return
        if time.time() - stored.get("session_started_at", 0) > QUOTA_SESSION_WINDOW_S:
            stored["session_cost_usd"] = 0.0
            stored["session_turns"] = 0
            stored["session_started_at"] = time.time()
        self._cache.update(stored)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._cache, f, indent=2)
        os.replace(tmp, self.state_file)

    def _maybe_reset_window(self) -> None:
        if time.time() - self._cache.get("session_started_at", 0) > QUOTA_SESSION_WINDOW_S:
            self._cache["session_cost_usd"] = 0.0
            self._cache["session_turns"] = 0
            self._cache["session_started_at"] = time.time()

    def update_rate_limit(self, info: dict) -> None:
        if not isinstance(info, dict):
            return
        for src, dst in (("status", "status"),
                         ("resetsAt", "resets_at"),
                         ("rateLimitType", "rate_limit_type"),
                         ("overageStatus", "overage_status"),
                         ("isUsingOverage", "is_using_overage")):
            if src in info:
                self._cache[dst] = info[src]
        self._cache["last_event_at"] = time.time()
        self._save()

    def update_turn(self, cost: float | None = None) -> None:
        """Called once per `result` event = once per round-trip turn."""
        self._maybe_reset_window()
        self._cache["session_turns"] = int(self._cache.get("session_turns", 0)) + 1
        try:
            c = float(cost) if cost is not None else 0.0
        except (TypeError, ValueError):
            c = 0.0
        if c > 0:
            self._cache["session_cost_usd"] = round(
                self._cache.get("session_cost_usd", 0.0) + c, 4
            )
        self._save()

    def maybe_warning_block(self) -> str | None:
        reasons: list[str] = []
        status = self._cache.get("status")
        if status not in (None, "allowed"):
            reasons.append(f"status={status}")

        resets_at = float(self._cache.get("resets_at") or 0)
        mins_to_reset = max(0, int((resets_at - time.time()) / 60)) if resets_at > 0 else None
        if mins_to_reset is not None and 0 < mins_to_reset < QUOTA_WARN_RESET_MIN:
            reasons.append(f"resets_in_min={mins_to_reset}")

        turns = int(self._cache.get("session_turns", 0))
        if turns >= QUOTA_HEAVY_TURNS:
            reasons.append(f"turns={turns}")

        if not reasons:
            return None

        reset_str = str(mins_to_reset) if mins_to_reset is not None else "?"
        return (
            "<proxy_quota_warning>\n"
            f"signals: {' | '.join(reasons)}\n"
            f"turns_this_session={turns} resets_in_minutes={reset_str}\n"
            "action: wrap up now — stop issuing new tool calls, summarize "
            "progress, and end the turn before the usage window resets.\n"
            "</proxy_quota_warning>"
        )

    def snapshot(self) -> dict:
        return {
            **self._cache,
            "config": {
                "warn_reset_min": QUOTA_WARN_RESET_MIN,
                "heavy_turns": QUOTA_HEAVY_TURNS,
                "session_window_s": QUOTA_SESSION_WINDOW_S,
            },
        }


_quota = QuotaTracker(QUOTA_STATE_FILE)


def _ingest_event(event: dict) -> None:
    """Pull rate_limit_event and result events into the quota tracker."""
    t = event.get("type")
    if t == "rate_limit_event":
        info = event.get("rate_limit_info") or {}
        if info:
            _quota.update_rate_limit(info)
    elif t == "result":
        _quota.update_turn(cost=event.get("total_cost_usd"))


def normalize_system(system: Any) -> str:
    """Anthropic accepts `system` as either a string or a list of text blocks."""
    if not system:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n\n".join(p for p in parts if p)
    return str(system)


# Internal protocol tags. `claude --print` can't accept custom tool definitions
# on the CLI, so we serialize the request's tools into the system prompt and ask
# the model to emit calls wrapped in these tags. We then parse them back into
# native Anthropic tool_use blocks. The deliberately unusual `proxy_` prefix
# keeps the model from drifting into the more common tag shapes it saw in
# pre-training (<tool_use>, <function_call>, ...).
TOOL_TAG_OPEN = "<proxy_tool_call>"
TOOL_TAG_CLOSE = "</proxy_tool_call>"
TOOL_BLOCK_RE = re.compile(
    re.escape(TOOL_TAG_OPEN) + r"\s*(\{.*?\})\s*" + re.escape(TOOL_TAG_CLOSE),
    re.DOTALL,
)


def _flatten_content(content: Any) -> str:
    """Render an Anthropic content array (or string) as plain text. Prior
    tool_use blocks are serialized using the SAME tag we ask the model to emit,
    so it sees one consistent format across history and instructions."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "tool_use":
            payload = json.dumps({
                "name": block.get("name"),
                "input": block.get("input", {}),
            }, ensure_ascii=False)
            parts.append(f"{TOOL_TAG_OPEN}{payload}{TOOL_TAG_CLOSE}")
        elif btype == "tool_result":
            inner = block.get("content")
            if isinstance(inner, list):
                inner = "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in inner)
            parts.append(
                f'<proxy_tool_result for="{block.get("tool_use_id")}">\n'
                f'{inner}\n'
                f'</proxy_tool_result>'
            )
        elif btype == "image":
            parts.append("[image omitted]")
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(p for p in parts if p)


def messages_to_stream_json(messages: list[dict]) -> str:
    """Serialize an Anthropic `messages` array as a single user-role stream-json
    event suitable for `claude --input-format stream-json` stdin.

    Important: claude --print stream-json input only consumes the FIRST event,
    everything after is ignored. So we collapse the whole conversation history
    into one user message: previous turns are rendered as a transcript, the
    final user message is highlighted as the current request."""
    if not messages:
        return ""

    history: list[dict] = list(messages)
    final_user: dict | None = None
    if history and history[-1].get("role") == "user":
        final_user = history.pop()

    transcript_lines: list[str] = []
    for msg in history:
        role = msg.get("role", "user")
        if role not in ("user", "assistant"):
            continue
        text = _flatten_content(msg.get("content"))
        if not text.strip():
            continue
        prefix = "USER" if role == "user" else "ASSISTANT"
        transcript_lines.append(f"{prefix}: {text}")

    pieces: list[str] = []
    if transcript_lines:
        pieces.append("=== Previous conversation ===")
        pieces.extend(transcript_lines)
        pieces.append("=== End previous conversation ===\n")
    if final_user is not None:
        pieces.append("=== Current user message ===")
        pieces.append(_flatten_content(final_user.get("content")))
        pieces.append("=== End current user message ===")
        pieces.append("")
        pieces.append("Reply to the current user message. Follow the system "
                      "instructions strictly. If a tool call is required, emit "
                      "the appropriate " + TOOL_TAG_OPEN + " tag.")
    elif not transcript_lines:
        # No history, no final user — extreme edge case. Fall back to empty.
        pieces.append("(no message)")

    combined = "\n".join(pieces)
    event = {
        "type": "user",
        "message": {"role": "user",
                    "content": [{"type": "text", "text": combined}]},
    }
    return json.dumps(event, ensure_ascii=False) + "\n"


# When the system prompt grows past this size, we drop it on disk and pass
# `--append-system-prompt-file` instead of `--append-system-prompt` to keep the
# argv short. macOS argv has a hard limit (ARG_MAX) and the `claude` CLI exits 1
# with empty stderr well before that ceiling when the argv balloons. The file
# path keeps argv tiny.
SYSTEM_PROMPT_INLINE_MAX = 8192


def build_claude_argv(model: str, system_arg: tuple[str, str | None]) -> list[str]:
    """Compose the `claude --print` argv. system_arg is a (mode, value) tuple:
    ("inline", text) -> --append-system-prompt <text>
    ("file",   path) -> --append-system-prompt-file <path>
    ("none",   None) -> no system prompt argument.

    The isolation flags below are important: without them the spawned model
    inherits the local Claude Code environment (CLAUDE.md, plugins, MCP servers)
    and may treat the tools we inject as "fake" because real MCP tools are
    present. We strip everything except the OAuth keychain auth so the model
    only sees the system prompt we explicitly pass. `--append-system-prompt` is
    used (not `--system-prompt`) so our content is appended to the CLI's minimal
    base prompt rather than replacing CLI-internal protocol primitives."""
    argv = [
        CLAUDE_BIN,
        "--print",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--no-session-persistence",
        "--model", model,
        "--tools", "",
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
        "--disable-slash-commands",
        "--exclude-dynamic-system-prompt-sections",
        "--setting-sources", "",
    ]
    mode, value = system_arg
    if mode == "inline" and value:
        argv.extend(["--append-system-prompt", value])
    elif mode == "file" and value:
        argv.extend(["--append-system-prompt-file", value])
    return argv


@contextlib.contextmanager
def system_prompt_arg(system: str):
    """Yield a (mode, value) pair plus cleanup. Small prompts ride argv,
    large prompts go to a tempfile that's removed afterwards."""
    if not system:
        yield ("none", None)
        return
    if len(system.encode("utf-8")) <= SYSTEM_PROMPT_INLINE_MAX:
        yield ("inline", system)
        return
    fd, path = tempfile.mkstemp(prefix="claude-proxy-sys-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(system)
        yield ("file", path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


TOOL_INSTRUCTIONS = """
## Tool calling protocol — STRICT

You have access to the tools listed below. To invoke a tool, output EXACTLY one
JSON object wrapped in {open}…{close} tags. The JSON must contain only "name"
and "input" keys. Do NOT add commentary inside or around the tag.

You may emit several tool calls in one response (one per tag). After all tool
calls, the system will execute them and return results inside
<proxy_tool_result for="…"> tags; you will then be invoked again to continue.

Required format (this is the ONLY accepted format):
{open}{{"name": "get_weather", "input": {{"city": "Paris"}}}}{close}

FORBIDDEN formats — never emit any of these, even if the conversation history
or your training data contained them:
  ✗ [tool_use name='get_weather' input={{"city":"Paris"}}]
  ✗ [tool_use name="get_weather" input={{...}}]
  ✗ <tool_use>…</tool_use>          (wrong tag)
  ✗ <function_call>…</function_call>
  ✗ ```json{{...}}```                (markdown fenced)
  ✗ Plain JSON without the tag wrapper
  ✗ Function-call syntax like get_weather({{...}})

If no tool is needed, just answer in natural language without any tag.

### Available tools
""".format(open=TOOL_TAG_OPEN, close=TOOL_TAG_CLOSE).strip()


def serialize_tools_to_system(tools: list[dict]) -> str:
    """Render the request's tool definitions as an instruction block appended
    to the system prompt."""
    if not tools:
        return ""
    parts = [TOOL_INSTRUCTIONS]
    for t in tools:
        name = t.get("name", "")
        desc = t.get("description", "") or ""
        schema = t.get("input_schema") or {}
        parts.append(f"\n#### {name}")
        if desc:
            parts.append(desc.strip())
        parts.append("Input schema:")
        parts.append("```json")
        parts.append(json.dumps(schema, ensure_ascii=False, indent=2))
        parts.append("```")
    return "\n".join(parts)


def _new_tool_use_id() -> str:
    return f"toolu_{uuid.uuid4().hex[:24]}"


def split_text_and_tool_calls(text: str) -> tuple[str, list[dict]]:
    """Extract tool-call tag blocks from a text body and return
    (clean_text_without_tags, [tool_use_blocks]). Blocks that don't parse as
    valid JSON are left in the text as-is."""
    tool_blocks: list[dict] = []
    leftover_pieces: list[str] = []
    last_end = 0
    for match in TOOL_BLOCK_RE.finditer(text):
        leftover_pieces.append(text[last_end:match.start()])
        raw = match.group(1).strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            leftover_pieces.append(match.group(0))
            last_end = match.end()
            continue
        if not isinstance(parsed, dict) or "name" not in parsed:
            leftover_pieces.append(match.group(0))
            last_end = match.end()
            continue
        tool_blocks.append({
            "type": "tool_use",
            "id": _new_tool_use_id(),
            "name": parsed["name"],
            "input": parsed.get("input", {}),
        })
        last_end = match.end()
    leftover_pieces.append(text[last_end:])
    clean_text = "".join(leftover_pieces).strip()
    return clean_text, tool_blocks


def restructure_message_with_tools(msg: dict) -> dict:
    """Rewrite an assistant message: scan its text blocks for tool-call tags,
    replace them with native Anthropic tool_use content blocks, and adjust
    stop_reason accordingly."""
    new_content: list[dict] = []
    has_tool_use = False
    for block in msg.get("content", []):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text", "")
            clean_text, tool_blocks = split_text_and_tool_calls(text)
            if clean_text:
                new_content.append({"type": "text", "text": clean_text})
            for tb in tool_blocks:
                new_content.append(tb)
                has_tool_use = True
        else:
            new_content.append(block)
    msg["content"] = new_content
    if has_tool_use:
        msg["stop_reason"] = "tool_use"
    return msg


async def _drain_stderr(proc: asyncio.subprocess.Process, sink: list[bytes]) -> None:
    """Drain stderr concurrently so the pipe never blocks. Captured for logging
    on non-zero exit. Keeps the last ~16 KiB only."""
    assert proc.stderr
    cap = 16 * 1024
    async for chunk in proc.stderr:
        sink.append(chunk)
        total = sum(len(c) for c in sink)
        if total > cap:
            joined = b"".join(sink)
            sink.clear()
            sink.append(joined[-cap:])


async def run_claude_collect(argv: list[str], stdin_bytes: bytes) -> dict:
    """Run claude in non-streaming mode: spawn, feed stdin, collect every
    stream-json event, return an Anthropic-format message built from the final
    assistant message + result event."""
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    stderr_buf: list[bytes] = []
    stderr_task = asyncio.create_task(_drain_stderr(proc, stderr_buf))

    proc.stdin.write(stdin_bytes)
    await proc.stdin.drain()
    proc.stdin.close()

    final_assistant: dict | None = None
    result_event: dict | None = None
    try:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                LOG.warning("non-json line on stdout: %s", line[:200])
                continue
            _ingest_event(event)
            t = event.get("type")
            if t == "assistant":
                final_assistant = event.get("message")
            elif t == "result":
                result_event = event
    finally:
        try:
            await asyncio.wait_for(proc.wait(), timeout=SUBPROCESS_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        await stderr_task

    duration = time.monotonic() - started
    stderr_text = b"".join(stderr_buf).decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        LOG.error("claude exited %d in %.2fs stderr=%s",
                  proc.returncode, duration, stderr_text[:1500] or "<empty>")
        raise web.HTTPBadGateway(
            text=json.dumps({
                "type": "error",
                "error": {"type": "upstream_error",
                          "message": f"claude exit {proc.returncode}: {stderr_text[:300] or '<empty>'}"}
            }),
            content_type="application/json",
        )

    if not final_assistant:
        LOG.error("no assistant message in %.2fs stderr=%s",
                  duration, stderr_text[:1500] or "<empty>")
        raise web.HTTPBadGateway(
            text=json.dumps({"type": "error",
                             "error": {"type": "upstream_error",
                                       "message": f"no assistant message produced; stderr={stderr_text[:200] or '<empty>'}"}}),
            content_type="application/json",
        )

    LOG.info("non-stream OK in %.2fs (%d output tokens)",
             duration,
             final_assistant.get("usage", {}).get("output_tokens", 0))
    return final_assistant


def _build_synthetic_sse(msg: dict) -> list[bytes]:
    """Translate a fully-collected Anthropic message (already restructured with
    native tool_use blocks) into the SSE event stream a client expects when
    `stream=true`. Used when tools are present and we had to buffer the whole
    response to extract tool_use blocks reliably."""
    events: list[bytes] = []

    def emit(name: str, payload: dict) -> None:
        events.append(
            f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
        )

    head = {
        "type": "message_start",
        "message": {
            "id": msg.get("id", f"msg_{uuid.uuid4().hex[:24]}"),
            "type": "message",
            "role": "assistant",
            "model": msg.get("model", ""),
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": msg.get("usage", {}),
        },
    }
    emit("message_start", head)

    for index, block in enumerate(msg.get("content", [])):
        btype = block.get("type")
        if btype == "text":
            emit("content_block_start", {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "text", "text": ""},
            })
            emit("content_block_delta", {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": block.get("text", "")},
            })
            emit("content_block_stop", {"type": "content_block_stop", "index": index})
        elif btype == "tool_use":
            emit("content_block_start", {
                "type": "content_block_start",
                "index": index,
                "content_block": {
                    "type": "tool_use",
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": {},
                },
            })
            emit("content_block_delta", {
                "type": "content_block_delta",
                "index": index,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(block.get("input", {}), ensure_ascii=False),
                },
            })
            emit("content_block_stop", {"type": "content_block_stop", "index": index})

    emit("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": msg.get("stop_reason", "end_turn"),
                  "stop_sequence": None},
        "usage": msg.get("usage", {}),
    })
    emit("message_stop", {"type": "message_stop"})
    return events


async def stream_claude_to_sse(argv: list[str], stdin_bytes: bytes,
                               response: web.StreamResponse) -> None:
    """Spawn claude and stream every native Anthropic SSE event back to the
    client as it lands on stdout. The CLI's `stream_event` payloads already
    match Anthropic SSE event shapes — we just re-emit them."""
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    stderr_buf: list[bytes] = []
    stderr_task = asyncio.create_task(_drain_stderr(proc, stderr_buf))

    proc.stdin.write(stdin_bytes)
    await proc.stdin.drain()
    proc.stdin.close()

    saw_message_stop = False
    try:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            _ingest_event(event)
            if event.get("type") != "stream_event":
                continue
            inner = event.get("event")
            if not inner or "type" not in inner:
                continue
            sse = f"event: {inner['type']}\ndata: {json.dumps(inner)}\n\n"
            await response.write(sse.encode("utf-8"))
            if inner["type"] == "message_stop":
                saw_message_stop = True
    finally:
        try:
            await asyncio.wait_for(proc.wait(), timeout=SUBPROCESS_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        await stderr_task

    duration = time.monotonic() - started
    stderr_text = b"".join(stderr_buf).decode("utf-8", errors="replace").strip()

    if not saw_message_stop and proc.returncode != 0:
        LOG.error("claude stream exited %d in %.2fs stderr=%s",
                  proc.returncode, duration, stderr_text[:1500] or "<empty>")
        err = {"type": "error",
               "error": {"type": "upstream_error",
                         "message": f"claude exit {proc.returncode}: {stderr_text[:300] or '<empty>'}"}}
        await response.write(f"event: error\ndata: {json.dumps(err)}\n\n".encode("utf-8"))
    elif not saw_message_stop:
        LOG.warning("stream completed without message_stop in %.2fs stderr=%s",
                    duration, stderr_text[:500] or "<empty>")
    else:
        LOG.info("stream OK in %.2fs", duration)


async def handle_messages(request: web.Request) -> web.StreamResponse:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response(
            {"type": "error", "error": {"type": "invalid_request_error",
                                        "message": "invalid JSON body"}},
            status=400,
        )

    model = body.get("model") or DEFAULT_MODEL
    system = normalize_system(body.get("system"))
    messages = body.get("messages") or []
    tools = body.get("tools") or []
    stream = bool(body.get("stream"))

    if not messages:
        return web.json_response(
            {"type": "error", "error": {"type": "invalid_request_error",
                                        "message": "messages array required"}},
            status=400,
        )

    if tools:
        tool_block = serialize_tools_to_system(tools)
        system = f"{system}\n\n{tool_block}".strip() if system else tool_block

    quota_warning = _quota.maybe_warning_block()
    if quota_warning:
        # Prepend the quota warning so the model sees it before everything else.
        system = f"{quota_warning}\n\n{system}" if system else quota_warning
        LOG.warning("quota near limit, injected warning: %s",
                    quota_warning.replace("\n", " | ")[:200])

    stdin_bytes = messages_to_stream_json(messages).encode("utf-8")

    LOG.info("→ %s stream=%s msgs=%d sys=%dB tools=%d quota_warn=%s",
             model, stream, len(messages), len(system), len(tools),
             "yes" if quota_warning else "no")

    with system_prompt_arg(system) as system_arg:
        argv = build_claude_argv(model, system_arg)

        # When tools are present we cannot stream the raw CLI output
        # transparently: tool-call tags would arrive as text deltas. Instead we
        # collect the whole response, restructure tool blocks, and replay
        # synthetic SSE events so the client sees a clean Anthropic stream.
        if stream and not tools:
            resp = web.StreamResponse(
                status=200,
                headers={"content-type": "text/event-stream",
                         "cache-control": "no-cache",
                         "connection": "keep-alive"},
            )
            await resp.prepare(request)
            await stream_claude_to_sse(argv, stdin_bytes, resp)
            await resp.write_eof()
            return resp

        msg = await run_claude_collect(argv, stdin_bytes)

    if tools:
        msg = restructure_message_with_tools(msg)
        tu_count = sum(1 for b in msg.get("content", []) if b.get("type") == "tool_use")
        if tu_count:
            LOG.info("  extracted %d tool_use block(s)", tu_count)

    if stream:
        resp = web.StreamResponse(
            status=200,
            headers={"content-type": "text/event-stream",
                     "cache-control": "no-cache",
                     "connection": "keep-alive"},
        )
        await resp.prepare(request)
        for evt in _build_synthetic_sse(msg):
            await resp.write(evt)
        await resp.write_eof()
        return resp

    return web.json_response(msg, status=200)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok",
                              "claude_bin": CLAUDE_BIN,
                              "default_model": DEFAULT_MODEL})


async def handle_quota(request: web.Request) -> web.Response:
    return web.json_response(_quota.snapshot())


_MODEL_PINS = ["opus", "sonnet", "haiku"]


async def handle_models(request: web.Request) -> web.Response:
    """Anthropic-style /v1/models stub. Returns the model aliases we forward to
    the claude CLI. Some clients probe this endpoint at startup."""
    return web.json_response({
        "data": [{"id": m, "type": "model", "display_name": m,
                  "created_at": "2026-01-01T00:00:00Z"} for m in _MODEL_PINS],
        "has_more": False,
        "first_id": _MODEL_PINS[0],
        "last_id": _MODEL_PINS[-1],
    })


async def handle_model_one(request: web.Request) -> web.Response:
    mid = request.match_info["model_id"]
    return web.json_response({"id": mid, "type": "model", "display_name": mid,
                              "created_at": "2026-01-01T00:00:00Z"})


async def handle_chat_completions(request: web.Request) -> web.Response:
    """OpenAI-format endpoint for clients that speak /v1/chat/completions. We
    translate to Anthropic /v1/messages internally so there's only one pipeline.
    Streaming is not supported here."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": {"message": "invalid JSON"}}, status=400)

    oai_messages = body.get("messages") or []
    system_parts: list[str] = []
    anthr_messages: list[dict] = []
    for m in oai_messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "system":
            if isinstance(content, str):
                system_parts.append(content)
        elif role in ("user", "assistant"):
            anthr_messages.append({"role": role,
                                   "content": [{"type": "text", "text": content if isinstance(content, str) else str(content)}]})
        else:
            continue
    if not anthr_messages:
        return web.json_response({"error": {"message": "no user messages"}}, status=400)

    model = body.get("model") or DEFAULT_MODEL
    system = "\n\n".join(p for p in system_parts if p)
    stdin_bytes = messages_to_stream_json(anthr_messages).encode("utf-8")

    LOG.info("→ (oai) %s msgs=%d sys=%dB", model, len(anthr_messages), len(system))
    with system_prompt_arg(system) as system_arg:
        argv = build_claude_argv(model, system_arg)
        msg = await run_claude_collect(argv, stdin_bytes)

    text = ""
    for block in msg.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            text += block.get("text", "")

    usage = msg.get("usage", {})
    return web.json_response({
        "id": msg.get("id", "chatcmpl-proxy"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    })


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("PROXY_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = web.Application()
    app.router.add_post("/v1/messages", handle_messages)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/quota", handle_quota)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/v1/models/{model_id}", handle_model_one)
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_post("/chat/completions", handle_chat_completions)

    LOG.info("claude-subscription-proxy listening on http://%s:%d (model=%s, claude=%s)",
             HOST, PORT, DEFAULT_MODEL, CLAUDE_BIN)
    web.run_app(app, host=HOST, port=PORT, print=None,
                handle_signals=True)


if __name__ == "__main__":
    main()
