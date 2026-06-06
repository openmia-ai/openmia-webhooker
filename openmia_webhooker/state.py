from __future__ import annotations

import json
import pathlib
import tempfile
from typing import Any

from .redaction import OUTPUT_KEY_RE, collect_candidate_strings, safe_summary


class FileStateStore:
    """Tiny JSON file state store for linking hook events into a trace."""

    def __init__(self, state_dir: pathlib.Path, fallback_state_dir: pathlib.Path | None = None) -> None:
        self.state_dir = state_dir
        self.fallback_state_dir = fallback_state_dir or pathlib.Path(tempfile.gettempdir()) / "openmia-custom-json" / "state"

    def path_for(self, thread_id: str, state_dir: pathlib.Path | None = None, create: bool = True) -> pathlib.Path:
        from .utils import stable_short

        target_dir = state_dir or self.state_dir
        if create:
            target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / f"{stable_short(thread_id, 24)}.json"

    def candidate_paths(self, thread_id: str) -> list[pathlib.Path]:
        paths = [self.path_for(thread_id, self.state_dir, create=False)]
        fallback = self.path_for(thread_id, self.fallback_state_dir, create=False)
        if fallback != paths[0]:
            paths.append(fallback)
        return paths

    def load(self, thread_id: str) -> dict[str, Any] | None:
        for path in self.candidate_paths(thread_id):
            if not path.exists():
                continue
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
        return None

    def save(self, thread_id: str, state: dict[str, Any]) -> None:
        last_error: OSError | None = None
        for state_dir in (self.state_dir, self.fallback_state_dir):
            try:
                path = self.path_for(thread_id, state_dir)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                tmp.replace(path)
                return
            except OSError as exc:
                last_error = exc
        if last_error:
            raise last_error

    def newest(self) -> tuple[str, dict[str, Any]] | None:
        states: list[pathlib.Path] = []
        for state_dir in (self.state_dir, self.fallback_state_dir):
            if not state_dir.exists():
                continue
            try:
                states.extend(state_dir.glob("*.json"))
            except OSError:
                continue
        try:
            states = sorted(states, key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return None
        for path in states:
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            thread_id = str(state.get("thread_id") or "")
            if thread_id:
                return thread_id, state
        return None


def newest_session_output_summary(base_dir: pathlib.Path, capture_text: bool) -> dict[str, Any] | None:
    """Best-effort final assistant-output summary from recent Codex session files."""

    sessions_dir = base_dir / "sessions"
    if not sessions_dir.exists():
        return None
    try:
        files = sorted(sessions_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    except OSError:
        return None
    candidate: str | None = None
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-250:]
        except OSError:
            continue
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            row_text = json.dumps(row, ensure_ascii=False).lower()
            if "assistant" not in row_text and "agent" not in row_text:
                continue
            strings = collect_candidate_strings(row, OUTPUT_KEY_RE, limit=10)
            strings = [s for s in strings if len(s.strip()) > 20]
            if strings:
                candidate = max(strings, key=len)
                break
        if candidate:
            break
    return safe_summary(candidate, capture_text)
