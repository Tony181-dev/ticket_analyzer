"""Assistant chat bubble: reply parsing, action whitelist, snapshot, ask flow, bug tickets, Copilot slots."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("TA_DATA_DIR", tempfile.mkdtemp(prefix="ta-test-"))
sys.path.insert(0, str(ROOT))

from app import assistant, mcp, paths, pipeline, settings  # noqa: E402
from app.ai import copilot  # noqa: E402

FAKE_DOCTOR = {"mode": "copilot", "ready": True,
               "checks": [{"label": "MCP Jira", "ok": True, "needed": True, "detail": "http://jira.example"}]}


def wait_idle(timeout=10):
    end = time.time() + timeout
    while assistant.busy() and time.time() < end:
        time.sleep(0.02)
    assert not assistant.busy(), "assistant did not finish"


class AssistantTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        paths.ensure_dirs()
        cls.doctor = patch.object(assistant.doctor, "checks", return_value=FAKE_DOCTOR)
        cls.doctor.start()

    @classmethod
    def tearDownClass(cls):
        cls.doctor.stop()

    def setUp(self):
        assistant._chat_cache["data"] = None
        assistant.CHAT_FILE.unlink(missing_ok=True)
        assistant._doctor_cache.update(at=0.0, value=None)
        self.run_ = pipeline.create("AST-1", gate="review", use_infographic=False)
        self.run_["attachments"] = [
            {"id": "1", "filename": "logs_0312.7z.001", "size": 80 * 1024 ** 2, "kind": "archive", "created": "2026-11-15"},
            {"id": "2", "filename": "logs_0312.7z.002", "size": 50 * 1024 ** 2, "kind": "archive", "created": "2026-11-15"},
            {"id": "3", "filename": "screen.mp4", "size": 210 * 1024 ** 2, "kind": "other", "created": "2026-11-15"},
        ]
        self.run_["focus"] = {"time_start": "12:29:00", "time_end": "12:32:00", "attachments": [
            {"filename": "logs_0312.7z.001", "reason": "near the incident"}]}
        self.run_["pending_gate"] = {"mode": "review", "problem": "", "source": "ai", "suggested": ["logs_0312.7z.001"]}

    # ------------------------------------------------------------ parsing / validation
    def test_parse_reply_extracts_the_actions_block(self):
        raw = ('Tải log trước.\n\n```actions\n[{"type": "log_scan", "label": "Quét"}]\n```')
        text, actions = assistant.parse_reply(raw)
        self.assertEqual(text, "Tải log trước.")
        self.assertEqual(actions, [{"type": "log_scan", "label": "Quét"}])
        text, actions = assistant.parse_reply("Hi\n```actions\n[not json\n```")
        self.assertEqual((text, actions), ("Hi", []))
        code = "Example:\n```json\n{\"a\": 1}\n```"
        self.assertEqual(assistant.parse_reply(code), (code, []))

    def test_validate_action_whitelist(self):
        run = self.run_
        ok = assistant.validate_action({"type": "download_attachments", "names": ["LOGS_0312.7z.001", "nope.zip"]}, run)
        self.assertEqual(ok["names"], ["logs_0312.7z.001"])
        self.assertEqual(ok["status"], "pending")
        self.assertIsNone(assistant.validate_action({"type": "download_attachments", "names": ["nope.zip"]}, run))
        self.assertIsNone(assistant.validate_action({"type": "rm_rf"}, run))
        self.assertIsNone(assistant.validate_action({"type": "log_scan"}, None), "run actions need an open run")
        gate = assistant.validate_action({"type": "resolve_gate", "names": ["logs_0312.7z.001"], "time_start": "bad",
                                          "time_end": "12:31:00", "shift_minutes": 420}, run)
        self.assertEqual((gate["names"], gate.get("time_start"), gate["time_end"], gate["shift_minutes"]),
                         (["logs_0312.7z.001"], None, "12:31:00", 420))
        settings_action = assistant.validate_action(
            {"type": "update_settings", "changes": {"port": 1, "ai_retries": 2, "default_gate": "bogus"}}, run)
        self.assertEqual(settings_action["changes"], {"ai_retries": 2})
        inputs = assistant.validate_action(
            {"type": "update_inputs", "changes": {"use_codegraph": "yes", "gate": "manual", "log_shift_minutes": True}}, run)
        self.assertEqual(inputs["changes"], {"gate": "manual"})
        bug = assistant.validate_action({"type": "create_bug", "title": "Crash", "severity": "urgent", "steps": "one"}, run)
        self.assertEqual((bug["severity"], bug["steps"]), ("medium", ["one"]))
        run.pop("pending_gate")
        self.assertIsNone(assistant.validate_action({"type": "resolve_gate", "names": ["logs_0312.7z.001"]}, run))

    # ------------------------------------------------------------ snapshot
    def test_snapshot_shows_the_run_and_hides_secrets(self):
        mcp.save_tokens({"jira_url": "http://jira.example", "jira_token": "SECRET-TOKEN-123",
                         "collab_url": "http://collab.example", "collab_token": "SECRET-TOKEN-456"})
        text = assistant.snapshot(self.run_, settings.load())
        for needle in ("AST-1", "logs_0312.7z.001", "split 7z volume", "AI pick: near the incident", "WAITING",
                       "210.0 MB", "## Readable folders", "copilot_model"):
            self.assertIn(needle, text)
        self.assertNotIn("SECRET-TOKEN", text)
        self.assertLessEqual(len(text), assistant.SNAPSHOT_LIMIT + 40)

    # ------------------------------------------------------------ ask flow
    def test_ask_answers_from_files_in_its_own_slot(self):
        seen = {}

        def fake_ask(prompt, cwd, model="", **kw):
            seen.update(prompt=prompt, cwd=Path(cwd), model=model, **kw)
            seen["files"] = sorted(p.name for p in Path(cwd).iterdir())
            seen["conversation"] = (Path(cwd) / "conversation.md").read_text(encoding="utf-8")
            kw["on_usage"]({"input_tokens": 1200, "output_tokens": 80, "model_calls": 1})
            return ("Nên tải `logs_0312.7z.001`.\n```actions\n"
                    + json.dumps([{"type": "resolve_gate", "label": "Tải & tiếp tục", "names": ["logs_0312.7z.001"]},
                                  {"type": "format_disk"}]) + "\n```")

        with patch.object(assistant.copilot, "ask", side_effect=fake_ask):
            assistant.ask("Nên tải gì?", self.run_["id"])
            wait_idle()
        self.assertEqual(seen["slot"], "assistant")
        self.assertEqual(seen["files"], ["assistant.md", "context.md", "conversation.md"])
        self.assertIn(str(pipeline.run_dir(self.run_["id"]).resolve()), seen["extra_dirs"])
        self.assertIn("Nên tải gì?", seen["conversation"])
        self.assertLess(len(seen["prompt"]), 1000, "material goes through files, not the command line")
        reply = assistant.state()["messages"][-1]
        self.assertEqual(reply["role"], "assistant")
        self.assertEqual(reply["text"], "Nên tải `logs_0312.7z.001`.")
        self.assertEqual([a["type"] for a in reply["actions"]], ["resolve_gate"], "unknown actions are dropped")
        self.assertEqual(reply["usage"]["input_tokens"], 1200)

    def test_failures_are_shown_and_triggers_fire_once(self):
        with patch.object(assistant.copilot, "ask", side_effect=copilot.CopilotError("Copilot CLI is not installed")):
            first = assistant.ask("", self.run_["id"], "scan", "gate", trigger="gate:x")
            wait_idle()
            self.assertIn("message", first)
            self.assertEqual(assistant.ask("", self.run_["id"], "scan", "gate", trigger="gate:x"), {"skipped": "duplicate"})
        messages = assistant.state()["messages"]
        self.assertIn("evidence gate", messages[0]["request"])
        self.assertIn("not installed", messages[-1]["error"])
        with self.assertRaises(ValueError):
            assistant.ask("   ")

    def test_turning_off_proactive_scans_blocks_delayed_triggers_but_keeps_manual_chat(self):
        with patch.object(assistant.settings, 'load', return_value={**settings.load(), 'assistant_proactive': False}), \
                patch.object(assistant.copilot, 'ask', return_value='Manual reply') as ask:
            self.assertEqual(assistant.ask('', self.run_['id'], 'scan', 'gate', trigger='queued'), {'skipped': 'disabled'})
            ask.assert_not_called()
            assistant.ask('Manual question', self.run_['id'])
            wait_idle()
            ask.assert_called_once()

    def test_busy_skips_proactive_scans_and_stop_cancels(self):
        release = []

        def slow(*a, **kw):
            end = time.time() + 5
            while not release and time.time() < end:
                time.sleep(0.01)
            return "ok"

        with patch.object(assistant.copilot, "ask", side_effect=slow), \
                patch.object(assistant.copilot, "cancel", side_effect=lambda slot: release.append(slot)) as cancel:
            assistant.ask("hello")
            self.assertEqual(assistant.ask("", None, "scan", trigger="t1"), {"skipped": "busy"})
            with self.assertRaises(RuntimeError):
                assistant.ask("again")
            assistant.stop()
            wait_idle()
        cancel.assert_called_with("assistant")
        self.assertEqual(assistant.state()["messages"][-1]["error"], assistant.t("asst.stopped"))
        self.assertNotIn("t1", assistant._chat()["triggers"], "a skipped trigger can fire later")

    # ------------------------------------------------------------ actions
    def _message_with(self, *actions):
        message = {"id": "m1", "role": "assistant", "run_id": self.run_["id"], "text": "", "actions": list(actions)}
        assistant._chat()["messages"].append(message)
        return message

    def test_run_action_executes_once(self):
        action = assistant.validate_action({"type": "resolve_gate", "names": ["logs_0312.7z.001"]}, self.run_)
        self._message_with(action)
        with patch.object(pipeline.executor, "resolve_gate") as resolve:
            assistant.run_action("m1", 0)
        args = resolve.call_args.args
        self.assertEqual(args[:5], (self.run_["id"], ["logs_0312.7z.001"], None, "12:29:00", "12:32:00"),
                         "the focus window is kept when the AI does not change it")
        self.assertEqual(action["status"], "done")
        with self.assertRaises(ValueError):
            assistant.run_action("m1", 0)
        with self.assertRaises(ValueError):
            assistant.run_action("m1", 5)

    def test_timezone_action(self):
        run = self.run_
        self.assertIsNone(assistant.validate_action({"type": "resolve_timezone", "mode": "manual"}, run), "shift required")
        self.assertIsNone(assistant.validate_action({"type": "resolve_timezone", "mode": "guess"}, run))
        bypass = assistant.validate_action({"type": "resolve_timezone", "mode": "bypass"}, run)
        manual = assistant.validate_action({"type": "resolve_timezone", "mode": "manual", "shift_minutes": -540,
                                            "ticket_utc_offset": "UTC+9"}, run)
        self.assertEqual((manual["shift_minutes"], manual["ticket_utc_offset"]), (-540, "+09:00"))
        self._message_with(manual, bypass)
        with patch.object(pipeline.executor, "resolve_timezone") as resolve:
            assistant.run_action("m1", 0)
        resolve.assert_called_once_with(run["id"], "manual", -540, None, None, "+09:00")
        run["pending_tz"] = {"candidates": [], "suggested": {"shift_minutes": 0}}
        self.assertIn("Time zone decision\nWAITING", assistant.snapshot(run, settings.load()))

    def test_failed_action_is_marked_and_raised(self):
        action = assistant.validate_action({"type": "download_attachments", "names": ["screen.mp4"]}, self.run_)
        self._message_with(action)
        with patch.object(pipeline.executor, "fetch_attachments", side_effect=RuntimeError("busy")):
            with self.assertRaises(RuntimeError):
                assistant.run_action("m1", 0)
        self.assertEqual((action["status"], action["result"]), ("error", "busy"))

    def test_bug_ticket_collects_environment_and_deduplicates(self):
        pipeline._log(self.run_, "✗ Jira ticket: invalid literal for int() with base 10: '77 kB'")
        fields = assistant.validate_action({
            "type": "create_bug", "title": "Jira step crashes on '77 kB' sizes", "severity": "high",
            "component": "attachments", "summary": "int() on a human size", "steps": ["Open a ticket", "Run"],
            "expected": "Sizes parsed", "actual": "Crash", "suspected_cause": "app/attachments.py _normalize"}, self.run_)
        self._message_with(fields, dict(fields, status="pending"))
        assistant.run_action("m1", 0)
        bugs = assistant.list_bugs()
        self.assertEqual(len(bugs), 1)
        text = Path(bugs[0]["path"]).read_text(encoding="utf-8")
        for needle in ('title: "Jira step crashes on \'77 kB\' sizes"', "severity: high", "## Steps to reproduce",
                       "1. Open a ticket", "## Environment (collected by the app)", "invalid literal for int()",
                       "app/attachments.py _normalize"):
            self.assertIn(needle, text)
        assistant.run_action("m1", 1)
        self.assertEqual(len(assistant.list_bugs()), 1, "same open title is not written twice")
        self.assertIn(bugs[0]["file"], assistant._chat()["messages"][-1]["actions"][1]["result"])


class CopilotSlotTest(unittest.TestCase):
    def test_slots_are_independent(self):
        class Proc:
            pid = 1
            def __init__(self):
                self.killed = False
            def poll(self):
                return None
            def kill(self):
                self.killed = True

        pipe, chat = Proc(), Proc()
        copilot._slot("pipeline")["proc"], copilot._slot("assistant")["proc"] = pipe, chat
        with patch.object(copilot.subprocess, "run") as taskkill, patch.object(copilot.subprocess, "CREATE_NO_WINDOW", 0, create=True):
            copilot.cancel("pipeline")
        killed = [call.args[0] for call in taskkill.call_args_list]
        self.assertEqual(killed, [["taskkill", "/pid", "1", "/T", "/F"]])
        self.assertIsNot(copilot._slot("pipeline")["lock"], copilot._slot("assistant")["lock"])
        copilot._slot("pipeline")["proc"] = copilot._slot("assistant")["proc"] = None

    def test_unsupported_add_dir_is_dropped_and_retried(self):
        calls = []

        class Done:
            def __init__(self, code):
                self.returncode = code

        def fake_execute(args, cwd, timeout, cancellable, slot="pipeline", on_line=None):
            calls.append((list(args), slot))
            if "--add-dir" in args:
                return Done(1), b"", b"error: unknown option '--add-dir'", False
            return Done(0), b"answer", b"", False

        with patch.object(copilot, "command", return_value=["copilot"]), \
                patch.object(copilot, "_execute", side_effect=fake_execute):
            self.assertEqual(copilot.ask("p", ".", extra_dirs=["a", "b", "a"], slot="assistant"), "answer")
        first, second = calls[0][0], calls[1][0]
        self.assertEqual(first.count("--add-dir"), 2)
        self.assertNotIn("--add-dir", second)
        self.assertEqual({slot for _, slot in calls}, {"assistant"})


if __name__ == "__main__":
    unittest.main()
