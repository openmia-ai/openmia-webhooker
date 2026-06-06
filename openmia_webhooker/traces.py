from __future__ import annotations

import time
from typing import Any

from .utils import sha256_text, stable_short, utc_now


def make_root_span(trace_id: str, started_at: str, status: str = "unknown") -> dict[str, Any]:
    return {
        "span_id": f"{trace_id}_root",
        "name": "Codex session",
        "type": "workflow",
        "status": status,
        "start_time": started_at,
        "metadata": {"source": "codex_hook", "role": "root"},
    }


def make_round_span(
    trace_id: str,
    round_id: str,
    round_index: Any,
    started_at: str,
    status: str = "unknown",
    parent_span_id: str | None = None,
) -> dict[str, Any]:
    return {
        "span_id": f"{trace_id}_round_{round_id}",
        "parent_span_id": parent_span_id or f"{trace_id}_root",
        "round_id": round_id,
        "name": f"Round {round_index}" if round_index else "Round unknown",
        "type": "round",
        "status": status,
        "start_time": started_at,
        "metadata": {"event": "Round", "turn_index": round_index, "turn_id": round_id},
    }


def make_chat_span(
    trace_id: str,
    chat_span_id: str,
    turn_id: str,
    turn_index: Any,
    started_at: str,
    status: str = "unknown",
    prompt_summary: dict[str, Any] | None = None,
    parent_span_id: str | None = None,
) -> dict[str, Any]:
    return {
        "span_id": chat_span_id,
        "parent_span_id": parent_span_id or f"{trace_id}_root",
        "round_id": turn_id,
        "name": f"Chat turn {turn_index}" if turn_index else "Chat turn unknown",
        "type": "chat",
        "status": status,
        "start_time": started_at,
        "input": {"prompt": prompt_summary} if prompt_summary else None,
        "metadata": {"event": "UserPromptSubmit", "turn_index": turn_index, "turn_id": turn_id},
    }


def make_reasoning_span(
    trace_id: str,
    chat_span_id: str,
    turn_id: str,
    turn_index: Any,
    suffix: str,
    name: str,
    started_at: str,
    ended_at: str,
    model_reasoning_effort: str,
    status: str = "success",
) -> dict[str, Any]:
    return {
        "span_id": f"{trace_id}_thinking_{turn_id}_{suffix}",
        "parent_span_id": chat_span_id,
        "name": name,
        "type": "llm",
        "status": status,
        "start_time": started_at,
        "end_time": ended_at,
        "output": {
            "reasoning": {
                "redacted": True,
                "available": False,
                "reason": "Codex hooks do not expose hidden thinking content",
            }
        },
        "metadata": {
            "event": "Reasoning",
            "turn_index": turn_index,
            "turn_id": turn_id,
            "content_redacted": True,
            "hidden_chain_of_thought_uploaded": False,
            "model_reasoning_effort": model_reasoning_effort,
        },
    }


def build_reasoning_spans(
    trace_id: str,
    chat_span_id: str,
    turn_id: str,
    turn_index: Any,
    state: dict[str, Any] | None,
    completed_at: str,
    fallback_started_at: str,
    model_reasoning_effort: str,
) -> list[dict[str, Any]]:
    prompt_at = str((state or {}).get("last_prompt_at") or fallback_started_at)
    first_tool_at = (state or {}).get("first_tool_at")
    last_tool_at = (state or {}).get("last_tool_at")
    if first_tool_at:
        return [
            make_reasoning_span(
                trace_id,
                chat_span_id,
                turn_id,
                turn_index,
                "before_tools",
                "Thinking before tools",
                prompt_at,
                str(first_tool_at),
                model_reasoning_effort,
            ),
            make_reasoning_span(
                trace_id,
                chat_span_id,
                turn_id,
                turn_index,
                "final",
                "Final reasoning",
                str(last_tool_at or first_tool_at),
                completed_at,
                model_reasoning_effort,
            ),
        ]
    return [
        make_reasoning_span(
            trace_id,
            chat_span_id,
            turn_id,
            turn_index,
            "single",
            "Thinking",
            prompt_at,
            completed_at,
            model_reasoning_effort,
        )
    ]


def build_self_test_trace() -> dict[str, Any]:
    now = utc_now()
    trace_id = f"codex_custom_self_test_{stable_short(str(time.time()), 10)}"
    return {
        "trace_id": trace_id,
        "session_id": "codex_custom_self_test",
        "name": "Codex custom JSON collector self-test",
        "status": "success",
        "start_time": now,
        "end_time": now,
        "input": {"prompt": {"redacted": True, "length": 12, "sha256": sha256_text("self test")}},
        "output": {"completion": {"redacted": True, "length": 9, "sha256": sha256_text("completed")}},
        "metadata": {"source": "codex_hook_custom_json", "mode": "self_test"},
        "spans": [
            make_root_span(trace_id, now, "success"),
            make_round_span(trace_id, "self_test_round_1", 1, now, "success"),
            {
                "span_id": f"{trace_id}_chat",
                "parent_span_id": f"{trace_id}_round_self_test_round_1",
                "round_id": "self_test_round_1",
                "name": "Self-test chat",
                "type": "chat",
                "status": "success",
                "start_time": now,
                "end_time": now,
            },
        ],
    }
