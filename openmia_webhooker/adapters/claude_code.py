from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Iterable, TextIO

from ..client import OpenMIAClient
from ..config import OpenMIAConfig
from ..redaction import redacted_copy, safe_summary, summarize_value
from ..runtime import to_runtime_payload
from ..state import FileStateStore
from ..utils import normalize_event_name, stable_short, utc_now


@dataclass
class WatchRound:
    session_id: str
    round_index: int
    round_id: str
    prompt: str | None
    started_at: str | None
    updated_at: float
    events: list[dict[str, Any]] = field(default_factory=list)


def parse_jsonl(lines: Iterable[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            events.append(
                {
                    "type": "raw",
                    "subtype": "unparsed_line",
                    "raw": summarize_value(text, capture_text=False),
                }
            )
            continue
        if isinstance(value, dict):
            events.append(value)
        else:
            events.append({"type": "raw", "value": value})
    return events


class ClaudeCodeCollector:
    """Adapter for Claude Code stream-json output."""

    def __init__(
        self,
        config: OpenMIAConfig,
        client: OpenMIAClient | None = None,
        state_store: FileStateStore | None = None,
    ) -> None:
        self.config = config
        self.client = client or OpenMIAClient.from_config(config)
        self.state_store = state_store or FileStateStore(config.state_dir)

    @classmethod
    def from_env(cls) -> "ClaudeCodeCollector":
        return cls(OpenMIAConfig.from_env())

    def log_event(self, record: dict[str, Any]) -> None:
        try:
            self.config.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.config.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"time": utc_now(), **record}, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def build_trace(
        self,
        events: list[dict[str, Any]],
        name: str = "Claude Code session",
        prompt: str | None = None,
        session_id: str | None = None,
        round_index: int | None = None,
        round_id: str | None = None,
        session_started_at: str | None = None,
        round_started_at: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        init_event = next((event for event in events if event.get("type") == "system" and event.get("subtype") == "init"), {})
        result_event = next((event for event in reversed(events) if event.get("type") == "result"), {})
        source_event = _first_event_with(events, ("model", "version", "cwd", "permissionMode", "claude_code_version"))
        session_id = str(
            session_id
            or init_event.get("session_id")
            or init_event.get("sessionId")
            or result_event.get("session_id")
            or result_event.get("sessionId")
            or f"claude_code_{stable_short(now, 16)}"
        )
        trace_id = f"claude_code_{stable_short(session_id, 24)}"
        status = self._trace_status(events, result_event)
        terminal_event = self._terminal_event(events, result_event, status)
        started_at = str(session_started_at or init_event.get("timestamp") or source_event.get("timestamp") or now)
        round_started_at = str(round_started_at or init_event.get("timestamp") or source_event.get("timestamp") or now)
        ended_at = str(terminal_event.get("timestamp") or now) if terminal_event else None
        prompt_summary = safe_summary(prompt, self.config.capture_text)
        result_text = self._result_text(events, result_event)
        output_summary = safe_summary(result_text, self.config.capture_text)
        round_index = round_index or 1
        round_id = round_id or f"round_{round_index}_{stable_short((prompt or result_text or session_id) + str(round_index), 10)}"
        chat_span_id = f"{trace_id}_chat_{round_id}"
        model = init_event.get("model") or source_event.get("model")
        version = init_event.get("claude_code_version") or init_event.get("version") or source_event.get("claude_code_version") or source_event.get("version")
        cwd = init_event.get("cwd") or source_event.get("cwd")
        permission_mode = init_event.get("permissionMode") or source_event.get("permissionMode")
        usage = result_event.get("usage") or source_event.get("usage")

        spans = [
            {
                "span_id": chat_span_id,
                "parent_span_id": None,
                "round_id": round_id,
                "name": f"Claude Code round {round_index} chat",
                "type": "chat",
                "status": status,
                "start_time": round_started_at,
                "end_time": ended_at,
                "input": {"prompt": prompt_summary} if prompt_summary else None,
                "output": {"completion": output_summary} if output_summary else None,
                "metadata": {
                    "source": "claude_code_stream_json",
                    "session_id": session_id,
                    "round_index": round_index,
                    "round_id": round_id,
                    "model": model,
                    "claude_code_version": version,
                    "cwd": cwd,
                    "permission_mode": permission_mode,
                    "usage": usage,
                },
                "model_name": model,
                "usage": usage,
            }
        ]
        spans.extend(self._event_spans(trace_id, events, round_started_at, chat_span_id, round_id, round_index))

        return {
            "trace_id": trace_id,
            "session_id": session_id,
            "name": name,
            "status": status,
            "start_time": started_at,
            "end_time": ended_at,
            "input": {"prompt": prompt_summary} if prompt_summary else None,
            "output": {"completion": output_summary} if output_summary else None,
            "metadata": {
                "source": "claude_code_stream_json",
                "mode": "stream_json",
                "event_count": len(events),
                "round_index": round_index,
                "round_id": round_id,
                "model": model,
                "claude_code_version": version,
                "cwd": cwd,
                "tools": init_event.get("tools"),
                "mcp_servers": init_event.get("mcp_servers"),
                "permission_mode": permission_mode,
                "usage": usage,
                "raw_events": [redacted_copy(event) for event in events[-100:]],
            },
            "spans": spans,
        }

    def handle(
        self,
        events: list[dict[str, Any]],
        name: str = "Claude Code session",
        prompt: str | None = None,
        dry_run: bool = False,
        stdout: TextIO | None = None,
    ) -> int:
        session_id = self._session_id(events)
        round_index, round_id, session_started_at = self._next_round(session_id, events, prompt, persist=not dry_run)
        legacy_trace = self.build_trace(
            events,
            name=name,
            prompt=prompt,
            session_id=session_id,
            round_index=round_index,
            round_id=round_id,
            session_started_at=session_started_at,
        )
        self.emit_legacy_trace(legacy_trace, dry_run=dry_run, stdout=stdout, event_name="stream_json")
        return 0

    def emit_legacy_trace(
        self,
        legacy_trace: dict[str, Any],
        dry_run: bool = False,
        stdout: TextIO | None = None,
        event_name: str = "stream_json",
    ) -> bool:
        out = stdout if stdout is not None else sys.stdout
        trace_id = str(legacy_trace["trace_id"])
        trace = to_runtime_payload(
            legacy_trace,
            source_type="claude_code",
            adapter_name="claude_code_stream_json",
            event_name=event_name,
        )

        if dry_run:
            print(json.dumps(redacted_copy(trace), ensure_ascii=False, indent=2, default=str), file=out)
            return True

        if not self.config.ingest_key:
            self.log_event({"level": "error", "message": "OPENMIA_CUSTOM_JSON_INGEST_KEY missing", "trace_id": trace_id})
            return False

        try:
            status, response = self.client.post_trace(trace)
            self.log_event(
                {
                    "level": "info",
                    "message": f"uploaded status {status}",
                    "status": status,
                    "trace_id": trace_id,
                    "response": response[:512],
                    "adapter": "claude_code",
                }
            )
            return True
        except urllib.error.HTTPError as exc:
            body = exc.read(2048).decode("utf-8", errors="replace")
            self.log_event({"level": "error", "message": "http_error", "status": exc.code, "trace_id": trace_id, "body": body})
        except Exception as exc:  # noqa: BLE001 - trace upload should not mask Claude Code output collection
            self.log_event({"level": "error", "message": "upload_failed", "trace_id": trace_id, "error": repr(exc)})
        return False

    def build_runtime_payload(
        self,
        events: list[dict[str, Any]],
        name: str = "Claude Code session",
        prompt: str | None = None,
        session_id: str | None = None,
        round_index: int | None = None,
        round_id: str | None = None,
        session_started_at: str | None = None,
        round_started_at: str | None = None,
        event_name: str = "stream_json",
    ) -> dict[str, Any]:
        legacy_trace = self.build_trace(
            events,
            name=name,
            prompt=prompt,
            session_id=session_id,
            round_index=round_index,
            round_id=round_id,
            session_started_at=session_started_at,
            round_started_at=round_started_at,
        )
        return to_runtime_payload(
            legacy_trace,
            source_type="claude_code",
            adapter_name="claude_code_stream_json",
            event_name=event_name,
        )

    def _session_id(self, events: list[dict[str, Any]]) -> str:
        init_event = next((event for event in events if event.get("type") == "system" and event.get("subtype") == "init"), {})
        result_event = next((event for event in reversed(events) if event.get("type") == "result"), {})
        return str(
            init_event.get("session_id")
            or init_event.get("sessionId")
            or result_event.get("session_id")
            or result_event.get("sessionId")
            or f"claude_code_{stable_short(utc_now(), 16)}"
        )

    def _state_key(self, session_id: str) -> str:
        return f"claude_code:{session_id}"

    def _next_round(self, session_id: str, events: list[dict[str, Any]], prompt: str | None, persist: bool) -> tuple[int, str, str]:
        now = utc_now()
        state_key = self._state_key(session_id)
        state = self.state_store.load(state_key) or {}
        try:
            round_index = int(state.get("round_index") or 0) + 1
        except (TypeError, ValueError):
            round_index = 1
        session_started_at = str(state.get("started_at") or now)
        round_id = f"round_{round_index}_{stable_short((prompt or json.dumps(events, sort_keys=True, default=str)) + str(round_index), 10)}"
        if persist:
            try:
                self.state_store.save(
                    state_key,
                    {
                        "thread_id": state_key,
                        "source": "claude_code_stream_json",
                        "session_id": session_id,
                        "trace_id": f"claude_code_{stable_short(session_id, 24)}",
                        "started_at": session_started_at,
                        "round_index": round_index,
                        "round_id": round_id,
                        "updated_at": now,
                    },
                )
            except OSError as exc:
                self.log_event({"level": "error", "message": "state_write_failed", "thread_id": state_key, "error": repr(exc)})
        return round_index, round_id, session_started_at

    def _trace_status(self, events: list[dict[str, Any]], result_event: dict[str, Any]) -> str:
        if result_event:
            return "error" if result_event.get("is_error") else "success"
        if any(event.get("type") == "error" or event.get("subtype") in {"api_retry", "error"} or event.get("is_error") for event in events):
            return "error"
        if any(event.get("type") == "system" and event.get("subtype") == "turn_duration" for event in events):
            return "success"
        return "unknown"

    def _terminal_event(self, events: list[dict[str, Any]], result_event: dict[str, Any], status: str) -> dict[str, Any]:
        if result_event:
            return result_event
        turn_duration = next(
            (event for event in reversed(events) if event.get("type") == "system" and event.get("subtype") == "turn_duration"),
            {},
        )
        if turn_duration:
            return turn_duration
        if status in {"error", "timeout"}:
            return next(
                (
                    event
                    for event in reversed(events)
                    if event.get("type") == "error"
                    or event.get("subtype") in {"api_retry", "error"}
                    or event.get("is_error")
                ),
                {},
            )
        return {}

    def _result_text(self, events: list[dict[str, Any]], result_event: dict[str, Any]) -> str | None:
        if isinstance(result_event.get("result"), str):
            return str(result_event["result"])
        texts: list[str] = []
        for event in events:
            if event.get("type") not in {"assistant", "message"}:
                continue
            for item in _content_items(event):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text)
        return "\n".join(texts) if texts else None

    def _event_spans(
        self,
        trace_id: str,
        events: list[dict[str, Any]],
        fallback_time: str,
        chat_span_id: str,
        round_id: str,
        round_index: int,
    ) -> list[dict[str, Any]]:
        spans: list[dict[str, Any]] = []
        for index, event in enumerate(events[:200], start=1):
            event_type = str(event.get("type") or "event")
            subtype = str(event.get("subtype") or event.get("status") or "")
            if event_type in {"result", "ai-title", "last-prompt", "queue-operation"} or (
                event_type == "system" and subtype in {"init", "turn_duration", "away_summary"}
            ):
                continue
            tool_items = _tool_content_items(event)
            if tool_items:
                spans.extend(self._tool_item_spans(trace_id, event, tool_items, index, fallback_time, chat_span_id, round_id, round_index))
                if not _text_content(event):
                    continue
            if _is_human_user_prompt_event(event):
                continue
            if event_type in {"assistant", "message"}:
                continue
            span_status = "error" if subtype in {"api_retry", "error"} or event.get("is_error") else "success"
            span_type = "error" if event_type == "error" or subtype == "error" or event.get("is_error") else "workflow"
            span_id = f"{trace_id}_event_{index}_{stable_short(json.dumps(event, sort_keys=True, default=str), 8)}"
            spans.append(
                {
                    "span_id": span_id,
                    "parent_span_id": chat_span_id,
                    "round_id": round_id,
                    "name": self._event_name(event_type, subtype, index),
                    "type": span_type,
                    "status": span_status,
                    "start_time": str(event.get("timestamp") or fallback_time),
                    "end_time": str(event.get("timestamp") or fallback_time),
                    "input": _event_input(event, self.config.capture_text),
                    "output": _event_output(event, self.config.capture_text),
                    "metadata": {
                        "source": "claude_code_stream_json",
                        "event_type": event_type,
                        "event_subtype": subtype or None,
                        "round_index": round_index,
                        "round_id": round_id,
                        "uuid": event.get("uuid"),
                        "raw_event": redacted_copy(event),
                    },
                }
            )
        return spans

    def _tool_item_spans(
        self,
        trace_id: str,
        event: dict[str, Any],
        items: list[dict[str, Any]],
        event_index: int,
        fallback_time: str,
        chat_span_id: str,
        round_id: str,
        round_index: int,
    ) -> list[dict[str, Any]]:
        spans: list[dict[str, Any]] = []
        for item_index, item in enumerate(items, start=1):
            item_type = str(item.get("type") or "tool")
            tool_name = str(item.get("name") or item.get("tool_name") or item.get("tool_use_id") or "tool")
            is_result = item_type == "tool_result"
            status = "error" if item.get("is_error") else "success"
            span_id = f"{trace_id}_tool_{event_index}_{item_index}_{stable_short(json.dumps(item, sort_keys=True, default=str), 8)}"
            spans.append(
                {
                    "span_id": span_id,
                    "parent_span_id": chat_span_id,
                    "round_id": round_id,
                    "name": f"Claude Code {'tool result' if is_result else 'tool use'}: {tool_name}",
                    "type": "tool",
                    "status": status,
                    "start_time": str(event.get("timestamp") or fallback_time),
                    "end_time": str(event.get("timestamp") or fallback_time),
                    "input": {"tool": summarize_value(item.get("input"), self.config.capture_text)} if not is_result and item.get("input") not in (None, "") else None,
                    "output": {"tool": summarize_value(item.get("content"), self.config.capture_text)} if is_result and item.get("content") not in (None, "") else None,
                    "metadata": {
                        "source": "claude_code_stream_json",
                        "event_type": str(event.get("type") or "event"),
                        "item_type": item_type,
                        "round_index": round_index,
                        "round_id": round_id,
                        "tool_name": item.get("name") or item.get("tool_name"),
                        "tool_use_id": item.get("id") or item.get("tool_use_id"),
                        "raw_event": redacted_copy(event),
                    },
                }
            )
        return spans

    def _event_name(self, event_type: str, subtype: str, index: int) -> str:
        suffix = normalize_event_name(subtype).replace("_", " ") if subtype else str(index)
        return f"Claude Code {event_type} {suffix}".strip()


def _content_items(event: dict[str, Any]) -> list[dict[str, Any]]:
    content = event.get("content")
    if isinstance(content, list):
        return [item for item in content if isinstance(item, dict)]
    message = event.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), list):
        return [item for item in message["content"] if isinstance(item, dict)]
    return []


def _tool_content_items(event: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in _content_items(event) if str(item.get("type") or "").startswith("tool_")]


def _text_content(event: dict[str, Any]) -> str | None:
    texts = [item.get("text") for item in _content_items(event) if isinstance(item.get("text"), str) and item.get("text").strip()]
    return "\n".join(texts) if texts else None


def _first_event_with(events: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[str, Any]:
    for event in events:
        if any(event.get(key) not in (None, "") for key in keys):
            return event
        message = event.get("message")
        if isinstance(message, dict) and any(message.get(key) not in (None, "") for key in keys):
            merged = dict(event)
            for key in keys:
                if key in message and key not in merged:
                    merged[key] = message[key]
            return merged
    return {}


def _event_input(event: dict[str, Any], capture_text: bool) -> dict[str, Any] | None:
    prompt = event.get("prompt") or event.get("input")
    if prompt not in (None, ""):
        return {"event": summarize_value(prompt, capture_text)}
    return None


def _event_output(event: dict[str, Any], capture_text: bool) -> dict[str, Any] | None:
    result = event.get("result") or event.get("output") or event.get("error")
    texts = [item.get("text") for item in _content_items(event) if isinstance(item.get("text"), str)]
    if texts:
        result = "\n".join(texts)
    if result not in (None, ""):
        return {"event": summarize_value(result, capture_text)}
    return None


def run_claude_code(
    claude_args: list[str],
    *,
    dry_run: bool = False,
    name: str = "Claude Code session",
    prompt: str | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    popen_factory: Any = subprocess.Popen,
    collector: ClaudeCodeCollector | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    args = list(claude_args)
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        print("claude-code-run requires Claude Code arguments after --", file=err)
        return 2
    if not _has_print_option(args):
        print("claude-code-run only supports non-interactive Claude Code runs; include -p or --print", file=err)
        return 2
    if _has_option(args, "--output-format"):
        print("claude-code-run manages --output-format; remove the conflicting Claude Code argument", file=err)
        return 2

    command = ["claude", *args, "--verbose", "--output-format", "stream-json", "--include-hook-events"]
    try:
        proc = popen_factory(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        print("claude executable not found", file=err)
        return 127
    stdout_text, stderr_text = proc.communicate()
    if stderr_text:
        print(stderr_text, end="" if stderr_text.endswith("\n") else "\n", file=err)

    events = parse_jsonl(stdout_text.splitlines())
    if not events:
        return int(getattr(proc, "returncode", 0) or 1)
    active_collector = collector or ClaudeCodeCollector.from_env()
    active_collector.handle(events, name=name, prompt=prompt or _prompt_from_claude_args(args), dry_run=dry_run, stdout=out)
    if not dry_run:
        result_event = next((event for event in reversed(events) if event.get("type") == "result"), {})
        result_text = active_collector._result_text(events, result_event)
        if result_text:
            print(result_text, file=out)
    return int(getattr(proc, "returncode", 0) or 0)


def watch_project_logs(
    *,
    projects_dir: pathlib.Path,
    dry_run: bool = False,
    once: bool = True,
    follow: bool = False,
    idle_flush_sec: float = 10.0,
    poll_interval_sec: float = 2.0,
    stdout: TextIO | None = None,
    collector: ClaudeCodeCollector | None = None,
    max_iterations: int | None = None,
) -> int:
    active_collector = collector or ClaudeCodeCollector.from_env()
    iterations = 0
    while True:
        rounds = _project_rounds(projects_dir)
        if follow:
            cutoff = time.time() - idle_flush_sec
            rounds = [round_item for round_item in rounds if round_item.updated_at <= cutoff]
        _emit_watch_rounds(active_collector, rounds, dry_run=dry_run, stdout=stdout)
        iterations += 1
        if once or (max_iterations is not None and iterations >= max_iterations):
            return 0
        time.sleep(poll_interval_sec)


def _emit_watch_rounds(
    collector: ClaudeCodeCollector,
    rounds: list[WatchRound],
    *,
    dry_run: bool,
    stdout: TextIO | None,
) -> None:
    for round_item in rounds:
        state_key = f"claude_code_watch:{round_item.session_id}"
        state = collector.state_store.load(state_key) or {}
        uploaded = state.get("uploaded_rounds")
        if not isinstance(uploaded, list):
            uploaded = []
        if round_item.round_id in uploaded:
            continue
        legacy_trace = collector.build_trace(
            round_item.events,
            name="Claude Code session",
            prompt=round_item.prompt,
            session_id=round_item.session_id,
            round_index=round_item.round_index,
            round_id=round_item.round_id,
            session_started_at=str(state.get("started_at") or round_item.started_at or utc_now()),
            round_started_at=round_item.started_at,
        )
        success = collector.emit_legacy_trace(legacy_trace, dry_run=dry_run, stdout=stdout, event_name="project_jsonl")
        if success and not dry_run:
            uploaded.append(round_item.round_id)
            try:
                collector.state_store.save(
                    state_key,
                    {
                        "thread_id": state_key,
                        "source": "claude_code_project_jsonl",
                        "session_id": round_item.session_id,
                        "trace_id": legacy_trace["trace_id"],
                        "started_at": state.get("started_at") or round_item.started_at or utc_now(),
                        "uploaded_rounds": uploaded[-500:],
                        "updated_at": utc_now(),
                    },
                )
            except OSError as exc:
                collector.log_event({"level": "error", "message": "state_write_failed", "thread_id": state_key, "error": repr(exc)})


def _project_rounds(projects_dir: pathlib.Path) -> list[WatchRound]:
    events = _read_project_events(projects_dir)
    current: dict[str, WatchRound] = {}
    rounds: list[WatchRound] = []
    round_counts: dict[str, int] = {}
    for event in events:
        session_id = _event_session_id(event)
        if not session_id:
            continue
        if _is_human_user_prompt_event(event):
            previous = current.get(session_id)
            if previous:
                rounds.append(previous)
            round_index = round_counts.get(session_id, 0) + 1
            round_counts[session_id] = round_index
            prompt = _user_prompt(event)
            round_id = f"round_{round_index}_{stable_short(session_id + str(event.get('uuid') or event.get('timestamp') or prompt or round_index), 10)}"
            current[session_id] = WatchRound(
                session_id=session_id,
                round_index=round_index,
                round_id=round_id,
                prompt=prompt,
                started_at=str(event.get("timestamp") or ""),
                updated_at=_event_timestamp(event),
                events=[event],
            )
            continue
        if session_id in current:
            current[session_id].events.append(event)
            current[session_id].updated_at = max(current[session_id].updated_at, _event_timestamp(event))
    rounds.extend(current.values())
    return rounds


def _read_project_events(projects_dir: pathlib.Path) -> list[dict[str, Any]]:
    rows: list[tuple[float, int, dict[str, Any]]] = []
    sequence = 0
    for path in sorted(projects_dir.rglob("*.jsonl")):
        try:
            file_mtime = path.stat().st_mtime
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            sequence += 1
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_time = _event_timestamp(event, fallback=file_mtime)
            event["_openmia_source_file"] = str(path)
            event["_openmia_event_time"] = event_time
            rows.append((event_time, sequence, event))
    return [event for _, _, event in sorted(rows, key=lambda item: (item[0], item[1]))]


def _event_session_id(event: dict[str, Any]) -> str | None:
    value = event.get("session_id") or event.get("sessionId")
    return str(value) if value not in (None, "") else None


def _event_timestamp(event: dict[str, Any], fallback: float | None = None) -> float:
    cached = event.get("_openmia_event_time")
    if isinstance(cached, (int, float)):
        return float(cached)
    value = event.get("timestamp")
    if isinstance(value, str):
        try:
            from datetime import datetime

            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback if fallback is not None else 0.0


def _user_prompt(event: dict[str, Any]) -> str | None:
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [item.get("text") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)]
            return "\n".join(texts) if texts else None
    content = event.get("content")
    return str(content) if isinstance(content, str) else None


def _is_human_user_prompt_event(event: dict[str, Any]) -> bool:
    if str(event.get("type") or "") != "user":
        return False
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else event.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        has_text = False
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type.startswith("tool_"):
                return False
            if item_type == "text" and isinstance(item.get("text"), str) and item["text"].strip():
                has_text = True
        return has_text
    return False


def _has_option(args: list[str], option: str) -> bool:
    return option in args or any(arg.startswith(f"{option}=") for arg in args)


def _has_print_option(args: list[str]) -> bool:
    return "-p" in args or "--print" in args or any(arg.startswith("--print=") for arg in args)


def _prompt_from_claude_args(args: list[str]) -> str | None:
    for index, arg in enumerate(args):
        if arg.startswith("--print="):
            return arg.split("=", 1)[1] or None
        if arg in {"-p", "--print"} and index + 1 < len(args):
            candidate = args[index + 1]
            if not candidate.startswith("-"):
                return candidate
    candidates = [arg for arg in args if arg and not arg.startswith("-")]
    return candidates[-1] if candidates else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openmia-webhooker claude-code")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--name", default="Claude Code session")
    parser.add_argument("--prompt", default=None)
    args = parser.parse_args(argv)

    events = parse_jsonl(sys.stdin)
    collector = ClaudeCodeCollector.from_env()
    return collector.handle(events, name=args.name, prompt=args.prompt, dry_run=args.dry_run)


def run_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openmia-webhooker claude-code-run")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--name", default="Claude Code session")
    parser.add_argument("--prompt", default=None)
    parser.add_argument("claude_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    return run_claude_code(
        args.claude_args,
        dry_run=args.dry_run,
        name=args.name,
        prompt=args.prompt,
    )


def watch_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openmia-webhooker claude-code-watch")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--follow", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--projects-dir", type=pathlib.Path, default=pathlib.Path.home() / ".claude" / "projects")
    parser.add_argument("--idle-flush-sec", type=float, default=10.0)
    parser.add_argument("--poll-interval-sec", type=float, default=2.0)
    args = parser.parse_args(argv)

    return watch_project_logs(
        projects_dir=args.projects_dir,
        dry_run=args.dry_run,
        once=args.once,
        follow=args.follow,
        idle_flush_sec=args.idle_flush_sec,
        poll_interval_sec=args.poll_interval_sec,
    )


if __name__ == "__main__":
    raise SystemExit(main())
