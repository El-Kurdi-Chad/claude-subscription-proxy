"""claude-subscription-proxy.

A tiny local HTTP server that speaks the Anthropic Messages API, the OpenAI
APIs (Chat Completions, Responses, legacy Completions — which also covers
LM Studio clients) and the Ollama API but, instead of calling
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
  * Loopback only by default. Without PROXY_API_KEY there is NO authentication
    on these endpoints — set one before exposing the port to a network.
  * This is personal tooling. Use it within Anthropic's terms for your plan.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import tempfile
import time
import uuid
from typing import Any, Awaitable, Callable

from aiohttp import web
from aiohttp.web_log import AccessLogger

LOG = logging.getLogger("claude-subscription-proxy")

# Settings come from environment variables first, then from config.json next to
# this file (written by ui.py), then from the defaults below. The JSON keys are
# the same names as the environment variables.
CONFIG_FILE = os.environ.get(
    "PROXY_CONFIG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))


def _load_config_file(path: str) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: ignoring unreadable config {path}: {exc}", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: str(v) for k, v in data.items() if v not in (None, "")}


_FILE_CONFIG = _load_config_file(CONFIG_FILE)


def _setting(name: str, default: str) -> str:
    return os.environ.get(name) or _FILE_CONFIG.get(name) or default


PORT = int(_setting("PROXY_PORT", "3456"))
HOST = _setting("PROXY_HOST", "127.0.0.1")
# Additional ports serving the same API, for clients that hard-code the default
# port of the backend they were built for (LM Studio 1234, Ollama 11434).
EXTRA_PORTS = [int(p) for p in re.split(r"[,\s]+", _setting("PROXY_EXTRA_PORTS", "")) if p.isdigit()]
# Optional access control, needed as soon as the proxy is reachable from a
# network: when set, every API request must present one of these keys (comma-
# separated) as `Authorization: Bearer <key>` or `x-api-key: <key>`.
API_KEYS = [k for k in re.split(r"[,\s]+", _setting("PROXY_API_KEY", "")) if k]
AUTH_EXEMPT_LOCALHOST = _setting("PROXY_AUTH_EXEMPT_LOCALHOST", "false").lower() in (
    "1", "true", "yes", "on")
CLAUDE_BIN = _setting("CLAUDE_BIN", "claude")
DEFAULT_MODEL = _setting("PROXY_DEFAULT_MODEL", "sonnet")
SUBPROCESS_TIMEOUT = int(_setting("PROXY_SUBPROCESS_TIMEOUT", "1800"))
LOG_LEVEL = _setting("PROXY_LOG_LEVEL", "INFO")

# Where small bits of state (quota tracking) live.
STATE_DIR = os.path.expanduser(_setting("PROXY_STATE_DIR", "~/.claude-subscription-proxy"))
QUOTA_STATE_FILE = os.path.join(STATE_DIR, "quota.json")
# The CLI runs in an empty directory: its base prompt includes the working
# directory, and the model would otherwise think it's working on whatever
# project the proxy happens to be started from.
WORK_DIR = os.path.join(STATE_DIR, "workdir")

# Quota tracking — surfaces a warning when the subscription's rolling usage
# window is filling up, so the model can wrap up gracefully instead of getting
# cut off mid-task. We rely on Anthropic's authoritative `rate_limit_info`
# fields (status flips "allowed" -> "throttled"/"denied"; resetsAt tells us how
# long until the window resets). We DON'T threshold on `total_cost_usd`: on a
# flat-rate subscription that figure reflects the equivalent API price, not what
# you actually pay. It's recorded for visibility (`/quota`) only.
QUOTA_WARN_RESET_MIN = int(_setting("PROXY_QUOTA_WARN_RESET_MIN", "20"))
QUOTA_HEAVY_TURNS = int(_setting("PROXY_QUOTA_HEAVY_TURNS", "120"))
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
        resets_at = float(self._cache.get("resets_at") or 0)
        # A throttled/rejected status only holds until its window resets; the
        # next rate_limit_event would refresh it, but until then it's stale.
        window_over = 0 < resets_at <= time.time()
        if status not in (None, "allowed") and not window_over:
            reasons.append(f"status={status}")

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
# Parsing is lenient: smaller models (haiku) sometimes open with a common tag
# like <tool_call> but close with </proxy_tool_call>, or wrap the JSON in a
# ```json fence. Accept any mix of these aliases so the call isn't leaked to
# the client as raw text.
_TOOL_TAG_NAMES = r"(?:proxy_tool_call|tool_call|tool_use|function_call)"
TOOL_BLOCK_RE = re.compile(
    r"<" + _TOOL_TAG_NAMES + r">\s*(?:```(?:json)?\s*)?(\{.*?\})\s*(?:```\s*)?</"
    + _TOOL_TAG_NAMES + r">",
    re.DOTALL,
)
# Empty wrappers some models put around their calls (<function_calls>…).
_TOOL_WRAPPER_RE = re.compile(r"</?(?:function_calls|tool_calls)>")


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
    event = {
        "type": "user",
        "message": {"role": "user",
                    "content": [{"type": "text", "text": render_prompt(messages)}]},
    }
    return json.dumps(event, ensure_ascii=False) + "\n"


def render_prompt(messages: list[dict]) -> str:
    """The single prompt text the CLI receives for a whole conversation."""

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

    return "\n".join(pieces)


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


def describe_argv(argv: list[str]) -> str:
    """The CLI command line for display, with the (long) system prompt value
    replaced by a placeholder."""
    shown: list[str] = []
    for i, arg in enumerate(argv):
        if i > 0 and argv[i - 1] == "--append-system-prompt":
            arg = "<system prompt>"
        shown.append(arg if arg and not re.search(r"[\s\"{}]", arg) else json.dumps(arg))
    return " ".join(shown) + "  < prompt (stream-json on stdin)"


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
So END your response right after your last tool call: never write
<proxy_tool_result> tags yourself and never guess what a tool will return.

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
        tool_input = parsed.get("input", parsed.get("arguments", {}))
        if isinstance(tool_input, str):
            try:
                tool_input = json.loads(tool_input)
            except json.JSONDecodeError:
                tool_input = {"_raw": tool_input}
        tool_blocks.append({
            "type": "tool_use",
            "id": _new_tool_use_id(),
            "name": parsed["name"],
            "input": tool_input if isinstance(tool_input, dict) else {},
        })
        last_end = match.end()
    trailing = text[last_end:]
    if tool_blocks and trailing.strip():
        # The model should stop after its tool calls. Anything after the last
        # one is speculation — typically an invented <proxy_tool_result> and
        # an answer built on it — so it must not reach the client.
        LOG.info("  dropped %d chars after the last tool call", len(trailing.strip()))
    elif not tool_blocks:
        leftover_pieces.append(trailing)
    clean_text = _TOOL_WRAPPER_RE.sub("", "".join(leftover_pieces)).strip()
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


async def _spawn_claude(argv: list[str]) -> asyncio.subprocess.Process:
    os.makedirs(WORK_DIR, exist_ok=True)
    return await asyncio.create_subprocess_exec(
        *argv,
        cwd=WORK_DIR,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def run_claude_collect(argv: list[str], stdin_bytes: bytes) -> dict:
    """Run claude in non-streaming mode: spawn, feed stdin, collect every
    stream-json event, return an Anthropic-format message built from the final
    assistant message + result event."""
    started = time.monotonic()
    proc = await _spawn_claude(argv)
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
                               response: web.StreamResponse,
                               collect: list[str] | None = None) -> None:
    """Spawn claude and stream every native Anthropic SSE event back to the
    client as it lands on stdout. The CLI's `stream_event` payloads already
    match Anthropic SSE event shapes — we just re-emit them. Text deltas are
    also appended to `collect`, if given."""
    async def on_event(inner: dict) -> None:
        sse = f"event: {inner['type']}\ndata: {json.dumps(inner)}\n\n"
        await response.write(sse.encode("utf-8"))
        delta = inner.get("delta") or {}
        if collect is not None and delta.get("type") == "text_delta":
            collect.append(delta.get("text", ""))

    async def on_error(message: str) -> None:
        err = {"type": "error",
               "error": {"type": "upstream_error", "message": message}}
        await response.write(f"event: error\ndata: {json.dumps(err)}\n\n".encode("utf-8"))

    await stream_claude_events(argv, stdin_bytes, on_event, on_error)


async def stream_claude_events(argv: list[str], stdin_bytes: bytes,
                               on_event: Callable[[dict], Awaitable[None]],
                               on_error: Callable[[str], Awaitable[None]]) -> None:
    """Spawn claude and hand every inner Anthropic stream event to `on_event`
    as it lands on stdout. `on_error` is called if the CLI fails before the
    message completed. Output-format specifics live in the callbacks."""
    started = time.monotonic()
    proc = await _spawn_claude(argv)
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
            await on_event(inner)
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
        await on_error(f"claude exit {proc.returncode}: {stderr_text[:300] or '<empty>'}")
    elif not saw_message_stop:
        LOG.warning("stream completed without message_stop in %.2fs stderr=%s",
                    duration, stderr_text[:500] or "<empty>")
    else:
        LOG.info("stream OK in %.2fs", duration)


def build_system_prompt(system: str, tools: list[dict]) -> tuple[str, str | None]:
    """Append the tool protocol + definitions and prepend a quota warning if
    the subscription window is filling up. Returns (system, quota_warning)."""
    if tools:
        tool_block = serialize_tools_to_system(tools)
        system = f"{system}\n\n{tool_block}".strip() if system else tool_block

    quota_warning = _quota.maybe_warning_block()
    if quota_warning:
        # Prepend the quota warning so the model sees it before everything else.
        system = f"{quota_warning}\n\n{system}" if system else quota_warning
        LOG.warning("quota near limit, injected warning: %s",
                    quota_warning.replace("\n", " | ")[:200])
    return system, quota_warning


_MODEL_PINS = ["opus", "sonnet", "haiku"]
_MODEL_ALIAS_RE = re.compile(r"^(opus|sonnet|haiku|default|opusplan)(\[1m\])?$")


def normalize_model(name: Any) -> str:
    """Map whatever model name a client sends onto something `claude --model`
    accepts. Clients built for other backends send e.g. "gpt-4o",
    "anthropic/claude-sonnet-4.5" (OpenRouter style) or an LM Studio / Ollama
    model tag like "llama3:latest" — those fall back to DEFAULT_MODEL."""
    raw = str(name or "").strip()
    model = raw.split("/")[-1].lower()          # drop provider prefixes
    if model.endswith(":latest"):
        model = model[: -len(":latest")]
    if _MODEL_ALIAS_RE.match(model):
        return model
    if model.startswith("claude-"):
        # OpenRouter writes versions with dots (claude-sonnet-4.5); the CLI wants dashes.
        return model.replace(".", "-")
    for alias in _MODEL_PINS:                    # e.g. "sonnet-latest", "claude sonnet"
        if alias in model:
            return alias
    if raw:
        LOG.info("  unknown model %r → using default %r", raw, DEFAULT_MODEL)
    return DEFAULT_MODEL


def _json_format_instruction(fmt: Any) -> str:
    """OpenAI `response_format` / Ollama `format` -> a system instruction, since
    the CLI has no structured-output switch."""
    schema = None
    if fmt in ("json", "json_object") or (isinstance(fmt, dict) and fmt.get("type") == "json_object"):
        return "Respond with a single valid JSON object only — no prose, no markdown fences."
    if isinstance(fmt, dict):
        if fmt.get("type") == "json_schema":
            schema = (fmt.get("json_schema") or {}).get("schema") or fmt.get("schema")
        elif fmt.get("type") in (None, "object") and ("properties" in fmt or fmt.get("type") == "object"):
            schema = fmt  # Ollama passes the JSON schema directly
    if schema:
        return ("Respond with a single valid JSON value matching this JSON schema — no prose, "
                "no markdown fences:\n" + json.dumps(schema, ensure_ascii=False))
    return ""


class UpstreamError(Exception):
    """The claude CLI failed after the client's stream was already opened."""


def _error_message(exc: BaseException) -> str:
    if isinstance(exc, web.HTTPException) and exc.text:
        try:
            return json.loads(exc.text)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            return exc.text
    return str(exc) or exc.__class__.__name__


_JSON_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def _strip_json_fences(text: str) -> str:
    """Models often wrap JSON in ```json fences despite being told not to."""
    match = _JSON_FENCE_RE.match(text)
    return match.group(1).strip() if match else text.strip()


async def complete(request: web.Request, *, api: str, model: str, system: str,
                   messages: list[dict], tools: list[dict], json_mode: bool = False,
                   on_text: Callable[[str], Awaitable[None]] | None = None) -> dict:
    """Run one claude turn for any API format and return an Anthropic-shaped
    message whose tool calls are already native tool_use blocks.

    With `on_text`, the reply text is handed over as it arrives: live deltas
    when there are no tools, otherwise the cleaned text in one piece once the
    tool calls have been extracted (tags must never reach the client). Tool
    calls are never passed to `on_text`; read them from the returned message.
    `json_mode` also buffers, so markdown fences can be stripped from the JSON."""
    system, quota_warning = build_system_prompt(system, tools)
    stdin_bytes = messages_to_stream_json(messages).encode("utf-8")
    stream = on_text is not None
    _track(request, api=api, model=model, stream=stream, msgs=len(messages),
           tools=len(tools), preview=_last_user_preview(messages))
    LOG.info("→ (%s) %s stream=%s msgs=%d sys=%dB tools=%d quota_warn=%s",
             api, model, stream, len(messages), len(system), len(tools),
             "yes" if quota_warning else "no")
    await _detail_request_body(request)
    _detail(request, system_prompt=system, prompt=render_prompt(messages))

    with system_prompt_arg(system) as system_arg:
        argv = build_claude_argv(model, system_arg)
        _detail(request, cli_command=describe_argv(argv))

        if on_text is not None and not tools and not json_mode:
            msg: dict[str, Any] = {"id": None, "role": "assistant", "model": model,
                                   "content": [], "stop_reason": None, "usage": {}}
            parts: list[str] = []
            errors: list[str] = []

            async def on_event(inner: dict) -> None:
                t = inner.get("type")
                if t == "message_start":
                    start = inner.get("message") or {}
                    msg["id"] = start.get("id")
                    msg["usage"].update(start.get("usage") or {})
                elif t == "content_block_delta":
                    delta = inner.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        parts.append(delta["text"])
                        await on_text(delta["text"])
                elif t == "message_delta":
                    msg["stop_reason"] = (inner.get("delta") or {}).get("stop_reason")
                    msg["usage"].update(inner.get("usage") or {})

            async def on_error(message: str) -> None:
                errors.append(message)

            await stream_claude_events(argv, stdin_bytes, on_event, on_error)
            if errors:
                _track(request, error=errors[0][:300])
                raise UpstreamError(errors[0])
            msg["content"] = [{"type": "text", "text": "".join(parts)}]
            _detail(request, raw_output="".join(parts),
                    result=json.dumps(msg, indent=2, ensure_ascii=False))
            return msg

        msg = await run_claude_collect(argv, stdin_bytes)

    _detail(request, raw_output=message_text(msg))
    if tools:
        msg = restructure_message_with_tools(msg)
        names = [b.get("name") for b in msg.get("content", []) if b.get("type") == "tool_use"]
        if names:
            LOG.info("  extracted %d tool call(s): %s", len(names), ", ".join(map(str, names)))
            _track(request, tool_calls=names)
    if json_mode:
        for block in msg.get("content", []):
            if block.get("type") == "text":
                block["text"] = _strip_json_fences(block.get("text", ""))
    _detail(request, result=json.dumps(msg, indent=2, ensure_ascii=False))
    if on_text is not None:
        text = "".join(b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text")
        if text:
            await on_text(text)
    return msg


def message_text(msg: dict) -> str:
    return "".join(b.get("text", "") for b in msg.get("content", [])
                   if isinstance(b, dict) and b.get("type") == "text")


def message_tool_uses(msg: dict) -> list[dict]:
    return [b for b in msg.get("content", []) if isinstance(b, dict) and b.get("type") == "tool_use"]


def _sse_response(content_type: str = "text/event-stream") -> web.StreamResponse:
    return web.StreamResponse(status=200, headers={"content-type": content_type,
                                                   "cache-control": "no-cache",
                                                   "connection": "keep-alive"})


async def _read_json(request: web.Request) -> dict | None:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


async def handle_messages(request: web.Request) -> web.StreamResponse:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response(
            {"type": "error", "error": {"type": "invalid_request_error",
                                        "message": "invalid JSON body"}},
            status=400,
        )

    model = normalize_model(body.get("model"))
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

    system, quota_warning = build_system_prompt(system, tools)
    stdin_bytes = messages_to_stream_json(messages).encode("utf-8")
    _track(request, api="anthropic", model=model, stream=stream, msgs=len(messages),
           tools=len(tools), preview=_last_user_preview(messages))

    LOG.info("→ %s stream=%s msgs=%d sys=%dB tools=%d quota_warn=%s",
             model, stream, len(messages), len(system), len(tools),
             "yes" if quota_warning else "no")
    await _detail_request_body(request)
    _detail(request, system_prompt=system, prompt=render_prompt(messages))

    with system_prompt_arg(system) as system_arg:
        argv = build_claude_argv(model, system_arg)
        _detail(request, cli_command=describe_argv(argv))

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
            streamed: list[str] = []
            await stream_claude_to_sse(argv, stdin_bytes, resp, collect=streamed)
            await resp.write_eof()
            _detail(request, raw_output="".join(streamed), result="".join(streamed))
            return resp

        msg = await run_claude_collect(argv, stdin_bytes)

    _detail(request, raw_output=message_text(msg))
    if tools:
        msg = restructure_message_with_tools(msg)
        tu_names = [b.get("name") for b in msg.get("content", []) if b.get("type") == "tool_use"]
        if tu_names:
            LOG.info("  extracted %d tool_use block(s)", len(tu_names))
            _track(request, tool_calls=tu_names)
    _detail(request, result=json.dumps(msg, indent=2, ensure_ascii=False))

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
                              "default_model": DEFAULT_MODEL,
                              "auth_required": bool(API_KEYS)})


async def handle_quota(request: web.Request) -> web.Response:
    return web.json_response(_quota.snapshot())


_MODELS_CREATED = 1767225600  # 2026-01-01, a fixed stamp for the model stubs


def _model_entry(mid: str) -> dict:
    """One model object that satisfies OpenAI / LM Studio (`object`, `created`,
    `owned_by`) and Anthropic (`type`, `display_name`, `created_at`) clients."""
    return {"id": mid, "object": "model", "created": _MODELS_CREATED, "owned_by": "anthropic",
            "type": "model", "display_name": mid, "created_at": "2026-01-01T00:00:00Z"}


async def handle_models(request: web.Request) -> web.Response:
    """GET /v1/models in a shape both OpenAI-style and Anthropic-style clients
    accept. Returns the model aliases we forward to the claude CLI."""
    return web.json_response({
        "object": "list",
        "data": [_model_entry(m) for m in _MODEL_PINS],
        "has_more": False,
        "first_id": _MODEL_PINS[0],
        "last_id": _MODEL_PINS[-1],
    })


async def handle_model_one(request: web.Request) -> web.Response:
    return web.json_response(_model_entry(request.match_info["model_id"]))


async def handle_lmstudio_models(request: web.Request) -> web.Response:
    """LM Studio's native REST API (GET /api/v0/models)."""
    def entry(mid: str) -> dict:
        return {"id": mid, "object": "model", "type": "llm", "publisher": "anthropic",
                "arch": "claude", "compatibility_type": "api", "quantization": "none",
                "state": "loaded", "max_context_length": 200000}
    mid = request.match_info.get("model_id")
    if mid:
        return web.json_response(entry(mid))
    return web.json_response({"object": "list", "data": [entry(m) for m in _MODEL_PINS]})


async def handle_count_tokens(request: web.Request) -> web.Response:
    """POST /v1/messages/count_tokens. The CLI has no tokenizer endpoint, so
    this is an estimate (~4 characters per token) — good enough for the context
    budgeting clients use it for."""
    body = await _read_json(request) or {}
    text = normalize_system(body.get("system")) + json.dumps(body.get("tools") or [])
    text += "".join(_flatten_content(m.get("content")) for m in body.get("messages") or []
                    if isinstance(m, dict))
    return web.json_response({"input_tokens": max(1, len(text) // 4)})


def _append_message(out: list[dict], role: str, blocks: list[dict]) -> None:
    """Append blocks as a message, merging into the previous one when the role
    repeats (Anthropic requires alternating user/assistant turns). Always
    copies `blocks`, so callers' lists are never mutated later."""
    if not blocks:
        return
    if out and out[-1]["role"] == role:
        out[-1]["content"].extend(blocks)
    else:
        out.append({"role": role, "content": list(blocks)})


def _oai_content_to_blocks(content: Any) -> list[dict]:
    """OpenAI message content (string or list of parts) -> Anthropic blocks."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]
    blocks: list[dict] = []
    for part in content:
        if isinstance(part, str):
            blocks.append({"type": "text", "text": part})
        elif isinstance(part, dict):
            ptype = part.get("type")
            if ptype in ("text", "input_text"):
                blocks.append({"type": "text", "text": part.get("text", "")})
            elif ptype in ("image_url", "input_image"):
                blocks.append({"type": "image"})  # rendered as "[image omitted]"
            else:
                blocks.append({"type": "text", "text": json.dumps(part, ensure_ascii=False)})
    return blocks


def _parse_tool_arguments(arguments: Any) -> dict:
    """OpenAI tool_call arguments are a JSON string; Anthropic wants an object."""
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except (TypeError, json.JSONDecodeError):
        return {"_raw": arguments}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


def oai_messages_to_anthropic(oai_messages: list[dict]) -> tuple[str, list[dict]]:
    """Translate an OpenAI `messages` array into (system, anthropic_messages).
    Assistant `tool_calls` become tool_use blocks and `tool` role messages
    become tool_result blocks, so multi-turn tool loops keep their history."""
    system_parts: list[str] = []
    out: list[dict] = []

    def append(role: str, blocks: list[dict]) -> None:
        _append_message(out, role, blocks)

    for m in oai_messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role in ("system", "developer"):
            text = _flatten_content(_oai_content_to_blocks(content))
            if text:
                system_parts.append(text)
        elif role == "user":
            append("user", _oai_content_to_blocks(content))
        elif role == "assistant":
            blocks = _oai_content_to_blocks(content)
            calls = list(m.get("tool_calls") or [])
            if m.get("function_call"):  # legacy single-function format
                calls.append({"id": None, "function": m["function_call"]})
            for tc in calls:
                fn = tc.get("function") or {}
                blocks.append({"type": "tool_use",
                               "id": tc.get("id") or _new_tool_use_id(),
                               "name": fn.get("name"),
                               "input": _parse_tool_arguments(fn.get("arguments"))})
            append("assistant", blocks)
        elif role in ("tool", "function"):
            append("user", [{"type": "tool_result",
                             "tool_use_id": m.get("tool_call_id") or m.get("name"),
                             "content": _flatten_content(_oai_content_to_blocks(content))}])
    return "\n\n".join(system_parts), out


def oai_tools_to_anthropic(body: dict) -> list[dict]:
    """OpenAI `tools` (and legacy `functions`) -> Anthropic tool definitions."""
    fns: list[dict] = []
    for t in body.get("tools") or []:
        if isinstance(t, dict) and t.get("type", "function") == "function" and t.get("function"):
            fns.append(t["function"])
    fns.extend(f for f in body.get("functions") or [] if isinstance(f, dict))
    return [{"name": f.get("name", ""),
             "description": f.get("description", "") or "",
             "input_schema": f.get("parameters") or {"type": "object", "properties": {}}}
            for f in fns]


def _tool_choice_instruction(tool_choice: Any) -> str:
    if tool_choice == "required":
        return "You MUST call at least one tool in this response."
    if isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or {}).get("name") or tool_choice.get("name")
        if name:
            return f"You MUST call the tool `{name}` in this response."
    return ""


def anthropic_to_oai_choice(msg: dict) -> tuple[str, list[dict], str]:
    """Anthropic message -> (text, openai_tool_calls, finish_reason)."""
    text = ""
    tool_calls: list[dict] = []
    for block in msg.get("content", []):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text += block.get("text", "")
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id") or _new_tool_use_id(),
                "type": "function",
                "function": {"name": block.get("name"),
                             "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)},
            })
    return text, tool_calls, _oai_finish_reason(msg.get("stop_reason"), bool(tool_calls))


def _oai_finish_reason(stop_reason: str | None, has_tool_calls: bool) -> str:
    if has_tool_calls:
        return "tool_calls"
    if stop_reason == "max_tokens":
        return "length"
    return "stop"


def _oai_usage(usage: dict) -> dict:
    prompt = usage.get("input_tokens", 0) or 0
    completion = usage.get("output_tokens", 0) or 0
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


async def handle_chat_completions(request: web.Request) -> web.StreamResponse:
    """OpenAI-format endpoint for clients that speak /v1/chat/completions. We
    translate to the same claude pipeline as /v1/messages: tools are injected
    via the system prompt, tool-call tags are parsed back into `tool_calls`,
    and `stream=true` is answered with chat.completion.chunk SSE."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": {"message": "invalid JSON"}}, status=400)

    system, anthr_messages = oai_messages_to_anthropic(body.get("messages") or [])
    if not anthr_messages:
        return web.json_response({"error": {"message": "no user messages"}}, status=400)

    tool_choice = body.get("tool_choice") or body.get("function_call")
    tools = [] if tool_choice == "none" else oai_tools_to_anthropic(body)
    json_instruction = _json_format_instruction(body.get("response_format"))
    extra = [_tool_choice_instruction(tool_choice) if tools else "", json_instruction]
    system = "\n\n".join(p for p in [system, *extra] if p)

    model = normalize_model(body.get("model"))
    stream = bool(body.get("stream"))
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    run = dict(api="openai", model=model, system=system, messages=anthr_messages, tools=tools,
               json_mode=bool(json_instruction))

    if not stream:
        msg = await complete(request, **run)
        text, tool_calls, finish_reason = anthropic_to_oai_choice(msg)
        message: dict[str, Any] = {"role": "assistant",
                                   "content": text if (text or not tool_calls) else None}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return web.json_response({
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": _oai_usage(msg.get("usage", {})),
        })

    def chunk(delta: dict, finish_reason: str | None = None) -> bytes:
        payload = {"id": completion_id, "object": "chat.completion.chunk",
                   "created": created, "model": model,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    resp = _sse_response()
    await resp.prepare(request)
    await resp.write(chunk({"role": "assistant", "content": ""}))
    try:
        msg = await complete(request, **run,
                             on_text=lambda text: resp.write(chunk({"content": text})))
    except (UpstreamError, web.HTTPException) as exc:
        message_ = _error_message(exc)
        _track(request, error=message_[:300])
        await resp.write(f"data: {json.dumps({'error': {'message': message_}})}\n\n".encode("utf-8"))
    else:
        _, tool_calls, finish_reason = anthropic_to_oai_choice(msg)
        for i, tc in enumerate(tool_calls):
            await resp.write(chunk({"tool_calls": [{"index": i, **tc}]}))
        await resp.write(chunk({}, finish_reason))
        if include_usage:
            usage = {"id": completion_id, "object": "chat.completion.chunk", "created": created,
                     "model": model, "choices": [], "usage": _oai_usage(msg.get("usage", {}))}
            await resp.write(f"data: {json.dumps(usage)}\n\n".encode("utf-8"))
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


# --------------------------------------------------------------------------
# OpenAI legacy text completions (POST /v1/completions)
# --------------------------------------------------------------------------

async def handle_completions(request: web.Request) -> web.StreamResponse:
    """Old prompt-in / text-out OpenAI endpoint, still used by some LM Studio
    and autocomplete-style clients. The prompt becomes a single user turn."""
    body = await _read_json(request)
    if body is None:
        return web.json_response({"error": {"message": "invalid JSON"}}, status=400)
    prompt = body.get("prompt")
    if isinstance(prompt, list):
        prompt = "\n".join(str(p) for p in prompt)
    prompt = str(prompt or "")
    if not prompt.strip():
        return web.json_response({"error": {"message": "prompt required"}}, status=400)

    model = normalize_model(body.get("model"))
    completion_id = f"cmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    run = dict(api="completions", model=model, system="", tools=[],
               messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}])

    def payload(text: str, finish_reason: str | None) -> dict:
        return {"id": completion_id, "object": "text_completion", "created": created,
                "model": model, "choices": [{"text": text, "index": 0, "logprobs": None,
                                             "finish_reason": finish_reason}]}

    if not body.get("stream"):
        msg = await complete(request, **run)
        return web.json_response({**payload(message_text(msg),
                                            _oai_finish_reason(msg.get("stop_reason"), False)),
                                  "usage": _oai_usage(msg.get("usage", {}))})

    def sse(obj: dict) -> bytes:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")

    resp = _sse_response()
    await resp.prepare(request)
    try:
        msg = await complete(request, **run, on_text=lambda text: resp.write(sse(payload(text, None))))
    except (UpstreamError, web.HTTPException) as exc:
        await resp.write(sse({"error": {"message": _error_message(exc)}}))
    else:
        await resp.write(sse(payload("", _oai_finish_reason(msg.get("stop_reason"), False))))
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


# --------------------------------------------------------------------------
# OpenAI Responses API (POST /v1/responses) — Codex CLI, newer OpenAI SDKs
# --------------------------------------------------------------------------

# Conversation state for `previous_response_id`. In memory only: it is lost on
# restart, like OpenAI's own store for requests sent with store=false.
_RESPONSES_HISTORY: collections.OrderedDict[str, list[dict]] = collections.OrderedDict()
_RESPONSES_HISTORY_MAX = 200


def _responses_content_blocks(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: list[dict] = []
    for part in content if isinstance(content, list) else []:
        if isinstance(part, str):
            blocks.append({"type": "text", "text": part})
        elif isinstance(part, dict):
            ptype = part.get("type")
            if ptype in ("input_text", "output_text", "text", "summary_text"):
                blocks.append({"type": "text", "text": part.get("text", "")})
            elif ptype == "refusal":
                blocks.append({"type": "text", "text": part.get("refusal", "")})
            elif ptype in ("input_image", "image_url"):
                blocks.append({"type": "image"})
            elif ptype == "input_file":
                blocks.append({"type": "text",
                               "text": f"[file omitted: {part.get('filename') or 'attachment'}]"})
    return blocks


def responses_input_to_anthropic(inp: Any) -> tuple[str, list[dict]]:
    """Responses API `input` (string or item list) -> (system, anthropic_messages)."""
    if isinstance(inp, str):
        return "", [{"role": "user", "content": [{"type": "text", "text": inp}]}] if inp else []
    system_parts: list[str] = []
    out: list[dict] = []
    for item in inp if isinstance(inp, list) else []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type") or "message"
        if itype == "message":
            role = item.get("role", "user")
            blocks = _responses_content_blocks(item.get("content"))
            if role in ("system", "developer"):
                system_parts.append(_flatten_content(blocks))
            elif role in ("user", "assistant"):
                _append_message(out, role, blocks)
        elif itype == "function_call":
            _append_message(out, "assistant", [{
                "type": "tool_use",
                "id": item.get("call_id") or item.get("id") or _new_tool_use_id(),
                "name": item.get("name"),
                "input": _parse_tool_arguments(item.get("arguments")),
            }])
        elif itype == "function_call_output":
            output = item.get("output")
            if isinstance(output, list):
                output = _flatten_content(_responses_content_blocks(output))
            elif not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            _append_message(out, "user", [{"type": "tool_result",
                                           "tool_use_id": item.get("call_id"),
                                           "content": output}])
        # reasoning items and hosted-tool calls carry nothing the CLI can use.
    return "\n\n".join(p for p in system_parts if p), out


def responses_tools_to_anthropic(tools: Any) -> list[dict]:
    out: list[dict] = []
    for t in tools if isinstance(tools, list) else []:
        if not isinstance(t, dict) or t.get("type") != "function":
            continue  # hosted tools (web_search, file_search, ...) can't be proxied
        fn = t["function"] if isinstance(t.get("function"), dict) else t
        out.append({"name": fn.get("name", ""),
                    "description": fn.get("description", "") or "",
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    return out


def _responses_usage(usage: dict) -> dict:
    prompt = usage.get("input_tokens", 0) or 0
    completion = usage.get("output_tokens", 0) or 0
    return {"input_tokens": prompt,
            "input_tokens_details": {"cached_tokens": usage.get("cache_read_input_tokens", 0) or 0},
            "output_tokens": completion,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": prompt + completion}


def _responses_function_call(block: dict) -> dict:
    return {"type": "function_call", "id": f"fc_{uuid.uuid4().hex[:24]}",
            "call_id": block.get("id"), "name": block.get("name"),
            "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
            "status": "completed"}


def anthropic_to_responses_output(msg: dict) -> list[dict]:
    items: list[dict] = []
    text = message_text(msg)
    if text:
        items.append({"type": "message", "id": f"msg_{uuid.uuid4().hex[:24]}",
                      "status": "completed", "role": "assistant",
                      "content": [{"type": "output_text", "text": text, "annotations": []}]})
    items.extend(_responses_function_call(b) for b in message_tool_uses(msg))
    return items


class _ResponsesStream:
    """Emits Responses API SSE events (output items, text deltas, function
    calls) with a running sequence_number."""

    def __init__(self, resp: web.StreamResponse):
        self.resp = resp
        self.seq = 0
        self.output: list[dict] = []
        self._msg_item: dict | None = None
        self._msg_index = 0
        self._text: list[str] = []

    async def emit(self, event_type: str, **payload: Any) -> None:
        data = {"type": event_type, "sequence_number": self.seq, **payload}
        self.seq += 1
        await self.resp.write(
            f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))

    async def text(self, delta: str) -> None:
        if self._msg_item is None:
            self._msg_item = {"type": "message", "id": f"msg_{uuid.uuid4().hex[:24]}",
                              "status": "in_progress", "role": "assistant", "content": []}
            self._msg_index = len(self.output)
            self.output.append(self._msg_item)
            await self.emit("response.output_item.added", output_index=self._msg_index,
                            item=self._msg_item)
            await self.emit("response.content_part.added", item_id=self._msg_item["id"],
                            output_index=self._msg_index, content_index=0,
                            part={"type": "output_text", "text": "", "annotations": []})
        self._text.append(delta)
        await self.emit("response.output_text.delta", item_id=self._msg_item["id"],
                        output_index=self._msg_index, content_index=0, delta=delta, logprobs=[])

    async def finish_text(self) -> None:
        if self._msg_item is None:
            return
        item_id = self._msg_item["id"]
        part = {"type": "output_text", "text": "".join(self._text), "annotations": []}
        await self.emit("response.output_text.done", item_id=item_id, output_index=self._msg_index,
                        content_index=0, text=part["text"], logprobs=[])
        await self.emit("response.content_part.done", item_id=item_id,
                        output_index=self._msg_index, content_index=0, part=part)
        self._msg_item.update(status="completed", content=[part])
        await self.emit("response.output_item.done", output_index=self._msg_index,
                        item=self._msg_item)

    async def function_call(self, item: dict) -> None:
        index = len(self.output)
        self.output.append(item)
        await self.emit("response.output_item.added", output_index=index,
                        item={**item, "status": "in_progress", "arguments": ""})
        await self.emit("response.function_call_arguments.delta", item_id=item["id"],
                        output_index=index, delta=item["arguments"])
        await self.emit("response.function_call_arguments.done", item_id=item["id"],
                        output_index=index, arguments=item["arguments"])
        await self.emit("response.output_item.done", output_index=index, item=item)


async def handle_responses(request: web.Request) -> web.StreamResponse:
    body = await _read_json(request)
    if body is None:
        return web.json_response({"error": {"message": "invalid JSON"}}, status=400)

    system, messages = responses_input_to_anthropic(body.get("input"))
    prev = body.get("previous_response_id")
    if prev:
        history = _RESPONSES_HISTORY.get(prev)
        if history is None:
            return web.json_response({"error": {
                "message": f"Previous response with id '{prev}' not found (the proxy keeps "
                           "them in memory only; it may have been restarted).",
                "type": "invalid_request_error", "param": "previous_response_id",
                "code": "previous_response_not_found"}}, status=404)
        merged: list[dict] = []
        for m in history + messages:
            _append_message(merged, m["role"], m["content"])
        messages = merged
    if not messages:
        return web.json_response({"error": {"message": "input required"}}, status=400)

    tool_choice = body.get("tool_choice")
    tools = [] if tool_choice == "none" else responses_tools_to_anthropic(body.get("tools"))
    json_instruction = _json_format_instruction((body.get("text") or {}).get("format"))
    extra = [body.get("instructions") or "", system,
             _tool_choice_instruction(tool_choice) if tools else "", json_instruction]
    system = "\n\n".join(p for p in extra if p)

    model = normalize_model(body.get("model"))
    resp_id = f"resp_{uuid.uuid4().hex}"
    created = int(time.time())
    run = dict(api="responses", model=model, system=system, messages=messages, tools=tools,
               json_mode=bool(json_instruction))

    def response_obj(status: str, output: list[dict], usage: dict | None = None,
                     error: dict | None = None) -> dict:
        text = "".join(p.get("text", "") for item in output if item.get("type") == "message"
                       for p in item.get("content", []))
        return {"id": resp_id, "object": "response", "created_at": created, "status": status,
                "model": model, "output": output, "output_text": text,
                "error": error, "incomplete_details": None,
                "instructions": body.get("instructions"),
                "previous_response_id": prev,
                "tools": body.get("tools") or [], "tool_choice": tool_choice or "auto",
                "parallel_tool_calls": body.get("parallel_tool_calls", True),
                "metadata": body.get("metadata") or {}, "store": body.get("store", True),
                "text": body.get("text") or {"format": {"type": "text"}},
                "usage": _responses_usage(usage) if usage is not None else None}

    def remember(msg: dict) -> None:
        if body.get("store", True) is False:
            return
        _RESPONSES_HISTORY[resp_id] = messages + [
            {"role": "assistant", "content": list(msg.get("content", []))}]
        while len(_RESPONSES_HISTORY) > _RESPONSES_HISTORY_MAX:
            _RESPONSES_HISTORY.popitem(last=False)

    if not body.get("stream"):
        msg = await complete(request, **run)
        remember(msg)
        return web.json_response(response_obj("completed", anthropic_to_responses_output(msg),
                                              msg.get("usage", {})))

    resp = _sse_response()
    await resp.prepare(request)
    rs = _ResponsesStream(resp)
    await rs.emit("response.created", response=response_obj("in_progress", []))
    await rs.emit("response.in_progress", response=response_obj("in_progress", []))
    try:
        msg = await complete(request, **run, on_text=rs.text)
    except (UpstreamError, web.HTTPException) as exc:
        message = _error_message(exc)
        _track(request, error=message[:300])
        await rs.emit("response.failed", response=response_obj(
            "failed", rs.output, error={"code": "server_error", "message": message}))
    else:
        await rs.finish_text()
        for block in message_tool_uses(msg):
            await rs.function_call(_responses_function_call(block))
        remember(msg)
        await rs.emit("response.completed",
                      response=response_obj("completed", rs.output, msg.get("usage", {})))
    await resp.write_eof()
    return resp


# --------------------------------------------------------------------------
# Ollama API (/api/chat, /api/generate, /api/tags, ...)
# --------------------------------------------------------------------------

OLLAMA_VERSION = "0.12.0"  # what we claim to be; clients gate features on it


def _ollama_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000Z"


def _ollama_model_details() -> dict:
    return {"parent_model": "", "format": "api", "family": "claude", "families": ["claude"],
            "parameter_size": "", "quantization_level": ""}


async def handle_ollama_root(request: web.Request) -> web.Response:
    # Ollama clients probe GET / for exactly this string.
    return web.Response(text="Ollama is running")


async def handle_ollama_version(request: web.Request) -> web.Response:
    return web.json_response({"version": OLLAMA_VERSION})


async def handle_ollama_tags(request: web.Request) -> web.Response:
    models = []
    for m in _MODEL_PINS:
        name = f"{m}:latest"
        models.append({"name": name, "model": name, "modified_at": "2026-01-01T00:00:00Z",
                       "size": 0, "digest": hashlib.sha256(name.encode()).hexdigest(),
                       "details": _ollama_model_details()})
    return web.json_response({"models": models})


async def handle_ollama_ps(request: web.Request) -> web.Response:
    return web.json_response({"models": []})


async def handle_ollama_show(request: web.Request) -> web.Response:
    return web.json_response({
        "license": "", "modelfile": "", "parameters": "", "template": "{{ .Prompt }}",
        "details": _ollama_model_details(),
        "model_info": {"general.architecture": "claude", "claude.context_length": 200000},
        "capabilities": ["completion", "tools"],
        "modified_at": "2026-01-01T00:00:00Z",
    })


def ollama_messages_to_anthropic(msgs: list[dict]) -> tuple[str, list[dict]]:
    """Ollama chat messages -> (system, anthropic_messages). Ollama tool results
    carry no call id, so they are matched to the preceding tool calls in order."""
    system_parts: list[str] = []
    out: list[dict] = []
    pending_ids: list[str] = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content") or ""
        blocks = _oai_content_to_blocks(content)
        blocks.extend({"type": "image"} for _ in m.get("images") or [])
        if role == "system":
            system_parts.append(_flatten_content(blocks))
        elif role == "user":
            _append_message(out, "user", blocks)
        elif role == "assistant":
            pending_ids = []
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                tid = tc.get("id") or _new_tool_use_id()
                pending_ids.append(tid)
                blocks.append({"type": "tool_use", "id": tid, "name": fn.get("name"),
                               "input": _parse_tool_arguments(fn.get("arguments"))})
            _append_message(out, "assistant", blocks)
        elif role == "tool":
            tid = m.get("tool_call_id") or (pending_ids.pop(0) if pending_ids else
                                            m.get("tool_name") or "tool")
            _append_message(out, "user", [{"type": "tool_result", "tool_use_id": tid,
                                           "content": _flatten_content(blocks)}])
    return "\n\n".join(p for p in system_parts if p), out


async def _ollama_reply(request: web.Request, *, api: str, name: str, stream: bool,
                        system: str, messages: list[dict], tools: list[dict], fmt: Any,
                        piece: Callable[[str], dict],
                        final: Callable[[dict, bool], dict]) -> web.StreamResponse:
    """Shared tail of /api/chat and /api/generate. `piece(text)` builds a
    streamed chunk's body, `final(msg, streamed)` the fields of the last one."""
    started = time.monotonic_ns()
    model = normalize_model(name)
    json_instruction = _json_format_instruction(fmt)
    system = "\n\n".join(p for p in [system, json_instruction] if p)

    def done(msg: dict, streamed: bool) -> dict:
        usage = msg.get("usage", {})
        elapsed = time.monotonic_ns() - started
        return {"model": name, "created_at": _ollama_ts(), **final(msg, streamed), "done": True,
                "done_reason": "length" if msg.get("stop_reason") == "max_tokens" else "stop",
                "total_duration": elapsed, "load_duration": 0,
                "prompt_eval_count": usage.get("input_tokens", 0) or 0, "prompt_eval_duration": 0,
                "eval_count": usage.get("output_tokens", 0) or 0, "eval_duration": elapsed}

    run = dict(api=api, model=model, system=system, messages=messages, tools=tools,
               json_mode=bool(json_instruction))
    if not stream:
        msg = await complete(request, **run)
        return web.json_response(done(msg, False))

    resp = _sse_response("application/x-ndjson")
    await resp.prepare(request)

    async def line(obj: dict) -> None:
        await resp.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    try:
        msg = await complete(request, **run, on_text=lambda text: line(
            {"model": name, "created_at": _ollama_ts(), **piece(text), "done": False}))
    except (UpstreamError, web.HTTPException) as exc:
        await line({"error": _error_message(exc)})
    else:
        await line(done(msg, True))
    await resp.write_eof()
    return resp


def _ollama_tool_calls(msg: dict) -> list[dict]:
    return [{"function": {"name": b.get("name"), "arguments": b.get("input", {})}}
            for b in message_tool_uses(msg)]


async def handle_ollama_chat(request: web.Request) -> web.StreamResponse:
    body = await _read_json(request)
    if body is None:
        return web.json_response({"error": "invalid JSON"}, status=400)
    name = body.get("model") or DEFAULT_MODEL
    system, messages = ollama_messages_to_anthropic(body.get("messages") or [])
    if not messages:  # Ollama's "load the model" request
        return web.json_response({"model": name, "created_at": _ollama_ts(),
                                  "message": {"role": "assistant", "content": ""},
                                  "done_reason": "load", "done": True})

    def final(msg: dict, streamed: bool) -> dict:
        message: dict[str, Any] = {"role": "assistant",
                                   "content": "" if streamed else message_text(msg)}
        calls = _ollama_tool_calls(msg)
        if calls:
            message["tool_calls"] = calls
        return {"message": message}

    return await _ollama_reply(
        request, api="ollama", name=name, stream=body.get("stream", True) is not False,
        system=system, messages=messages, tools=oai_tools_to_anthropic(body),
        fmt=body.get("format"), piece=lambda text: {"message": {"role": "assistant", "content": text}}, final=final)


async def handle_ollama_generate(request: web.Request) -> web.StreamResponse:
    body = await _read_json(request)
    if body is None:
        return web.json_response({"error": "invalid JSON"}, status=400)
    name = body.get("model") or DEFAULT_MODEL
    prompt = str(body.get("prompt") or "")
    if not prompt.strip():  # Ollama's "load the model" request
        return web.json_response({"model": name, "created_at": _ollama_ts(), "response": "",
                                  "done_reason": "load", "done": True})
    blocks: list[dict] = [{"type": "text", "text": prompt}]
    blocks.extend({"type": "image"} for _ in body.get("images") or [])
    return await _ollama_reply(
        request, api="ollama", name=name, stream=body.get("stream", True) is not False,
        system=str(body.get("system") or ""), fmt=body.get("format"),
        messages=[{"role": "user", "content": blocks}], tools=[],
        piece=lambda text: {"response": text},
        final=lambda msg, streamed: {"response": "" if streamed else message_text(msg),
                                     "context": []})


async def handle_not_supported(request: web.Request) -> web.Response:
    return web.json_response({"error": {
        "message": f"{request.path} is not supported: the claude CLI has no embeddings model.",
        "type": "invalid_request_error"}}, status=501)


CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    # "*" alone does not cover Authorization (CORS spec), so name it explicitly.
    "Access-Control-Allow-Headers": "*, Authorization",
    "Access-Control-Max-Age": "86400",
}


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Browser-based clients (e.g. stio) send a CORS preflight OPTIONS before
    every POST/GET. Answer it for any path instead of letting the router 405."""
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=CORS_HEADERS)
    return await handler(request)


# Ring buffer of recent API calls, exposed at GET /requests for the UI.
RECENT_REQUESTS: collections.deque[dict] = collections.deque(maxlen=200)


def _track(request: web.Request, **fields: Any) -> None:
    """Attach details (model, tools, ...) to this request's history record."""
    record = request.get("track")
    if record is not None:
        record.update(fields)


# What went in and out of each recent request — the client's body, the system
# prompt and prompt text the CLI actually received, its raw output and the
# parsed result. Kept apart from RECENT_REQUESTS (which the UI polls) because
# these can be large; served per request at GET /requests/{id}.
REQUEST_DETAILS: collections.OrderedDict[str, dict] = collections.OrderedDict()
REQUEST_DETAILS_MAX = 50
_DETAIL_CLIP = 200_000


def _clip(text: str) -> str:
    if len(text) <= _DETAIL_CLIP:
        return text
    return text[:_DETAIL_CLIP] + f"\n… [{len(text) - _DETAIL_CLIP} more characters]"


def _detail(request: web.Request, **fields: Any) -> None:
    record = request.get("track")
    if record is None:
        return
    details = REQUEST_DETAILS.setdefault(record["id"], {})
    details.update({k: _clip(v) if isinstance(v, str) else v for k, v in fields.items()})
    REQUEST_DETAILS.move_to_end(record["id"])
    while len(REQUEST_DETAILS) > REQUEST_DETAILS_MAX:
        REQUEST_DETAILS.popitem(last=False)


async def _detail_request_body(request: web.Request) -> None:
    raw = await request.text()  # aiohttp caches the body, so this is free
    try:
        raw = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except ValueError:
        pass
    _detail(request, client_request=raw)


async def handle_request_detail(request: web.Request) -> web.Response:
    rid = request.match_info["request_id"]
    record = next((r for r in RECENT_REQUESTS if r.get("id") == rid), None)
    if record is None:
        return web.json_response({"error": {"message": f"request {rid} not found"}}, status=404)
    return web.json_response({**record, "details": REQUEST_DETAILS.get(rid, {})})


def _last_user_preview(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return " ".join(_flatten_content(m.get("content")).split())[:160]
    return ""


@web.middleware
async def request_log_middleware(request: web.Request, handler):
    if request.method != "POST":
        return await handler(request)
    record: dict[str, Any] = {"id": uuid.uuid4().hex[:12], "time": time.time(),
                              "path": request.path, "status": "running"}
    request["track"] = record
    RECENT_REQUESTS.append(record)
    started = time.monotonic()
    try:
        resp = await handler(request)
        record["status"] = resp.status
        return resp
    except web.HTTPException as exc:
        record["status"] = exc.status
        record["error"] = (exc.text or "")[:300]
        raise
    except Exception as exc:
        record["status"] = 500
        record["error"] = repr(exc)[:300]
        raise
    finally:
        record["duration_s"] = round(time.monotonic() - started, 2)


# Reachable without a key even when API_KEYS is set: liveness only, no data.
_AUTH_OPEN_PATHS = {"/health", "/"}
_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def _presented_key(request: web.Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return (request.headers.get("x-api-key") or request.headers.get("api-key") or "").strip()


def _key_valid(key: str) -> bool:
    return bool(key) and any(hmac.compare_digest(key.encode(), k.encode()) for k in API_KEYS)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Enforce API_KEYS when configured. Error bodies follow the format of the
    API being called, so clients show a readable "invalid API key" message."""
    if (not API_KEYS or request.path in _AUTH_OPEN_PATHS
            or (AUTH_EXEMPT_LOCALHOST and request.remote in _LOOPBACK)):
        return await handler(request)
    key = _presented_key(request)
    if _key_valid(key):
        return await handler(request)

    reason = "invalid API key" if key else "missing API key"
    LOG.warning("rejected %s %s from %s: %s", request.method, request.path, request.remote, reason)
    message = (f"{reason}: send it as 'Authorization: Bearer <key>' or 'x-api-key: <key>'")
    if request.path.startswith("/api/") and not request.path.startswith("/api/v0/"):
        body: dict[str, Any] = {"error": message}  # Ollama
    else:
        body = {"type": "error", "error": {"type": "authentication_error", "message": message,
                                           "code": "invalid_api_key"}}
    return web.json_response(body, status=401, headers={"WWW-Authenticate": "Bearer"})


class QuietAccessLogger(AccessLogger):
    """Access log without the UI's status polling, which would otherwise drown
    out the real API calls."""
    _QUIET_PATHS = {"/health", "/requests", "/quota"}

    def log(self, request, response, time):  # noqa: A002 — aiohttp's signature
        if request.method == "GET" and (request.path in self._QUIET_PATHS
                                        or request.path.startswith("/requests/")):
            return
        super().log(request, response, time)


async def handle_requests(request: web.Request) -> web.Response:
    return web.json_response(list(reversed(RECENT_REQUESTS)))


async def _add_cors_headers(request: web.Request, response: web.StreamResponse) -> None:
    # Runs right before headers are sent, so it also covers SSE StreamResponses
    # and error responses raised as HTTPException.
    response.headers.update(CORS_HEADERS)


def build_app() -> web.Application:
    # Auth runs after the request log so rejected calls show up in /requests.
    app = web.Application(middlewares=[cors_middleware, request_log_middleware, auth_middleware])
    app.on_response_prepare.append(_add_cors_headers)
    r = app.router

    # Proxy status (used by ui.py)
    r.add_get("/health", handle_health)
    r.add_get("/quota", handle_quota)
    r.add_get("/requests", handle_requests)
    r.add_get("/requests/{request_id}", handle_request_detail)

    # Anthropic Messages API
    r.add_post("/v1/messages", handle_messages)
    r.add_post("/v1/messages/count_tokens", handle_count_tokens)

    # OpenAI API — also what LM Studio speaks. Clients disagree on whether the
    # base URL includes /v1, and LM Studio's native API lives under /api/v0.
    for prefix in ("/v1", "", "/api/v0"):
        r.add_post(f"{prefix}/chat/completions", handle_chat_completions)
        r.add_post(f"{prefix}/completions", handle_completions)
        r.add_post(f"{prefix}/responses", handle_responses)
        r.add_post(f"{prefix}/embeddings", handle_not_supported)
    for prefix in ("/v1", ""):
        r.add_get(f"{prefix}/models", handle_models)
        r.add_get(f"{prefix}/models/{{model_id}}", handle_model_one)
    r.add_get("/api/v0/models", handle_lmstudio_models)
    r.add_get("/api/v0/models/{model_id}", handle_lmstudio_models)

    # Ollama API
    r.add_get("/", handle_ollama_root)
    r.add_get("/api/version", handle_ollama_version)
    r.add_get("/api/tags", handle_ollama_tags)
    r.add_get("/api/ps", handle_ollama_ps)
    r.add_post("/api/show", handle_ollama_show)
    r.add_post("/api/chat", handle_ollama_chat)
    r.add_post("/api/generate", handle_ollama_generate)
    r.add_post("/api/embed", handle_not_supported)
    r.add_post("/api/embeddings", handle_not_supported)
    return app


async def serve(app: web.Application) -> None:
    """Listen on PORT plus any EXTRA_PORTS. An extra port that is taken (e.g.
    the real LM Studio is running) is skipped with a warning; the main port
    failing is fatal."""
    runner = web.AppRunner(app, access_log_class=QuietAccessLogger)
    await runner.setup()
    if API_KEYS:
        LOG.info("API key required for all API requests%s",
                 " (except from this machine)" if AUTH_EXEMPT_LOCALHOST else "")
    elif HOST not in _LOOPBACK and HOST != "localhost":
        LOG.warning("listening on %s WITHOUT an API key — anyone who can reach this port "
                    "can use your Claude subscription. Set PROXY_API_KEY.", HOST)
    try:
        for port in [PORT, *[p for p in EXTRA_PORTS if p != PORT]]:
            try:
                await web.TCPSite(runner, HOST, port).start()
            except OSError as exc:
                if port == PORT:
                    raise
                LOG.warning("extra port %d not available, skipping: %s", port, exc)
                continue
            LOG.info("claude-subscription-proxy listening on http://%s:%d (model=%s, claude=%s)",
                     HOST, port, DEFAULT_MODEL, CLAUDE_BIN)
        await asyncio.Event().wait()  # serve until the process is stopped
    finally:
        await runner.cleanup()


def main() -> None:
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if _FILE_CONFIG:
        LOG.info("loaded settings from %s", CONFIG_FILE)
    try:
        asyncio.run(serve(build_app()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
