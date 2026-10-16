"""End-to-end pipeline test with fake MCP + fake AI (no network, no login).

LogSleuth (Node), CodeGraph and the XHTML repair/validation run for real.
Run:  python -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ta-test-")
os.environ["TA_DATA_DIR"] = TMP
sys.path.insert(0, str(ROOT))

from app import ai, mcp, paths, pipeline, settings  # noqa: E402

REPORT = ('<ac:structured-macro ac:name="info"><ac:rich-text-body><p>BT connect fails with status 133 & retries</p>'
          '</ac:rich-text-body></ac:structured-macro><h2>Summary</h2><p>Connect fails.</p>'
          '<h2>Diagnostic Diagram</h2><ac:structured-macro ac:name="plantuml"><ac:plain-text-body><![CDATA[@startuml\n'
          'participant App\nparticipant BT\nApp -> BT: connect\nBT --> App: status=133\n@enduml]]></ac:plain-text-body>'
          '</ac:structured-macro>')


class FakeWorld:
    def __init__(self):
        self.calls = []
        self.prompts = []
        self.pages = {}

    def mcp_call(self, role, tool, args, timeout=120):
        self.calls.append((role, tool, args))
        if tool == "jira_get_issue":
            return {"content": [{"type": "text", "text": json.dumps({"key": args["issue_key"], "summary": "BT connect fails",
                                                                       "description": "Happened at 12:30 on 10-10."})}]}
        if tool == "confluence_create_page":
            if args["title"] in self.pages:
                return {"isError": True, "content": [{"type": "text", "text": "Error calling tool 'create_page': A page with this title already exists"}]}
            self.pages[args["title"]] = "555"
            return {"content": [{"type": "text", "text": json.dumps({"page": {"id": "555", "title": args["title"], "url": "http://collab/x?pageId=555"}})}]}
        if tool == "confluence_get_page":
            return {"content": [{"type": "text", "text": json.dumps({"metadata": {"id": self.pages.get(args.get("title"), ""), "title": args.get("title")}})}]}
        if tool == "confluence_update_page":
            return {"content": [{"type": "text", "text": json.dumps({"page": {"id": args["page_id"], "title": args["title"]}})}]}
        raise AssertionError(tool)

    def ask(self, cfg, prompt, files, run_dir, session_id=None, small=False):
        names = [Path(f).name for f in files]
        self.prompts.append((prompt, names))
        if "understand.md" in names:
            return ("Copilot said:\n## Summary\nBT connect failure.\n\n```json\n"
                    '{"time_start": "12:29:00", "time_end": "12:31:00", "confidence": "high", '
                    '"reason": "ticket says 12:30", "keywords": ["status=133"], "services": ["NoSuchService"]}\n```')
        if "ticket-analysis.md" in names:
            return "# Analysis\n\nRoot cause: status 133 from stack."
        if "report.md" in names:
            # Copilot CLI style: a sentence, then the fenced JSON string (with a bare & to repair)
            return "Here is the report:\n\n```json\n" + json.dumps(REPORT) + "\n```"
        raise AssertionError(names)


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        paths.ensure_dirs()
        settings.save({"ai_mode": "copilot", "review_before_publish": True})
        cls.world = FakeWorld()
        mcp.call = cls.world.mcp_call
        pipeline.mcp.call = cls.world.mcp_call
        pipeline.collab.mcp.call = cls.world.mcp_call
        pipeline.ai.ask = cls.world.ask
        work = Path(TMP) / "work"
        (work / "src").mkdir(parents=True)
        (work / "src" / "bt.py").write_text(textwrap.dedent('''
            import logging
            log = logging.getLogger("BluetoothService")
            def connect(dev):
                log.error("connect failed status=%d", 133)
                retry(dev)
            def retry(dev):
                log.warning("retry scheduled")
        '''), encoding="utf-8")
        (work / "app.log").write_text(
            "10-10 12:20:00.000  1000  1001 I BluetoothService: boot\n"
            "10-10 12:30:01.200  1000  1001 E BluetoothService: connect failed status=133\n"
            "10-10 12:30:01.300  1000  1001 W BluetoothService: retry scheduled\n"
            "10-10 13:00:00.000  1000  1001 E AudioRouter: crash in mixer\n", encoding="utf-8")
        cls.work = work

    def wait(self):
        for _ in range(600):
            if not pipeline.executor.busy:
                return
            time.sleep(0.1)
        self.fail("pipeline did not finish")

    def test_full_flow(self):
        run = pipeline.create("http://jira/browse/abc-42", [str(self.work / "app.log")], str(self.work / "src"))
        self.assertEqual(run["issue"], "ABC-42")
        pipeline.executor.start(run["id"])
        self.wait()
        run = pipeline.get(run["id"])
        status = {k: v["status"] for k, v in run["steps"].items()}
        errors = {k: v["error"] for k, v in run["steps"].items() if v["error"]}
        self.assertEqual(errors, {})
        self.assertEqual(status, {"jira": "done", "understand": "done", "log": "done", "graph": "done",
                                  "analyze": "done", "format": "done", "collab": "waiting"})
        self.assertEqual(run["focus"]["time_start"], "12:29:00")
        brief = (pipeline.run_dir(run["id"]) / "03-log-brief.md").read_text(encoding="utf-8")
        self.assertIn("status=133", brief)
        self.assertNotIn("crash in mixer", brief, "time window must drop the 13:00 line")
        report = (pipeline.run_dir(run["id"]) / "06-report.xhtml").read_text(encoding="utf-8")
        self.assertIn("&amp; retries", report)
        self.assertTrue((pipeline.run_dir(run["id"]) / "04-brain.html").exists())
        analyze_files = [names for prompt, names in self.world.prompts if "ticket-analysis.md" in names][0]
        self.assertIn("03-log-brief.md", analyze_files)
        self.assertIn("04-graph.simple.json", analyze_files)

        # publish (create), then publish again (duplicate -> update)
        pipeline.executor.start(run["id"], "collab", only=True, publish=True)
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["collab"]["status"], "done", run["steps"]["collab"]["error"])
        self.assertEqual(run["collab"]["action"], "created")
        pipeline.executor.start(run["id"], "collab", only=True, publish=True)
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["collab"]["action"], "updated")
        self.assertEqual(run["collab"]["page_id"], "555")

        # editing the analysis marks format + collab stale
        pipeline.write_file(run["id"], "05-analysis.md", "# Edited")
        self.assertEqual(pipeline.get(run["id"])["steps"]["format"]["status"], "stale")
        self.assertEqual(pipeline.get(run["id"])["steps"]["collab"]["status"], "stale")
        # a hand-edited report is saved in its repaired, valid form
        pipeline.write_file(run["id"], "06-report.xhtml", "<h2>Edited</h2><p>unclosed & raw")
        saved = (pipeline.run_dir(run["id"]) / "06-report.xhtml").read_text(encoding="utf-8")
        self.assertIn("</p>", saved)
        self.assertIn("&amp; raw", saved)
        self.assertIn('ac:name="plantuml"', saved)
        with self.assertRaises(ValueError):
            pipeline.write_file(run["id"], "01-jira.md", "not editable")

    def test_jira_only_skips_optional_steps(self):
        run = pipeline.create("XYZ-7")
        pipeline.executor.start(run["id"])
        self.wait()
        steps = pipeline.get(run["id"])["steps"]
        self.assertEqual([steps[s]["status"] for s in ("understand", "log", "graph")], ["skipped"] * 3)
        self.assertEqual(steps["format"]["status"], "done", steps["format"]["error"])


if __name__ == "__main__":
    unittest.main()
