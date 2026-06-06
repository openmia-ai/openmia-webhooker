from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openmia_webhooker.adapters.claude_code import ClaudeCodeCollector, parse_jsonl, run_claude_code, watch_project_logs
from openmia_webhooker.config import OpenMIAConfig


class FakeClient:
    def __init__(self) -> None:
        self.traces = []

    def post_trace(self, trace):
        self.traces.append(trace)
        return 200, '{"success":true}'


class FakeClaudeProcess:
    command: list[str] = []

    def __init__(self, command, stdout=None, stderr=None, text=None) -> None:
        self.command = list(command)
        FakeClaudeProcess.command = self.command
        self.returncode = 0

    def communicate(self) -> tuple[str, str]:
        return (
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "system",
                            "subtype": "init",
                            "session_id": "claude-run-session",
                            "model": "claude-sonnet-4-6",
                            "claude_code_version": "2.1.153",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "result",
                            "is_error": False,
                            "result": "wrapper result",
                            "session_id": "claude-run-session",
                            "usage": {"input_tokens": 10, "output_tokens": 2},
                        }
                    ),
                ]
            )
            + "\n",
            "claude stderr\n",
        )


class ClaudeCodeAdapterTests(unittest.TestCase):
    def make_collector(self, capture_text: bool = False) -> tuple[ClaudeCodeCollector, FakeClient]:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        config = OpenMIAConfig(
            endpoint="https://example.invalid/custom_json",
            ingest_key="test_ingest_key",
            capture_text=capture_text,
            base_dir=pathlib.Path(tmpdir.name),
        )
        client = FakeClient()
        return ClaudeCodeCollector(config, client=client), client

    def test_parse_jsonl_keeps_valid_events_and_summarizes_invalid_lines(self) -> None:
        events = parse_jsonl(['{"type":"system","subtype":"init"}\n', "not-json\n"])

        self.assertEqual(events[0]["type"], "system")
        self.assertEqual(events[1]["type"], "raw")
        self.assertEqual(events[1]["subtype"], "unparsed_line")

    def test_build_success_trace_from_stream_json(self) -> None:
        collector, _ = self.make_collector(capture_text=False)
        events = [
            {
                "type": "system",
                "subtype": "init",
                "cwd": "/root",
                "session_id": "claude-session-1",
                "model": "claude-sonnet-4-6",
                "claude_code_version": "2.1.153",
                "tools": ["Bash", "Read"],
                "permissionMode": "default",
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hello from claude"}]},
                "uuid": "assistant-1",
            },
            {
                "type": "result",
                "is_error": False,
                "result": "hello from claude",
                "session_id": "claude-session-1",
            },
        ]

        trace = collector.build_trace(events, prompt="say hello")

        self.assertEqual(trace["session_id"], "claude-session-1")
        self.assertEqual(trace["status"], "success")
        self.assertEqual(trace["metadata"]["source"], "claude_code_stream_json")
        self.assertTrue(trace["input"]["prompt"]["redacted"])
        self.assertNotIn("text", trace["input"]["prompt"])
        self.assertTrue(trace["output"]["completion"]["redacted"])
        self.assertEqual(len(trace["spans"]), 1)
        self.assertEqual(trace["spans"][0]["type"], "chat")
        self.assertIsNone(trace["spans"][0]["parent_span_id"])

    def test_same_session_uploads_increment_rounds_in_one_trace(self) -> None:
        collector, client = self.make_collector(capture_text=False)
        events = [
            {"type": "system", "subtype": "init", "session_id": "claude-multi-round"},
            {"type": "result", "is_error": False, "result": "ok", "session_id": "claude-multi-round"},
        ]

        self.assertEqual(collector.handle(events, prompt="first", dry_run=False), 0)
        self.assertEqual(collector.handle(events, prompt="second", dry_run=False), 0)

        self.assertEqual(len(client.traces), 2)
        first_trace, second_trace = client.traces
        self.assertEqual(first_trace["schemaVersion"], "openmia.runtime.v1")
        self.assertEqual(second_trace["schemaVersion"], "openmia.runtime.v1")
        self.assertEqual(first_trace["session"]["id"], "claude-multi-round")
        self.assertEqual(first_trace["trace"]["id"], second_trace["trace"]["id"])

        first_chat = next(span for span in first_trace["spans"] if span["type"] == "chat")
        second_chat = next(span for span in second_trace["spans"] if span["type"] == "chat")

        self.assertNotIn("round", {span["type"] for span in first_trace["spans"]})
        self.assertNotIn("round", {span["type"] for span in second_trace["spans"]})
        self.assertTrue(first_chat["roundId"].startswith("round_1_"))
        self.assertTrue(second_chat["roundId"].startswith("round_2_"))
        self.assertIsNone(first_chat["parentId"])
        self.assertIsNone(second_chat["parentId"])
        self.assertNotEqual(first_chat["id"], second_chat["id"])

    def test_build_error_trace_from_api_retries_without_result(self) -> None:
        collector, _ = self.make_collector(capture_text=False)
        events = [
            {"type": "system", "subtype": "init", "session_id": "retry-session"},
            {"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 10, "error": "unknown"},
        ]

        trace = collector.build_trace(events)

        self.assertEqual(trace["status"], "error")
        retry_spans = [span for span in trace["spans"] if span["metadata"].get("event_subtype") == "api_retry"]
        self.assertEqual(len(retry_spans), 1)
        self.assertEqual(retry_spans[0]["status"], "error")

    def test_handle_dry_run_and_upload(self) -> None:
        collector, client = self.make_collector(capture_text=False)
        events = [{"type": "system", "subtype": "init", "session_id": "upload-session"}]

        out = io.StringIO()
        self.assertEqual(collector.handle(events, dry_run=True, stdout=out), 0)
        dry_trace = json.loads(out.getvalue())
        self.assertEqual(dry_trace["schemaVersion"], "openmia.runtime.v1")
        self.assertEqual(dry_trace["source"]["type"], "claude_code")

        self.assertEqual(collector.handle(events, dry_run=False), 0)
        self.assertEqual(len(client.traces), 1)
        self.assertEqual(client.traces[0]["schemaVersion"], "openmia.runtime.v1")

    def test_runtime_payload_has_top_level_chat_without_session_or_round_span(self) -> None:
        collector, client = self.make_collector(capture_text=False)
        events = [
            {"type": "system", "subtype": "init", "session_id": "shape-session"},
            {"type": "result", "is_error": False, "result": "ok", "session_id": "shape-session"},
        ]

        collector.handle(events, prompt="hello", dry_run=False)

        runtime_trace = client.traces[0]
        self.assertEqual(runtime_trace["schemaVersion"], "openmia.runtime.v1")
        chat_spans = [span for span in runtime_trace["spans"] if span["type"] == "chat"]
        self.assertEqual(len(chat_spans), 1)
        self.assertIsNone(chat_spans[0]["parentId"])
        self.assertTrue(chat_spans[0]["roundId"].startswith("round_1_"))
        self.assertNotIn("round", {span["type"] for span in runtime_trace["spans"]})
        self.assertNotIn("Claude Code session", {span["name"] for span in runtime_trace["spans"]})

    def test_claude_code_run_invokes_stream_json_and_outputs_result(self) -> None:
        collector, client = self.make_collector(capture_text=False)
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_claude_code(
            ["--", "--print=hello"],
            dry_run=False,
            stdout=stdout,
            stderr=stderr,
            popen_factory=FakeClaudeProcess,
            collector=collector,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(FakeClaudeProcess.command[:2], ["claude", "--print=hello"])
        self.assertIn("--verbose", FakeClaudeProcess.command)
        self.assertIn("--output-format", FakeClaudeProcess.command)
        self.assertIn("stream-json", FakeClaudeProcess.command)
        self.assertIn("--include-hook-events", FakeClaudeProcess.command)
        self.assertIn("claude stderr", stderr.getvalue())
        self.assertEqual(stdout.getvalue().strip(), "wrapper result")
        self.assertEqual(len(client.traces), 1)
        runtime_trace = client.traces[0]
        self.assertEqual(runtime_trace["schemaVersion"], "openmia.runtime.v1")
        chat_span = next(span for span in runtime_trace["spans"] if span["type"] == "chat")
        self.assertIsNone(chat_span["parentId"])
        self.assertEqual(chat_span["modelName"], "claude-sonnet-4-6")
        self.assertEqual(chat_span["usage"], {"input_tokens": 10, "output_tokens": 2})

    def test_claude_code_run_rejects_conflicting_output_format(self) -> None:
        collector, client = self.make_collector(capture_text=False)
        stderr = io.StringIO()

        exit_code = run_claude_code(
            ["-p", "hello", "--output-format", "json"],
            dry_run=False,
            stderr=stderr,
            popen_factory=FakeClaudeProcess,
            collector=collector,
        )

        self.assertEqual(exit_code, 2)
        self.assertIn("manages --output-format", stderr.getvalue())
        self.assertEqual(client.traces, [])

    def test_claude_code_watch_once_uploads_project_rounds_idempotently(self) -> None:
        collector, client = self.make_collector(capture_text=False)
        projects_dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: _cleanup_tree(projects_dir))
        project = projects_dir / "root-project"
        project.mkdir(parents=True)
        (project / "session.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "user",
                            "uuid": "user-1",
                            "timestamp": "2026-06-06T20:00:00.000Z",
                            "sessionId": "watch-session",
                            "cwd": "/root",
                            "version": "2.1.153",
                            "message": {"role": "user", "content": "hello"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "uuid": "assistant-1",
                            "timestamp": "2026-06-06T20:00:01.000Z",
                            "sessionId": "watch-session",
                            "cwd": "/root",
                            "version": "2.1.153",
                            "message": {
                                "role": "assistant",
                                "model": "claude-sonnet-4-6",
                                "content": [
                                    {"type": "text", "text": "hi"},
                                    {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"file_path": "README.md"}},
                                ],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "uuid": "assistant-2",
                            "timestamp": "2026-06-06T20:00:02.000Z",
                            "sessionId": "watch-session",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "done"}],
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        self.assertEqual(watch_project_logs(projects_dir=projects_dir, once=True, dry_run=False, collector=collector), 0)
        self.assertEqual(watch_project_logs(projects_dir=projects_dir, once=True, dry_run=False, collector=collector), 0)

        self.assertEqual(len(client.traces), 1)
        runtime_trace = client.traces[0]
        self.assertEqual(runtime_trace["schemaVersion"], "openmia.runtime.v1")
        self.assertEqual(runtime_trace["session"]["id"], "watch-session")
        chat_span = next(span for span in runtime_trace["spans"] if span["type"] == "chat")
        self.assertIsNone(chat_span["parentId"])
        self.assertEqual(chat_span["startTime"], "2026-06-06T20:00:00.000Z")
        self.assertTrue(chat_span["roundId"].startswith("round_1_"))
        self.assertNotIn("round", {span["type"] for span in runtime_trace["spans"]})
        tool_spans = [span for span in runtime_trace["spans"] if span["type"] == "tool"]
        self.assertGreaterEqual(len(tool_spans), 2)
        self.assertTrue(all(span["parentId"] == chat_span["id"] for span in tool_spans))

    def test_claude_code_watch_keeps_user_tool_results_in_current_round(self) -> None:
        collector, client = self.make_collector(capture_text=False)
        projects_dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: _cleanup_tree(projects_dir))
        project = projects_dir / "root-project"
        project.mkdir(parents=True)
        (project / "session.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "user",
                            "uuid": "user-1",
                            "timestamp": "2026-06-06T20:00:00.000Z",
                            "sessionId": "tool-result-session",
                            "message": {"role": "user", "content": "read the file"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "uuid": "assistant-1",
                            "timestamp": "2026-06-06T20:00:01.000Z",
                            "sessionId": "tool-result-session",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"file_path": "README.md"}}],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "user",
                            "uuid": "tool-result-1",
                            "timestamp": "2026-06-06T20:00:02.000Z",
                            "sessionId": "tool-result-session",
                            "message": {
                                "role": "user",
                                "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "file contents"}],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "uuid": "assistant-2",
                            "timestamp": "2026-06-06T20:00:03.000Z",
                            "sessionId": "tool-result-session",
                            "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        self.assertEqual(watch_project_logs(projects_dir=projects_dir, once=True, dry_run=False, collector=collector), 0)

        self.assertEqual(len(client.traces), 1)
        runtime_trace = client.traces[0]
        chat_spans = [span for span in runtime_trace["spans"] if span["type"] == "chat"]
        self.assertEqual(len(chat_spans), 1)
        self.assertTrue(chat_spans[0]["roundId"].startswith("round_1_"))
        self.assertNotIn("round_2", json.dumps(runtime_trace, ensure_ascii=False))
        tool_spans = [span for span in runtime_trace["spans"] if span["type"] == "tool"]
        self.assertEqual(len(tool_spans), 2)
        self.assertEqual({span["metadata"]["item_type"] for span in tool_spans}, {"tool_use", "tool_result"})
        self.assertTrue(all(span["parentId"] == chat_spans[0]["id"] for span in tool_spans))

    def test_claude_code_watch_follow_uses_file_mtime_for_rows_without_timestamps(self) -> None:
        collector, client = self.make_collector(capture_text=False)
        projects_dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: _cleanup_tree(projects_dir))
        project = projects_dir / "root-project"
        project.mkdir(parents=True)
        transcript = project / "session.jsonl"
        transcript.write_text(
            "\n".join(
                [
                    json.dumps({"type": "user", "uuid": "user-1", "sessionId": "mtime-session", "message": {"role": "user", "content": "hello"}}),
                    json.dumps({"type": "assistant", "uuid": "assistant-1", "sessionId": "mtime-session", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        old_mtime = 1_780_000_000
        os.utime(transcript, (old_mtime, old_mtime))

        self.assertEqual(
            watch_project_logs(
                projects_dir=projects_dir,
                once=False,
                follow=True,
                idle_flush_sec=10,
                poll_interval_sec=0,
                dry_run=False,
                collector=collector,
                max_iterations=1,
            ),
            0,
        )

        self.assertEqual(len(client.traces), 1)

    def test_claude_code_watch_follow_flushes_after_idle(self) -> None:
        collector, client = self.make_collector(capture_text=False)
        projects_dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: _cleanup_tree(projects_dir))
        project = projects_dir / "root-project"
        project.mkdir(parents=True)
        (project / "session.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"type": "user", "uuid": "user-1", "timestamp": "2026-06-06T20:00:00.000Z", "sessionId": "follow-session", "message": {"content": "hello"}}),
                    json.dumps({"type": "assistant", "uuid": "assistant-1", "timestamp": "2026-06-06T20:00:01.000Z", "sessionId": "follow-session", "message": {"content": [{"type": "text", "text": "hi"}]}}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        self.assertEqual(
            watch_project_logs(
                projects_dir=projects_dir,
                once=False,
                follow=True,
                idle_flush_sec=0,
                poll_interval_sec=0,
                dry_run=False,
                collector=collector,
                max_iterations=1,
            ),
            0,
        )

        self.assertEqual(len(client.traces), 1)


def _cleanup_tree(path: pathlib.Path) -> None:
    with contextlib.suppress(OSError):
        for child in sorted(path.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        path.rmdir()


if __name__ == "__main__":
    unittest.main()
