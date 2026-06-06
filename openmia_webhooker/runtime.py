from __future__ import annotations

from typing import Any

RUNTIME_SCHEMA_VERSION = "openmia.runtime.v1"
WEBHOOKER_ADAPTER_VERSION = "0.1.0"


def to_runtime_payload(
    trace: dict[str, Any],
    *,
    source_type: str,
    adapter_name: str,
    event_name: str | None = None,
) -> dict[str, Any]:
    """Wrap an OpenMIA custom_json trace as Runtime JSON v1.

    The SDK/webhooker owns source semantics. The app server owns validation,
    raw-first storage and normalized writes.
    """

    session_id = _first(trace.get("session_id"), trace.get("sessionId"))
    metadata = _record(trace.get("metadata"))
    source_metadata = {
        "type": source_type,
        "adapter": adapter_name,
        "adapterVersion": WEBHOOKER_ADAPTER_VERSION,
        "event": event_name or metadata.get("event"),
    }
    spans = [_runtime_span(span) for span in _list(trace.get("spans")) if isinstance(span, dict)]

    return {
        "schemaVersion": RUNTIME_SCHEMA_VERSION,
        "source": source_metadata,
        "session": {
            "id": session_id,
            "name": _first(trace.get("session_name"), session_id),
            "startedAt": _first(trace.get("session_started_at"), trace.get("start_time"), trace.get("startTime")),
            "endedAt": _first(trace.get("session_ended_at"), trace.get("end_time"), trace.get("endTime")),
            "metadata": {
                "source": source_type,
                "adapter": adapter_name,
            },
        },
        "trace": {
            "id": _first(trace.get("trace_id"), trace.get("id"), trace.get("traceId")),
            "name": trace.get("name"),
            "status": trace.get("status"),
            "startTime": _first(trace.get("start_time"), trace.get("startTime")),
            "endTime": _first(trace.get("end_time"), trace.get("endTime")),
            "input": trace.get("input"),
            "output": trace.get("output"),
            "metadata": {
                **metadata,
                "runtime_schema": RUNTIME_SCHEMA_VERSION,
                "runtime_source": source_metadata,
            },
        },
        "spans": spans,
    }


def _runtime_span(span: dict[str, Any]) -> dict[str, Any]:
    metadata = _record(span.get("metadata"))
    return {
        "id": _first(span.get("span_id"), span.get("id"), span.get("spanId")),
        "parentId": _first(span.get("parent_span_id"), span.get("parent_id"), span.get("parentId")),
        "roundId": _first(span.get("round_id"), span.get("roundId"), metadata.get("turn_id")),
        "name": span.get("name"),
        "type": _first(span.get("type"), span.get("span_type"), span.get("spanType")),
        "status": span.get("status"),
        "startTime": _first(span.get("start_time"), span.get("startTime")),
        "endTime": _first(span.get("end_time"), span.get("endTime")),
        "latencyMs": _first(span.get("latency_ms"), span.get("latencyMs")),
        "input": span.get("input"),
        "output": span.get("output"),
        "metadata": metadata,
        "modelProvider": _first(span.get("model_provider"), span.get("modelProvider")),
        "modelName": _first(span.get("model_name"), span.get("modelName")),
        "usage": span.get("usage"),
        "cost": span.get("cost"),
        "errorMessage": _first(span.get("error_message"), span.get("errorMessage"), span.get("error")),
    }


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None
