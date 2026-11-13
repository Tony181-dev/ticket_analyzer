"""Copilot CLI argument construction tests (no CLI install, login, or network needed)."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.ai import copilot  # noqa: E402


class FakeProcess:
    def __init__(self, stdout=b"analysis", stderr=b"", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def communicate(self, timeout=None):
        return self.stdout, self.stderr


class CopilotCliTest(unittest.TestCase):
    def test_analysis_effort_and_context_are_passed_to_cli(self):
        proc = FakeProcess()
        with tempfile.TemporaryDirectory(prefix="ta-copilot-test-") as cwd, \
                patch.object(copilot, "command", return_value=["copilot"]), \
                patch.object(copilot.subprocess, "Popen", return_value=proc) as popen:
            result = copilot.ask("analyze evidence", cwd, "gpt-6-luna",
                                 effort="high", context="long_context")

        args = popen.call_args.args[0]
        self.assertEqual(result, "analysis")
        self.assertEqual(args[args.index("--effort") + 1], "high")
        self.assertEqual(args[args.index("--context") + 1], "long_context")
        self.assertEqual(args[args.index("--model") + 1], "gpt-6-luna")
        self.assertEqual(args[args.index("--output-format") + 1], "json")

    def test_jsonl_usage_events_return_answer_and_token_counts(self):
        events = [
            {"type": "assistant.message", "data": {"content": "Inspecting the attached file.", "toolRequests": [{"toolName": "read_file"}]}},
            {"type": "assistant.message", "data": {"content": "measured answer"}},
            {"type": "assistant.usage", "data": {
                "model": "gpt-6-luna", "inputTokens": 120, "outputTokens": 24,
                "cacheReadTokens": 40, "cacheWriteTokens": 5, "reasoningTokens": 8,
            }},
        ]
        proc = FakeProcess("\n".join(json.dumps(event) for event in events).encode("utf-8"))
        usage = []
        with tempfile.TemporaryDirectory(prefix="ta-copilot-test-") as cwd, \
                patch.object(copilot, "command", return_value=["copilot"]), \
                patch.object(copilot.subprocess, "Popen", return_value=proc):
            result = copilot.ask("prompt", cwd, on_usage=usage.append)

        self.assertEqual(result, "measured answer")
        self.assertEqual(usage, [{
            "input_tokens": 120, "output_tokens": 24, "cache_read_tokens": 40,
            "cache_write_tokens": 5, "reasoning_tokens": 8, "model_calls": 1,
        }])

    def test_older_cli_falls_back_to_plain_text_output(self):
        rejected = FakeProcess(stdout=b"", stderr=b"error: unknown option '--output-format'", returncode=1)
        legacy = FakeProcess(stdout=b"legacy answer")
        with tempfile.TemporaryDirectory(prefix="ta-copilot-test-") as cwd, \
                patch.object(copilot, "command", return_value=["copilot"]), \
                patch.object(copilot.subprocess, "Popen", side_effect=[rejected, legacy]) as popen:
            result = copilot.ask("prompt", cwd)

        self.assertEqual(result, "legacy answer")
        retry_args = popen.call_args_list[1].args[0]
        self.assertNotIn("--output-format", retry_args)


if __name__ == "__main__":
    unittest.main()