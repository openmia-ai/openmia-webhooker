from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from typing import Any, Iterable, TextIO

from ..client import OpenMIAClient
from ..config import OpenMIAConfig
from ..redaction import redacted_copy, safe_summary, summarize_value
from ..runtime import to_runtime_payload
from ..state import FileStateStore
from ..utils import normalize_event_name, stable_short, utc_now


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
    ) -> dict[str, Any]:
        now = utc_now()
        init_event = next((event for event in events if event.get("type") == "system" and event.get("subtype") == "init"), {})
        result_event = next((event for event in reversed(events) if event.get("type") == "result"), {})
        session_id = str(session_id or init_event.get("session_id") or result_event.get("session_id") or f"claude_code_{stable_short(now, 16)}")
        trace_id = f"claude_code_{stable_short(session_id, 24)}"
        status = self._trace_status(events, result_event)
        started_at = str(session_started_at or init_event.get("timestamp") or now)
        round_started_at = str(init_event.get("timestamp") or now)
        ended_at = now if result_event or status in {"error", "timeout"} else None
        prompt_summary = safe_summary(prompt, self.config.capture_text)
        result_text = self._result_text(events, result_event)
        output_summary = safe_summary(result_text, self.config.capture_text)
        round_index = round_index or 1
        round_id = round_id or f"round_{round_index}_{stable_short((prompt or result_text or session_id) + str(round_index), 10)}"
        chat_span_id = f"{trace_id}_chat_{round_id}"

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
                    "model": init_event.get("model"),
                    "claude_code_version": init_event.get("claude_code_version"),
                    "cwd": init_event.get("cwd"),
                    "permission_mode": init_event.get("permissionMode"),
                },
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
                "model": init_event.get("model"),
                "claude_code_version": init_event.get("claude_code_version"),
                "cwd": init_event.get("cwd"),
                "tools": init_event.get("tools"),
                "mcp_servers": init_event.get("mcp_servers"),
                "permission_mode": init_event.get("permissionMode"),
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
        out = stdout if stdout is not None else sys.stdout
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
        trace_id = str(legacy_trace["trace_id"])
        trace = to_runtime_payload(
            legacy_trace,
            source_type="claude_code",
            adapter_name="claude_code_stream_json",
            event_name="stream_json",
        )

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
                    "adapter": "claude_code",
                }
            )
        except urllib.error.HTTPError as exc:
            body = exc.read(2048).decode("utf-8", errors="replace")
            self.log_event({"level": "error", "message": "http_error", "status": exc.code, "trace_id": trace_id, "body": body})
        except Exception as exc:  # noqa: BLE001 - trace upload should not mask Claude Code output collection
            self.log_event({"level": "error", "message": "upload_failed", "trace_id": trace_id, "error": repr(exc)})
        return 0

    def _session_id(self, events: list[dict[str, Any]]) -> str:
        init_event = next((event for event in events if event.get("type") == "system" and event.get("subtype") == "init"), {})
        result_event = next((event for event in reversed(events) if event.get("type") == "result"), {})
        return str(init_event.get("session_id") or result_event.get("session_id") or f"claude_code_{stable_short(utc_now(), 16)}")

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
        if any(event.get("subtype") == "api_retry" for event in events):
            return "error"
        return "unknown"

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
            if event_type == "system" and subtype == "init":
                continue
            span_status = "error" if subtype in {"api_retry", "error"} or event.get("is_error") else "success"
            span_type = "workflow"
            if event_type == "assistant":
                span_type = "llm"
            elif _has_tool_content(event):
                span_type = "tool"
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


def _has_tool_content(event: dict[str, Any]) -> bool:
    return any(str(item.get("type") or "").startswith("tool_") for item in _content_items(event))


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--name", default="Claude Code session")
    parser.add_argument("--prompt", default=None)
    args = parser.parse_args(argv)

    events = parse_jsonl(sys.stdin)
    collector = ClaudeCodeCollector.from_env()
    return collector.handle(events, name=args.name, prompt=args.prompt, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
