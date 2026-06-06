from __future__ import annotations

import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openmia_webhooker.adapters.claude_code import ClaudeCodeCollector, parse_jsonl
from openmia_webhooker.config import OpenMIAConfig


class FakeClient:
    def __init__(self) -> None:
        self.traces = []

    def post_trace(self, trace):
        self.traces.append(trace)
        return 200, '{"success":true}'


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
        self.assertGreaterEqual(len(trace["spans"]), 3)

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


if __name__ == "__main__":
    unittest.main()
