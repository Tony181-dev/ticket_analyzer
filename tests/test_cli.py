"""`python -m app run ...`: unattended runs with a fake Jira and a fake AI."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("TA_DATA_DIR", tempfile.mkdtemp(prefix="ta-cli-"))
sys.path.insert(0, str(ROOT))

from app import cli, paths, pipeline, settings  # noqa: E402

REPORT = ('<ac:structured-macro ac:name="info"><ac:rich-text-body><p>x</p></ac:rich-text-body></ac:structured-macro>'
          '<h2>Diagnostic Diagram</h2><ac:structured-macro ac:name="plantuml"><ac:plain-text-body><![CDATA[@startuml\n'
          'participant A\nparticipant B\nA -> B: call\n@enduml]]></ac:plain-text-body></ac:structured-macro>')


class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        paths.ensure_dirs()
        cls.saved = (pipeline.mcp.call, pipeline.ai.ask, pipeline.collab.mcp.call)
        cls.pages = {}

        def mcp_call(role, tool, args, timeout=120):
            if tool == "jira_get_issue":
                if args["issue_key"] == "DOWN-1":
                    return {"isError": True, "content": [{"type": "text", "text": "Jira is unreachable"}]}
                return {"content": [{"type": "text", "text": json.dumps({"key": args["issue_key"], "summary": "CLI ticket"})}]}
            if tool == "confluence_create_page":
                cls.pages[args["title"]] = args["content"]
                return {"content": [{"type": "text", "text": json.dumps({"page": {"id": "77", "title": args["title"],
                                                                                    "url": "http://collab/77"}})}]}
            raise AssertionError(tool)

        def ask(cfg, prompt, files, run_dir, session_id=None, small=False, effort=None, context=None, on_usage=None):
            names = [Path(f).name for f in files]
            if "understand.md" in names:
                return '```json\n{"time_start": null, "time_end": null}\n```'
            if "ticket-analysis.md" in names:
                return "# Analysis\n\nDone."
            if "report.md" in names:
                return "```json\n" + json.dumps(REPORT) + "\n```"
            raise AssertionError(names)

        pipeline.mcp.call = pipeline.collab.mcp.call = mcp_call
        pipeline.ai.ask = ask
        settings.save({"ai_mode": "copilot", "review_before_publish": True})
        cls.log = Path(tempfile.mkdtemp(prefix="ta-cli-log-")) / "app.log"
        cls.log.write_text("10-10 12:30:01.200  1000  1001 E Bt: connect failed status=133\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        pipeline.mcp.call, pipeline.ai.ask, pipeline.collab.mcp.call = cls.saved

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_json_summary_and_until(self):
        code, out, err = self.run_cli("http://jira/browse/cli-1", "--log", str(self.log), "--no-infographic",
                                      "--until", "analyze", "--json")
        self.assertEqual(code, 0, err)
        summary = json.loads(out)
        self.assertEqual(summary["issue"], "CLI-1")
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["steps"]["analyze"], "done")
        self.assertEqual(summary["steps"]["format"], "idle", "--until stops after the named step")
        self.assertTrue(Path(summary["files"]["05-analysis.md"]).is_file())
        self.assertGreater(summary["tokens"]["total_tokens"], 0)
        self.assertIn("✓", err, "progress goes to stderr so stdout stays pure JSON")
        run = pipeline.get(summary["run"])
        self.assertEqual(run["inputs"]["gate"], "skip", "the command line never waits for a person")

    def test_publish_and_several_tickets(self):
        code, out, _ = self.run_cli("CLI-2", "CLI-3", "--no-infographic", "--publish", "--quiet")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(self.pages), ["CLI-2 - CLI ticket", "CLI-3 - CLI ticket"])
        self.assertIn("http://collab/77", out)
        self.assertEqual(out.count("published"), 2)

    def test_default_stops_with_the_report_ready(self):
        code, out, _ = self.run_cli("CLI-4", "--no-infographic", "--json", "--quiet")
        summary = json.loads(out)
        self.assertEqual(code, 0)
        self.assertFalse(summary["published"])
        self.assertEqual(summary["steps"]["collab"], "waiting")
        self.assertTrue(Path(summary["files"]["06-report.xhtml"]).is_file())

    def test_failures_set_the_exit_code(self):
        code, out, _ = self.run_cli("DOWN-1", "--json", "--quiet")
        self.assertEqual(code, 1)
        summary = json.loads(out)
        self.assertFalse(summary["ok"])
        self.assertIn("Jira is unreachable", summary["errors"]["jira"])
        code, _, err = self.run_cli("not a ticket", "--quiet")
        self.assertEqual(code, 1)
        self.assertIn("not a ticket", err)
        code, _, err = self.run_cli("CLI-5", "--log", str(self.log.with_name("missing.log")), "--quiet")
        self.assertEqual(code, 1)
        self.assertIn("missing.log", err)


if __name__ == "__main__":
    unittest.main()
