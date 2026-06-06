from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
from typing import Any, BinaryIO, TextIO

from ..client import OpenMIAClient
from ..config import OpenMIAConfig
from ..redaction import (
    OUTPUT_KEY_RE,
    PROMPT_KEY_RE,
    collect_candidate_strings,
    redacted_copy,
    safe_summary,
    summarize_value,
)
from ..runtime import to_runtime_payload
from ..state import FileStateStore, newest_session_output_summary
from ..traces import build_reasoning_spans, build_self_test_trace, make_chat_span, make_root_span
from ..utils import find_first_key, normalize_event_name, safe_id_part, stable_short, utc_now, sha256_text

ID_KEYS = (
    "conversation_id",
    "conversationId",
    "codex.conversation_id",
    "session_id",
    "sessionId",
    "thread_id",
    "threadId",
    "rollout_id",
    "rolloutId",
)
EVENT_ID_KEYS = ("event_id", "eventId", "hook_run_id", "hookRunId", "turn_id", "turnId", "id")
TOOL_NAME_KEYS = (
    "tool_name",
    "toolName",
    "tool",
    "command",
    "function_name",
    "functionName",
    "recipient_name",
    "recipientName",
)
TOOL_CALL_ID_KEYS = (
    "tool_call_id",
    "toolCallId",
    "call_id",
    "callId",
    "invocation_id",
    "invocationId",
    "tool_use_id",
    "toolUseId",
)
TOOL_INPUT_KEYS = (
    "arguments",
    "args",
    "params",
    "parameters",
    "input",
    "query",
    "q",
    "url",
    "urls",
    "command",
    "cmd",
)
TOOL_OUTPUT_KEYS = (
    "result",
    "results",
    "response",
    "responses",
    "output",
    "stdout",
    "stderr",
    "error",
    "errors",
    "content",
    "text",
)


def parse_stdin(stream: BinaryIO | None = None) -> tuple[Any, str]:
    source = stream if stream is not None else sys.stdin.buffer
    raw = source.read()
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return {}, ""
    try:
        return json.loads(text), text
    except json.JSONDecodeError:
        return {"_raw_stdin_sha256": sha256_text(text), "_raw_stdin_length": len(text)}, text


def find_tool_name(payload: Any, event_name: str) -> str:
    found = find_first_key(payload, TOOL_NAME_KEYS)
    if isinstance(found, dict):
        nested = find_first_key(found, ("name", "tool_name", "toolName", "command"))
        if nested not in (None, ""):
            return str(nested)
    if found not in (None, ""):
        return str(found)
    normalized = normalize_event_name(event_name)
    if "web" in normalized or "search" in normalized:
        return "web_search"
    return "unknown_tool"


def find_tool_payload_value(payload: Any, keys: tuple[str, ...]) -> Any | None:
    found = find_first_key(payload, keys)
    return found if found not in (None, "") else None


def is_tool_event(event_name: str, payload: Any) -> bool:
    normalized = normalize_event_name(event_name)
    if "tool" in normalized or normalized in {"exec_command", "web_search", "web_run"}:
        return True
    return find_first_key(payload, TOOL_NAME_KEYS) not in (None, "")


class CodexCollector:
    """Adapter that turns Codex hook payloads into OpenMIA traces."""

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
    def from_env(cls) -> "CodexCollector":
        return cls(OpenMIAConfig.from_env())

    def log_event(self, record: dict[str, Any]) -> None:
        try:
            self.config.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.config.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"time": utc_now(), **record}, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def _save_state(self, thread_id: str, state: dict[str, Any]) -> None:
        try:
            self.state_store.save(thread_id, state)
        except OSError as exc:
            self.log_event({"level": "error", "message": "state_write_failed", "thread_id": thread_id, "error": repr(exc)})

    def read_model_reasoning_effort(self) -> str:
        for key in ("CODEX_MODEL_REASONING_EFFORT", "OPENAI_REASONING_EFFORT", "MODEL_REASONING_EFFORT"):
            if os.environ.get(key):
                return str(os.environ[key])
        try:
            text = self.config.codex_config_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "unknown"
        match = re.search(r"(?m)^\s*model_reasoning_effort\s*=\s*[\"']?([^\"'\n]+)", text)
        return match.group(1).strip() if match else "unknown"

    def choose_thread_id(self, payload: Any, raw_text: str) -> str:
        found = find_first_key(payload, ID_KEYS)
        if found not in (None, ""):
            return str(found)
        cwd = find_first_key(payload, ("cwd", "current_working_directory", "workingDirectory"))
        base = str(cwd or os.getcwd())
        return f"codex_local_{stable_short(base, 20)}"

    def choose_event_id(self, payload: Any, raw_text: str) -> str:
        found = find_first_key(payload, EVENT_ID_KEYS)
        if found not in (None, ""):
            return str(found)
        return f"event_{int(time.time() * 1000)}_{stable_short(raw_text, 10)}"

    def _state_for_event(self, thread_id: str, has_explicit_thread_id: bool) -> tuple[str, dict[str, Any] | None]:
        state = self.state_store.load(thread_id)
        if state or has_explicit_thread_id:
            return thread_id, state
        newest = self.state_store.newest()
        if newest:
            return newest
        return thread_id, None

    def build_user_prompt_trace(self, payload: Any, raw_text: str) -> tuple[str, str, dict[str, Any]]:
        now = utc_now()
        thread_id = self.choose_thread_id(payload, raw_text)
        event_id = self.choose_event_id(payload, raw_text)
        existing_state = self.state_store.load(thread_id) or {}
        prompts = collect_candidate_strings(payload, PROMPT_KEY_RE, limit=10)
        prompt = max(prompts, key=len) if prompts else None
        prompt_summary = safe_summary(prompt, self.config.capture_text)
        trace_id = str(existing_state.get("trace_id") or f"codex_custom_{stable_short(thread_id, 24)}")
        session_started_at = str(existing_state.get("started_at") or now)
        try:
            turn_index = int(existing_state.get("turn_index") or 0) + 1
        except (TypeError, ValueError):
            turn_index = 1
        turn_id = f"turn_{turn_index}_{stable_short(event_id + (prompt or ''), 10)}"
        chat_span_id = f"{trace_id}_chat_{turn_id}"
        cwd = find_first_key(payload, ("cwd", "current_working_directory", "workingDirectory")) or os.getcwd()
        state = {
            "thread_id": thread_id,
            "trace_id": trace_id,
            "event_id": event_id,
            "turn_index": turn_index,
            "turn_id": turn_id,
            "chat_span_id": chat_span_id,
            "started_at": session_started_at,
            "last_prompt_at": now,
            "first_tool_at": None,
            "last_tool_at": None,
            "last_tool_name": None,
            "last_tool_call_id": None,
            "last_tool_start_span_id": None,
            "last_tool_result_span_id": None,
            "tool_spans": {},
            "cwd": str(cwd),
            "prompt": prompt_summary,
        }
        self._save_state(thread_id, state)
        trace = {
            "trace_id": trace_id,
            "session_id": thread_id,
            "name": "Codex session",
            "status": "unknown",
            "start_time": session_started_at,
            "input": {"prompt": prompt_summary} if prompt_summary else {"prompt": {"redacted": True, "available": False}},
            "metadata": {
                "source": "codex_hook_custom_json",
                "mode": "standard_custom_json",
                "event": "UserPromptSubmit",
                "thread_id": thread_id,
                "event_id": event_id,
                "turn_index": turn_index,
                "turn_id": turn_id,
                "cwd": str(cwd),
                "hook_payload": redacted_copy(payload),
            },
            "spans": [
                make_root_span(trace_id, session_started_at),
                {
                    **make_chat_span(trace_id, chat_span_id, turn_id, turn_index, now, prompt_summary=prompt_summary),
                    "metadata": {
                        "event": "UserPromptSubmit",
                        "turn_index": turn_index,
                        "turn_id": turn_id,
                        "prompt_redacted": not self.config.capture_text,
                    },
                },
            ],
        }
        return thread_id, trace_id, trace

    def build_stop_trace(self, payload: Any, raw_text: str) -> tuple[str, str, dict[str, Any]]:
        now = utc_now()
        explicit_thread_id = find_first_key(payload, ID_KEYS)
        thread_id = self.choose_thread_id(payload, raw_text)
        thread_id, state = self._state_for_event(thread_id, explicit_thread_id not in (None, ""))
        trace_id = str((state or {}).get("trace_id") or f"codex_custom_{stable_short(thread_id, 24)}")
        started_at = str((state or {}).get("started_at") or now)
        turn_id = str((state or {}).get("turn_id") or f"turn_unknown_{stable_short(raw_text or now, 10)}")
        chat_span_id = str((state or {}).get("chat_span_id") or f"{trace_id}_chat_{turn_id}")
        stop_span_id = f"{trace_id}_stop_{turn_id}"
        turn_index = (state or {}).get("turn_index")
        chat_started_at = str((state or {}).get("last_prompt_at") or started_at)
        outputs = collect_candidate_strings(payload, OUTPUT_KEY_RE, limit=12)
        output_summary = (
            safe_summary(max(outputs, key=len), self.config.capture_text)
            if outputs
            else newest_session_output_summary(self.config.base_dir, self.config.capture_text)
        )
        thinking_spans = build_reasoning_spans(
            trace_id,
            chat_span_id,
            turn_id,
            turn_index,
            state,
            now,
            started_at,
            self.read_model_reasoning_effort(),
        )
        trace = {
            "trace_id": trace_id,
            "session_id": thread_id,
            "name": "Codex session",
            "status": "success",
            "start_time": started_at,
            "end_time": now,
            "input": {"prompt": (state or {}).get("prompt")} if (state or {}).get("prompt") else None,
            "output": {"completion": output_summary or {"available": False, "reason": "Stop hook did not expose assistant output"}},
            "metadata": {
                "source": "codex_hook_custom_json",
                "mode": "standard_custom_json",
                "event": "Stop",
                "thread_id": thread_id,
                "turn_index": turn_index,
                "turn_id": turn_id,
                "completed_at": now,
                "hook_payload": redacted_copy(payload),
            },
            "spans": [
                make_root_span(trace_id, started_at, "success"),
                make_chat_span(trace_id, chat_span_id, turn_id, turn_index, chat_started_at, "success", (state or {}).get("prompt")),
                *thinking_spans,
                {
                    "span_id": stop_span_id,
                    "parent_span_id": chat_span_id,
                    "name": f"Stop turn {turn_index}" if turn_index else f"Stop {turn_id}",
                    "type": "workflow",
                    "status": "success",
                    "start_time": now,
                    "end_time": now,
                    "output": {"completion": output_summary} if output_summary else None,
                    "metadata": {
                        "event": "Stop",
                        "turn_index": turn_index,
                        "turn_id": turn_id,
                        "output_redacted": not self.config.capture_text,
                    },
                },
            ],
        }
        return thread_id, trace_id, trace

    def build_tool_trace(self, payload: Any, raw_text: str, event_name: str) -> tuple[str, str, dict[str, Any]]:
        now = utc_now()
        normalized_event = normalize_event_name(event_name)
        explicit_thread_id = find_first_key(payload, ID_KEYS)
        thread_id = self.choose_thread_id(payload, raw_text)
        thread_id, state = self._state_for_event(thread_id, explicit_thread_id not in (None, ""))

        trace_id = str((state or {}).get("trace_id") or f"codex_custom_{stable_short(thread_id, 24)}")
        started_at = str((state or {}).get("started_at") or now)
        turn_index = (state or {}).get("turn_index")
        turn_id = str((state or {}).get("turn_id") or f"turn_unknown_{stable_short(thread_id, 10)}")
        chat_span_id = str((state or {}).get("chat_span_id") or f"{trace_id}_chat_{turn_id}")
        chat_started_at = str((state or {}).get("last_prompt_at") or started_at)

        event_id = self.choose_event_id(payload, raw_text)
        tool_name = find_tool_name(payload, event_name)
        tool_id_part = safe_id_part(tool_name, 40)
        raw_tool_call_id = find_first_key(payload, TOOL_CALL_ID_KEYS)
        tool_call_id = str(raw_tool_call_id) if raw_tool_call_id not in (None, "") else ""
        tool_spans = (state or {}).get("tool_spans")
        if not isinstance(tool_spans, dict):
            tool_spans = {}

        is_terminal = any(part in normalized_event for part in ("post", "result", "end", "complete", "finish"))
        if is_terminal and not tool_call_id and (state or {}).get("last_tool_call_id"):
            tool_call_id = str((state or {}).get("last_tool_call_id"))
        if not tool_call_id:
            tool_call_id = f"synthetic_{stable_short(event_id + tool_name, 10)}"
        tool_call_state: dict[str, Any] = {}
        if tool_call_id and tool_call_id in tool_spans:
            existing_tool_state = tool_spans[tool_call_id]
            if isinstance(existing_tool_state, dict):
                tool_call_state = dict(existing_tool_state)
            elif isinstance(existing_tool_state, str):
                tool_call_state = {"start_span_id": existing_tool_state}
        tool_short = stable_short((tool_call_id or event_id) + tool_name, 10)
        start_span_id = str(tool_call_state.get("start_span_id") or f"{trace_id}_tool_start_{turn_id}_{tool_id_part}_{tool_short}")
        result_span_id = str(tool_call_state.get("result_span_id") or f"{trace_id}_tool_result_{turn_id}_{tool_id_part}_{tool_short}")
        tool_span_id = result_span_id if is_terminal else start_span_id
        tool_started_at = str(tool_call_state.get("started_at") or now)

        tool_input = find_tool_payload_value(payload, TOOL_INPUT_KEYS)
        tool_output = find_tool_payload_value(payload, TOOL_OUTPUT_KEYS) if is_terminal else None
        input_summary = summarize_value(tool_input, self.config.capture_text)
        output_summary = summarize_value(tool_output, self.config.capture_text)

        status_value = find_first_key(payload, ("status", "state", "outcome"))
        status_text = str(status_value).lower() if status_value not in (None, "") else ""
        if "error" in status_text or "fail" in status_text or find_first_key(payload, ("error", "errors")) not in (None, ""):
            span_status = "error"
        elif is_terminal:
            span_status = "success"
        else:
            span_status = "running"

        updated_state = dict(state or {})
        updated_state.update(
            {
                "thread_id": thread_id,
                "trace_id": trace_id,
                "started_at": started_at,
                "turn_index": turn_index,
                "turn_id": turn_id,
                "chat_span_id": chat_span_id,
                "first_tool_at": updated_state.get("first_tool_at") or tool_started_at,
                "last_tool_at": now,
                "last_tool_name": tool_name,
                "last_tool_call_id": tool_call_id,
                "last_tool_start_span_id": start_span_id,
                "last_tool_result_span_id": result_span_id if is_terminal else updated_state.get("last_tool_result_span_id"),
            }
        )
        tool_call_state.update(
            {
                "tool_name": tool_name,
                "started_at": tool_started_at,
                "last_seen_at": now,
                "start_span_id": start_span_id,
                "result_span_id": result_span_id,
            }
        )
        if is_terminal:
            tool_call_state["completed_at"] = now
        tool_spans[tool_call_id] = tool_call_state
        updated_state["tool_spans"] = dict(list(tool_spans.items())[-50:])
        self._save_state(thread_id, updated_state)

        span_kind = "result" if is_terminal else "start"
        tool_metadata = {
            "event": event_name,
            "thread_id": thread_id,
            "turn_index": turn_index,
            "turn_id": turn_id,
            "span_kind": span_kind,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "tool_start_span_id": start_span_id,
            "tool_result_span_id": result_span_id,
            "tool_input_summary": input_summary,
            "tool_output_summary": output_summary,
            "tool_payload_redacted": not self.config.capture_text,
            "hook_payload": redacted_copy(payload),
        }
        trace = {
            "trace_id": trace_id,
            "session_id": thread_id,
            "name": "Codex session",
            "status": "unknown" if span_status == "running" else span_status,
            "start_time": started_at,
            "metadata": {
                "source": "codex_hook_custom_json",
                "mode": "standard_custom_json",
                "event": event_name,
                "thread_id": thread_id,
                "turn_index": turn_index,
                "turn_id": turn_id,
                "tool_name": tool_name,
            },
            "spans": [
                make_root_span(trace_id, started_at),
                make_chat_span(trace_id, chat_span_id, turn_id, turn_index, chat_started_at),
                {
                    "span_id": tool_span_id,
                    "parent_span_id": chat_span_id,
                    "name": f"Tool {'result' if is_terminal else 'start'}: {tool_name}",
                    "type": "tool",
                    "status": span_status,
                    "start_time": tool_started_at if is_terminal else now,
                    "end_time": now if is_terminal else None,
                    "input": {"tool": input_summary} if input_summary else None,
                    "output": {"tool": output_summary} if output_summary else None,
                    "metadata": tool_metadata,
                },
            ],
        }
        return thread_id, trace_id, trace

    def build_trace(self, event_name: str, payload: Any, raw_text: str) -> tuple[str, str, dict[str, Any]]:
        event = normalize_event_name(event_name)
        if event == "self_test":
            trace = build_self_test_trace()
            return (
                str(trace["session_id"]),
                str(trace["trace_id"]),
                to_runtime_payload(trace, source_type="codex", adapter_name="codex_hooks", event_name=event_name),
            )
        if event in {"user_prompt_submit", "userpromptsubmit", "prompt_submit"}:
            thread_id, trace_id, trace = self.build_user_prompt_trace(payload, raw_text)
            return thread_id, trace_id, to_runtime_payload(trace, source_type="codex", adapter_name="codex_hooks", event_name=event_name)
        if event == "stop":
            thread_id, trace_id, trace = self.build_stop_trace(payload, raw_text)
            return thread_id, trace_id, to_runtime_payload(trace, source_type="codex", adapter_name="codex_hooks", event_name=event_name)
        if is_tool_event(event_name, payload):
            thread_id, trace_id, trace = self.build_tool_trace(payload, raw_text, event_name)
            return thread_id, trace_id, to_runtime_payload(trace, source_type="codex", adapter_name="codex_hooks", event_name=event_name)
        thread_id, trace_id, trace = self.build_tool_trace(payload, raw_text, event_name)
        return thread_id, trace_id, to_runtime_payload(trace, source_type="codex", adapter_name="codex_hooks", event_name=event_name)

    def handle(
        self,
        event_name: str,
        payload: Any,
        raw_text: str,
        dry_run: bool = False,
        stdout: TextIO | None = None,
    ) -> int:
        out = stdout if stdout is not None else sys.stdout
        _, trace_id, trace = self.build_trace(event_name, payload, raw_text)

        if dry_run:
            print(json.dumps(redacted_copy(trace), ensure_ascii=False, indent=2, default=str), file=out)
            return 0

        if not self.config.ingest_key:
            self.log_event({"level": "error", "message": "OPENMIA_CUSTOM_JSON_INGEST_KEY missing", "trace_id": trace_id})
            return 0

        try:
            status, response = self.client.post_trace(trace)
            self.log_event(
                {
                    "level": "info",
                    "message": f"uploaded status {status}",
                    "status": status,
                    "trace_id": trace_id,
                    "response": response[:512],
                }
            )
        except urllib.error.HTTPError as exc:
            body = exc.read(2048).decode("utf-8", errors="replace")
            self.log_event({"level": "error", "message": "http_error", "status": exc.code, "trace_id": trace_id, "body": body})
        except Exception as exc:  # noqa: BLE001 - hooks must never block Codex
            self.log_event({"level": "error", "message": "upload_failed", "trace_id": trace_id, "error": repr(exc)})
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("event")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    payload, raw_text = parse_stdin()
    collector = CodexCollector.from_env()
    return collector.handle(args.event, payload, raw_text, dry_run=args.dry_run)
