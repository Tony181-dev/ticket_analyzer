"""Copilot CLI argument construction tests (no CLI install, login, or network needed)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
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
    def test_public_reasoning_streams_before_the_final_answer_and_keeps_usage(self):
        with tempfile.TemporaryDirectory() as cwd:
            signal = Path(cwd) / "observed"
            received, usage = [], []

            def observe(event):
                received.append(event)
                if event["kind"] == "reasoning":
                    signal.write_text("observed", encoding="utf-8")

            script = """
import json, sys, time
from pathlib import Path
def emit(kind, **data):
    print(json.dumps({'type': kind, 'data': data}), flush=True)
emit('assistant.reasoning_delta', reasoningId='r1', deltaContent='Checking ')
deadline = time.monotonic() + 3
while not Path('observed').exists() and time.monotonic() < deadline: time.sleep(.01)
if not Path('observed').exists(): sys.exit(9)
emit('assistant.reasoning_delta', reasoningId='r1', deltaContent='evidence.')
emit('assistant.reasoning', reasoningId='r1', content='Checking evidence.')
emit('tool.execution_start', toolCallId='t1', toolName='read_file', arguments={'secret': 'NEVER_CAPTURE'})
emit('tool.execution_complete', toolCallId='t1', success=True, result={'content': 'NEVER_CAPTURE'})
emit('assistant.message', messageId='m1', content='Final answer', reasoningText='Checking evidence.', reasoningOpaque='NEVER_CAPTURE')
emit('assistant.usage', inputTokens=12, outputTokens=4, reasoningTokens=2)
"""
            with patch.object(copilot, "command", return_value=[sys.executable, "-u", "-c", script]):
                with copilot.observe(observe):
                    result = copilot.ask("prompt", cwd, on_usage=usage.append)
            self.assertEqual(result, "Final answer")
            self.assertTrue(signal.exists())
            blocks = {e['id']: e for e in received if e['kind'] == 'reasoning'}
            self.assertEqual(len(blocks), 1, 'final snapshot replaces streaming deltas')
            self.assertEqual(next(iter(blocks.values()))['text'], 'Checking evidence.')
            self.assertNotIn('NEVER_CAPTURE', json.dumps(received))
            self.assertEqual(usage[0]['reasoning_tokens'], 2)
            self.assertEqual(received[-1]['kind'], 'call.completed')

    def test_streaming_timeout_retains_partial_trace_and_reaps_process(self):
        script = "import time; print('{\"type\":\"assistant.intent\",\"data\":{\"intent\":\"Inspecting\"}}',flush=True); time.sleep(15)"
        received = []
        with tempfile.TemporaryDirectory() as cwd, patch.object(copilot, "command", return_value=[sys.executable, "-u", "-c", script]):
            with self.assertRaisesRegex(copilot.CopilotError, 'timed out'):
                copilot.ask('prompt', cwd, timeout=.5, on_event=received.append)
        self.assertTrue(any(e['text'] == 'Inspecting' for e in received))
        self.assertEqual(received[-1]['kind'], 'call.failed')
        self.assertIsNone(copilot._slot('pipeline')['proc'])

    def test_scoped_observer_does_not_leak_to_next_call(self):
        received = []
        with copilot.observe(received.append):
            self.assertIsNotNone(copilot._observer.get())
        self.assertIsNone(copilot._observer.get())

    def test_legacy_cli_with_observer_uses_nonstreaming_text_fallback(self):
        script = """
import sys
if '--output-format' in sys.argv:
    print("error: unknown option '--output-format'", file=sys.stderr)
    sys.exit(1)
if sys.argv[sys.argv.index('--stream') + 1] != 'off' or '-s' not in sys.argv: sys.exit(9)
print('legacy answer')
"""
        received = []
        with tempfile.TemporaryDirectory() as cwd, patch.object(copilot, 'command', return_value=[sys.executable, '-u', '-c', script]):
            answer = copilot.ask('prompt', cwd, on_event=received.append)
        self.assertEqual(answer, 'legacy answer')
        self.assertTrue(any(e['kind'] == 'unavailable' for e in received))
        self.assertEqual(received[-1]['kind'], 'call.completed')

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
