from __future__ import annotations

import json
import re
from typing import Any

from .utils import sha256_text

SENSITIVE_KEY_RE = re.compile(
    r"(authorization|cookie|api[_-]?key|secret|token|password|credential|bearer|prompt|user[_-]?prompt|content|message|text|input|output)",
    re.IGNORECASE,
)
PROMPT_KEY_RE = re.compile(r"(^|[_\-.])(prompt|user_prompt|userPrompt|input|message|content|text)($|[_\-.])", re.IGNORECASE)
OUTPUT_KEY_RE = re.compile(r"(^|[_\-.])(output|response|answer|assistant|final|content|message|text)($|[_\-.])", re.IGNORECASE)


def summarize_value(value: Any, capture_text: bool) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    summary: dict[str, Any] = {
        "redacted": not capture_text,
        "length": len(text),
        "sha256": sha256_text(text),
    }
    if capture_text:
        summary["value"] = value
    return summary


def safe_summary(text: str | None, capture_text: bool) -> dict[str, Any] | None:
    if not text:
        return None
    summary: dict[str, Any] = {
        "redacted": not capture_text,
        "length": len(text),
        "sha256": sha256_text(text),
    }
    if capture_text:
        summary["text"] = text
    return summary


def collect_candidate_strings(value: Any, key_regex: re.Pattern[str], limit: int = 20) -> list[str]:
    found: list[str] = []

    def walk(node: Any, key_path: str = "") -> None:
        if len(found) >= limit:
            return
        if isinstance(node, dict):
            for key, child in node.items():
                child_path = f"{key_path}.{key}" if key_path else str(key)
                if isinstance(child, str) and key_regex.search(child_path) and child.strip():
                    found.append(child)
                else:
                    walk(child, child_path)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{key_path}.{index}" if key_path else str(index))

    walk(value)
    return found


def redacted_copy(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[DEPTH_LIMIT]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if SENSITIVE_KEY_RE.search(str(key)):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = redacted_copy(child, depth + 1)
        return result
    if isinstance(value, list):
        return [redacted_copy(child, depth + 1) for child in value[:50]]
    if isinstance(value, str):
        if len(value) > 500:
            return {"redactedLongString": True, "length": len(value), "sha256": sha256_text(value)}
        return value
    return value
