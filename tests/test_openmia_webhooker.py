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

from openmia_webhooker.adapters.codex import CodexCollector, parse_stdin
from openmia_webhooker.config import OpenMIAConfig
from openmia_webhooker.redaction import redacted_copy, summarize_value
from openmia_webhooker.state import FileStateStore


class FakeClient:
    def __init__(self) -> None:
        self.traces = []

    def post_trace(self, trace):
        self.traces.append(trace)
        return 200, '{"success":true}'


class OpenMIAWebhookerTests(unittest.TestCase):
    def make_collector(self, tmpdir: str, capture_text: bool = False) -> tuple[CodexCollector, FakeClient]:
        base_dir = pathlib.Path(tmpdir)
        (base_dir / "config.toml").write_text('model_reasoning_effort = "xhigh"\n', encoding="utf-8")
        config = OpenMIAConfig(
            endpoint="https://example.invalid/custom_json",
            ingest_key="test_ingest_key",
            capture_text=capture_text,
            base_dir=base_dir,
        )
        client = FakeClient()
        collector = CodexCollector(config, client=client, state_store=FileStateStore(config.state_dir))
        return collector, client

    def test_redaction_and_summary(self) -> None:
        payload = {"api_key": "secret", "nested": {"message": "hide me", "safe": "keep me"}}
        redacted = redacted_copy(payload)
        self.assertEqual(redacted["api_key"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["message"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["safe"], "keep me")

        summary = summarize_value({"cmd": "python3 --version"}, capture_text=False)
        self.assertTrue(summary["redacted"])
        self.assertIn("sha256", summary)
        self.assertNotIn("value", summary)

    def test_parse_stdin_handles_json_and_raw_text(self) -> None:
        payload, raw = parse_stdin(io.BytesIO(b'{"conversation_id":"abc"}'))
        self.assertEqual(payload["conversation_id"], "abc")
        self.assertEqual(raw, '{"conversation_id":"abc"}')

        payload, raw = parse_stdin(io.BytesIO(b"not-json"))
        self.assertIn("_raw_stdin_sha256", payload)
        self.assertEqual(raw, "not-json")

    def test_user_prompt_trace_saves_redacted_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            collector, _ = self.make_collector(tmpdir, capture_text=False)
            _, trace_id, trace = collector.build_user_prompt_trace(
                {"conversation_id": "thread-1", "prompt": "private prompt"},
                '{"conversation_id":"thread-1","prompt":"private prompt"}',
            )

            self.assertTrue(trace_id.startswith("codex_custom_"))
            self.assertEqual(trace["session_id"], "thread-1")
            self.assertTrue(trace["input"]["prompt"]["redacted"])
            self.assertNotIn("text", trace["input"]["prompt"])

            state = collector.state_store.load("thread-1")
            self.assertEqual(state["turn_index"], 1)
            self.assertEqual(state["prompt"]["length"], len("private prompt"))

    def test_capture_text_true_keeps_prompt_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            collector, _ = self.make_collector(tmpdir, capture_text=True)
            _, _, trace = collector.build_user_prompt_trace(
                {"conversation_id": "thread-2", "prompt": "visible prompt"},
                "",
            )
            self.assertEqual(trace["input"]["prompt"]["text"], "visible prompt")

    def test_tool_trace_reuses_turn_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            collector, _ = self.make_collector(tmpdir, capture_text=False)
            collector.build_user_prompt_trace({"conversation_id": "thread-3", "prompt": "run a command"}, "")

            _, _, pre_trace = collector.build_tool_trace(
                {
                    "conversation_id": "thread-3",
                    "tool_call_id": "call-1",
                    "tool_name": "exec_command",
                    "cmd": "python3 --version",
                },
                "",
                "pre_tool_use",
            )
            _, _, post_trace = collector.build_tool_trace(
                {
                    "conversation_id": "thread-3",
                    "tool_call_id": "call-1",
                    "tool_name": "exec_command",
                    "stdout": "Python 3.14.4",
                },
                "",
                "post_tool_use",
            )

            self.assertEqual(pre_trace["spans"][3]["status"], "running")
            self.assertEqual(post_trace["spans"][3]["status"], "success")
            self.assertEqual(post_trace["spans"][3]["metadata"]["tool_call_id"], "call-1")
            self.assertTrue(post_trace["spans"][3]["output"]["tool"]["redacted"])

    def test_stop_trace_adds_reasoning_spans(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            collector, _ = self.make_collector(tmpdir, capture_text=False)
            collector.build_user_prompt_trace({"conversation_id": "thread-4", "prompt": "finish"}, "")
            _, _, trace = collector.build_stop_trace({"conversation_id": "thread-4", "output": "done"}, "")

            span_names = [span["name"] for span in trace["spans"]]
            self.assertIn("Thinking", span_names)
            self.assertEqual(trace["status"], "success")
            self.assertTrue(trace["output"]["completion"]["redacted"])

    def test_handle_dry_run_and_upload_with_fake_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            collector, client = self.make_collector(tmpdir, capture_text=False)

            out = io.StringIO()
            self.assertEqual(collector.handle("self_test", {}, "", dry_run=True, stdout=out), 0)
            dry_trace = json.loads(out.getvalue())
            self.assertEqual(dry_trace["schemaVersion"], "openmia.runtime.v1")
            self.assertEqual(dry_trace["source"]["type"], "codex")
            self.assertEqual(dry_trace["trace"]["input"], "[REDACTED]")

            self.assertEqual(collector.handle("self_test", {}, "", dry_run=False), 0)
            self.assertEqual(len(client.traces), 1)
            self.assertEqual(client.traces[0]["schemaVersion"], "openmia.runtime.v1")

    def test_build_trace_outputs_runtime_json_v1(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            collector, _ = self.make_collector(tmpdir, capture_text=False)
            _, trace_id, trace = collector.build_trace(
                "UserPromptSubmit",
                {"conversation_id": "runtime-thread", "prompt": "hello"},
                "",
            )

            self.assertTrue(trace_id.startswith("codex_custom_"))
            self.assertEqual(trace["schemaVersion"], "openmia.runtime.v1")
            self.assertEqual(trace["source"]["type"], "codex")
            self.assertEqual(trace["session"]["id"], "runtime-thread")
            self.assertEqual(trace["trace"]["id"], trace_id)
            self.assertGreaterEqual(len(trace["spans"]), 2)
            self.assertEqual(trace["spans"][1]["type"], "round")
            self.assertEqual(trace["spans"][1]["parentId"], f"{trace_id}_root")
            self.assertEqual(trace["spans"][2]["type"], "chat")
            self.assertEqual(trace["spans"][2]["parentId"], trace["spans"][1]["id"])
            self.assertEqual(trace["spans"][2]["roundId"], trace["spans"][2]["metadata"]["turn_id"])

    def test_same_codex_session_reuses_trace_and_increments_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            collector, _ = self.make_collector(tmpdir, capture_text=False)
            _, first_trace_id, first_trace = collector.build_trace(
                "UserPromptSubmit",
                {"conversation_id": "codex-multi-round", "prompt": "first"},
                "",
            )
            _, second_trace_id, second_trace = collector.build_trace(
                "UserPromptSubmit",
                {"conversation_id": "codex-multi-round", "prompt": "second"},
                "",
            )

            self.assertEqual(first_trace_id, second_trace_id)
            self.assertEqual(first_trace["trace"]["id"], second_trace["trace"]["id"])
            self.assertEqual(first_trace["session"]["id"], "codex-multi-round")

            first_round = next(span for span in first_trace["spans"] if span["type"] == "round")
            second_round = next(span for span in second_trace["spans"] if span["type"] == "round")
            first_chat = next(span for span in first_trace["spans"] if span["type"] == "chat")
            second_chat = next(span for span in second_trace["spans"] if span["type"] == "chat")

            self.assertTrue(first_round["roundId"].startswith("turn_1_"))
            self.assertTrue(second_round["roundId"].startswith("turn_2_"))
            self.assertEqual(first_chat["parentId"], first_round["id"])
            self.assertEqual(second_chat["parentId"], second_round["id"])
            self.assertEqual(first_chat["roundId"], first_round["roundId"])
            self.assertEqual(second_chat["roundId"], second_round["roundId"])
            self.assertNotEqual(first_chat["id"], second_chat["id"])

    def test_state_store_falls_back_when_primary_is_unwritable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fallback = pathlib.Path(tmpdir) / "fallback-state"
            store = FileStateStore(pathlib.Path("/proc/openmia-unwritable-state"), fallback_state_dir=fallback)
            store.save("fallback-thread", {"thread_id": "fallback-thread", "value": 42})

            self.assertEqual(store.load("fallback-thread")["value"], 42)
            self.assertTrue(store.path_for("fallback-thread", fallback).exists())


if __name__ == "__main__":
    unittest.main()
