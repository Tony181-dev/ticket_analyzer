"""End-to-end pipeline test with fake MCP + fake AI (no network, no login).

LogSleuth (Node), CodeGraph and the XHTML repair/validation run for real.
Run:  python -m unittest discover -s tests
"""
import base64
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.sessions = []
        self.analysis_options = []
        self.pages = {}
        self.page_bodies = {}
        self.uploads = {}
        self.fail_upload = False
        self.fail_infographic = False
        self.next_page_id = 555

    def mcp_call(self, role, tool, args, timeout=120):
        self.calls.append((role, tool, args))
        if tool == "jira_get_issue":
            return {"content": [{"type": "text", "text": json.dumps({"key": args["issue_key"], "summary": "BT connect fails",
                                                                       "description": "Happened at 12:30 on 10-10."})}]}
        if tool == "confluence_create_page":
            if args["title"] in self.pages:
                return {"isError": True, "content": [{"type": "text", "text": "Error calling tool 'create_page': A page with this title already exists"}]}
            page_id = str(self.next_page_id)
            self.next_page_id += 1
            self.pages[args["title"]] = page_id
            self.page_bodies[args["title"]] = args["content"]
            return {"content": [{"type": "text", "text": json.dumps({"page": {"id": page_id, "title": args["title"], "url": f"http://collab/x?pageId={page_id}"}})}]}
        if tool == "confluence_get_page":
            return {"content": [{"type": "text", "text": json.dumps({"metadata": {"id": self.pages.get(args.get("title"), ""), "title": args.get("title")}})}]}
        if tool == "confluence_update_page":
            self.page_bodies[args["title"]] = args["content"]
            return {"content": [{"type": "text", "text": json.dumps({"page": {"id": args["page_id"], "title": args["title"]}})}]}
        if tool == "confluence_upload_attachment":
            if self.fail_upload:
                return {"isError": False, "content": [{"type": "text", "text": json.dumps({
                    "attachment": {"success": False, "error": "attachment upload failed"}})}]}
            name = args["filename"]
            entry = {"id": str(9000 + len(self.uploads)), "title": name,
                     "extensions": {"fileSize": len(base64.b64decode(args["content_base64"]))}}
            self.uploads[(args["content_id"], name)] = entry
            return {"content": [{"type": "text", "text": json.dumps({"attachment": entry})}]}
        if tool == "confluence_get_attachments":
            entry = self.uploads.get((args["content_id"], args["filename"]))
            return {"content": [{"type": "text", "text": json.dumps({"attachments": [entry] if entry else []})}]}
        if tool == "confluence_delete_page":
            for title, page_id in list(self.pages.items()):
                if page_id == args["page_id"]:
                    self.pages.pop(title, None)
                    self.page_bodies.pop(title, None)
            return {"content": [{"type": "text", "text": "{}"}]}
        raise AssertionError(tool)

    def ask(self, cfg, prompt, files, run_dir, session_id=None, small=False, effort=None, context=None, on_usage=None):
        names = [Path(f).name for f in files]
        self.prompts.append((prompt, names))
        self.sessions.append(session_id)
        if "infographic.md" in names:
            if self.fail_infographic:
                return "This is not valid infographic JSON"
            return json.dumps({
                "title": "BT connect failure",
                "summary": "Bluetooth connection fails and the service schedules a retry.",
                "root_cause": "Status 133 is reported by the connection attempt.",
                "timeline": [{"time": "12:30", "event": "Connection failure is recorded."}],
                "evidence": ["Log reports connect failed status=133."],
                "actions": ["Review the connection failure path."],
            }, ensure_ascii=False)
        if "understand.md" in names:
            return ("Copilot said:\n## Summary\nBT connect failure.\n\n```json\n"
                    '{"time_start": "12:29:00", "time_end": "12:31:00", "confidence": "high", '
                    '"reason": "ticket says 12:30", "keywords": ["status=133"], "services": ["NoSuchService"]}\n```')
        if "ticket-analysis.md" in names:
            self.analysis_options.append((effort, context))
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
        settings.save({"ai_mode": "copilot", "review_before_publish": False})
        run = pipeline.create("http://jira/browse/abc-42", [str(self.work / "app.log")], str(self.work / "src"),
                              auto_publish_to_collab=False)
        self.assertEqual(run["issue"], "ABC-42")
        self.assertFalse(run["inputs"]["auto_publish_to_collab"])
        pipeline.executor.start(run["id"])
        self.wait()
        run = pipeline.get(run["id"])
        status = {k: v["status"] for k, v in run["steps"].items()}
        errors = {k: v["error"] for k, v in run["steps"].items() if v["error"]}
        self.assertEqual(errors, {})
        self.assertEqual(status, {"jira": "done", "understand": "done", "log": "done", "graph": "done",
                                  "analyze": "done", "format": "done", "infographic": "done", "collab": "waiting"})
        self.assertEqual(run["focus"]["time_start"], "12:29:00")
        understand_files = [names for prompt, names in self.world.prompts if "understand.md" in names]
        self.assertEqual(understand_files[-1], ["01-jira.md", "understand.md"],
                 "Step 2 must read the ticket only, not log spans or log files")
        self.assertFalse((pipeline.run_dir(run["id"]) / "02-log-span.md").exists())
        self.assertTrue((pipeline.run_dir(run["id"]) / "03-catalog.md").exists(),
                "LogSleuth must use the selected source folder's catalog")
        brief = (pipeline.run_dir(run["id"]) / "03-log-brief.md").read_text(encoding="utf-8")
        self.assertIn("status=133", brief)
        self.assertNotIn("crash in mixer", brief, "time window must drop the 13:00 line")
        report = (pipeline.run_dir(run["id"]) / "06-report.xhtml").read_text(encoding="utf-8")
        self.assertIn("&amp; retries", report)
        self.assertTrue((pipeline.run_dir(run["id"]) / "04-brain.html").exists())
        analyze_files = [names for prompt, names in self.world.prompts if "ticket-analysis.md" in names][-1]
        self.assertIn("03-log-brief.md", analyze_files)
        self.assertIn("04-graph.simple.json", analyze_files)
        self.assertIn("05-source-evidence.md", analyze_files)
        self.assertIn('log.error("connect failed status=%d", 133)',
                  (pipeline.run_dir(run["id"]) / "05-source-evidence.md").read_text(encoding="utf-8"))
        self.assertEqual(self.world.analysis_options[-1], ("high", "long_context"))

        # publish (create), then publish again (duplicate -> update)
        pipeline.executor.start(run["id"], "collab", only=True, publish=True)
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["collab"]["status"], "done", run["steps"]["collab"]["error"])
        self.assertEqual(run["collab"]["action"], "created")
        created_id = run["collab"]["page_id"]
        self.assertIn('ri:filename="06-infographic.svg"', self.world.page_bodies[run["collab"]["title"]])
        pipeline.executor.start(run["id"], "collab", only=True, publish=True)
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["collab"]["action"], "updated")
        self.assertEqual(run["collab"]["page_id"], created_id, "the existing page is updated, not duplicated")

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

    def test_rerunning_analysis_rebuilds_stale_prerequisites(self):
        run = pipeline.create("STALE-1", [str(self.work / "app.log")], str(self.work / "src"))
        pipeline.executor.start(run["id"])
        self.wait()

        fresh_source = self.work / "fresh-src"
        fresh_source.mkdir()
        (fresh_source / "bt.py").write_text(
            'import logging\nlog = logging.getLogger("BluetoothService")\n'
            'def connect():\n    log.error("fresh evidence failure")\n', encoding="utf-8")
        fresh_log = self.work / "fresh.log"
        fresh_log.write_text(
            "10-10 12:30:01.200  1000  1001 E BluetoothService: fresh evidence failure\n",
            encoding="utf-8")
        pipeline.update_inputs(run["id"], log_files=[str(fresh_log)], source_dir=str(fresh_source))
        stale = pipeline.get(run["id"])
        self.assertEqual(stale["steps"]["log"]["status"], "stale")
        self.assertEqual(stale["steps"]["graph"]["status"], "stale")

        pipeline.executor.start(run["id"], "analyze", only=True)
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["log"]["status"], "done")
        self.assertEqual(run["steps"]["graph"]["status"], "done")
        self.assertEqual(run["steps"]["analyze"]["status"], "done")
        self.assertEqual(run["steps"]["format"]["status"], "stale")
        brief = (pipeline.run_dir(run["id"]) / "03-log-brief.md").read_text(encoding="utf-8")
        evidence = (pipeline.run_dir(run["id"]) / "05-source-evidence.md").read_text(encoding="utf-8")
        self.assertIn("fresh evidence failure", brief)
        self.assertIn('log.error("fresh evidence failure")', evidence)

    def test_source_caches_refresh_after_source_changes(self):
        source = self.work / "cache-src"
        source.mkdir()
        source_file = source / "service.py"
        source_file.write_text(
            'import logging\nlog = logging.getLogger("CacheService")\n'
            'def first():\n    log.error("original cache marker")\n', encoding="utf-8")

        catalog = pipeline.logsleuth.catalog_for(source)
        original_catalog = catalog.read_text(encoding="utf-8")
        graph_dir, original_meta = pipeline.codegraph.build(source, token_budget=2000)

        source_file.write_text(
            'import logging\nlog = logging.getLogger("CacheService")\n'
            'def first():\n    log.error("fresh cache marker")\n'
            'def added():\n    return True\n', encoding="utf-8")
        stat = source_file.stat()
        os.utime(source_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))

        refreshed_catalog = pipeline.logsleuth.catalog_for(source)
        refreshed_graph_dir, refreshed_meta = pipeline.codegraph.build(source, token_budget=2000)
        self.assertEqual(catalog, refreshed_catalog)
        self.assertIn("original cache marker", original_catalog)
        self.assertIn("fresh cache marker", refreshed_catalog.read_text(encoding="utf-8"))
        self.assertEqual(graph_dir, refreshed_graph_dir)
        self.assertGreater(refreshed_meta["nodes"], original_meta["nodes"])

        pipeline.codegraph.build(source, token_budget=1500)
        simple = json.loads((graph_dir / "graph.simple.json").read_text(encoding="utf-8"))
        self.assertEqual(simple["meta"]["token_budget"], 1500)

    def test_missing_ticket_time_uses_unfiltered_logs(self):
        original_ask = pipeline.ai.ask

        def no_time_ask(cfg, prompt, files, run_dir, session_id=None, small=False, effort=None, context=None, on_usage=None):
            names = [Path(f).name for f in files]
            if "understand.md" in names:
                self.world.prompts.append((prompt, names))
                self.world.sessions.append(session_id)
                return ('No incident time is stated in the ticket.\n```json\n'
                        '{"time_start": null, "time_end": null, "confidence": "none", '
                        '"reason": "The ticket does not state an incident time."}\n```')
            return self.world.ask(cfg, prompt, files, run_dir, session_id=session_id, small=small,
                                  effort=effort, context=context, on_usage=on_usage)

        pipeline.ai.ask = no_time_ask
        try:
            run = pipeline.create("NOTIME-1", [str(self.work / "app.log")], str(self.work / "src"), use_codegraph=False)
            pipeline.executor.start(run["id"])
            self.wait()
        finally:
            pipeline.ai.ask = original_ask

        run = pipeline.get(run["id"])
        self.assertIsNone(run["focus"]["time_start"])
        self.assertIsNone(run["focus"]["time_end"])
        self.assertEqual(run["focus"]["filter_mode"], "full")
        brief = (pipeline.run_dir(run["id"]) / "03-log-brief.md").read_text(encoding="utf-8")
        self.assertNotIn("**Time window**", brief)
        self.assertIn("4 lines read", brief, "without a ticket time the full log time range is passed to LogSleuth")

    def test_log_brief_falls_back_without_source_and_codegraph_can_be_disabled(self):
        run = pipeline.create("NOCATALOG-1", [str(self.work / "app.log")], use_codegraph=False)
        self.assertFalse(run["inputs"]["use_codegraph"])
        pipeline.executor.start(run["id"])
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["log"]["status"], "done")
        self.assertIn("từ khóa", run["steps"]["log"]["note"].lower())
        self.assertEqual(run["steps"]["graph"]["status"], "skipped")
        self.assertIn("code graph", run["steps"]["graph"]["note"].lower())
        self.assertIn("status=133", (pipeline.run_dir(run["id"]) / "03-log-brief.md").read_text(encoding="utf-8"))
        self.assertFalse((pipeline.run_dir(run["id"]) / "03-catalog.md").exists())
        self.assertFalse((pipeline.run_dir(run["id"]) / "04-graph.simple.json").exists())

        graph_run = pipeline.create("NOGRAPH-1", [str(self.work / "app.log")], str(self.work / "src"),
                                    use_codegraph=False)
        pipeline.executor.start(graph_run["id"])
        self.wait()
        graph_run = pipeline.get(graph_run["id"])
        self.assertEqual(graph_run["steps"]["log"]["status"], "done")
        self.assertTrue((pipeline.run_dir(graph_run["id"]) / "03-catalog.md").exists())
        self.assertEqual(graph_run["steps"]["graph"]["status"], "skipped")
        self.assertFalse((pipeline.run_dir(graph_run["id"]) / "04-graph.simple.json").exists())
        graph_run = pipeline.update_inputs(graph_run["id"], use_codegraph=True)
        self.assertEqual(graph_run["steps"]["graph"]["status"], "idle",
                 "enabling Code Graph after a skipped run should make the step runnable")
        self.assertEqual(graph_run["steps"]["analyze"]["status"], "stale")

    def test_auto_uses_defaults_while_manual_honors_tuning(self):
        custom = {"mode": "auto", "budget_tokens": 4000, "before": 0, "after": 0,
                  "window_ms": 0, "services": ["WrongService"], "time_enabled": True,
                  "time_start": "03:00:00", "time_end": "03:01:00"}
        run = pipeline.create("AUTO-DEFAULTS-1", logsleuth_options=custom)
        auto = pipeline._log_options(run, 12000)
        self.assertEqual(auto["mode"], "auto")
        self.assertEqual(auto["budget_tokens"], 12000)
        self.assertEqual((auto["before"], auto["after"], auto["window_ms"]), (5, 12, 300))
        self.assertEqual(auto["services"], [])
        self.assertFalse(auto["time_enabled"])

        manual_run = {"inputs": {"logsleuth": {**custom, "mode": "manual"}}}
        manual = pipeline._log_options(manual_run, 12000)
        self.assertEqual(manual["mode"], "manual")
        self.assertEqual(manual["budget_tokens"], 4000)
        self.assertEqual((manual["before"], manual["after"], manual["window_ms"]), (0, 0, 0))
        self.assertEqual(manual["services"], ["WrongService"])
        self.assertTrue(manual["time_enabled"])

    def test_auto_publish_preference_is_saved_per_run(self):
        settings.save({"ai_mode": "copilot", "review_before_publish": True})
        run = pipeline.create("AUTOPUBLISH-INPUT-1")
        self.assertFalse(run["inputs"]["auto_publish_to_collab"])

        run = pipeline.update_inputs(run["id"], auto_publish_to_collab=True)
        self.assertTrue(run["inputs"]["auto_publish_to_collab"])

        settings.save({"review_before_publish": False})
        manual_run = pipeline.create("MANUAL-PUBLISH-1", auto_publish_to_collab=False)
        self.assertFalse(manual_run["inputs"]["auto_publish_to_collab"],
                         "an explicit unchecked run option overrides the global default")

    def test_m365_single_session_and_infographic(self):
        settings.save({"ai_mode": "m365", "review_before_publish": True})
        calls = self.world.calls
        start = len(calls)
        self.world.sessions.clear()

        def fake_image(cfg, prompt, files, image_dir, session_id=None):
            Path(image_dir).mkdir(parents=True, exist_ok=True)
            image = Path(image_dir) / "infographic-1.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
            self.world.sessions.append(session_id)
            return [str(image)]
        original = pipeline.ai.ask_image
        pipeline.ai.ask_image = fake_image
        try:
            run = pipeline.create("INF-1", [str(self.work / "app.log")], auto_publish_to_collab=True)
            pipeline.executor.start(run["id"])
            self.wait()
        finally:
            pipeline.ai.ask_image = original
            settings.save({"ai_mode": "copilot", "review_before_publish": True})
        run = pipeline.get(run["id"])
        errors = {k: v["error"] for k, v in run["steps"].items() if v["error"]}
        self.assertEqual(errors, {})
        self.assertEqual(run["steps"]["infographic"]["status"], "done")
        self.assertEqual(run["steps"]["collab"]["status"], "done")
        self.assertEqual(set(self.world.sessions), {run["session_id"]}, "every AI step must reuse one session")
        self.assertEqual(len(self.world.sessions), 4)
        self.assertTrue((pipeline.run_dir(run["id"]) / "06-infographic.png").exists())
        new = calls[start:]
        page = next(a for _, tool, a in new if tool == "confluence_update_page")
        self.assertIn('ri:filename="06-infographic.png"', page["content"])
        self.assertTrue(any(tool == "confluence_upload_attachment" and a["content_id"] == run["collab"]["page_id"]
                    for _, tool, a in new))

        # a later step keeps the conversation; running the whole workflow again opens a new one
        first_session = run["session_id"]
        pipeline.executor.start(run["id"], "analyze", only=True)
        self.wait()
        self.assertEqual(pipeline.get(run["id"])["session_id"], first_session)
        pipeline.executor.start(run["id"], until="understand")
        self.wait()
        self.assertNotEqual(pipeline.get(run["id"])["session_id"], first_session, "a full run starts a new M365 chat")
        self.assertEqual(self.world.sessions[-1], pipeline.get(run["id"])["session_id"])

    def test_infographic_toggle_skips_step_but_publishes_report(self):
        settings.save({"ai_mode": "copilot", "review_before_publish": True})
        run = pipeline.create("NOINFO-1", [str(self.work / "app.log")], str(self.work / "src"), use_infographic=False)
        pipeline.executor.start(run["id"])
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["infographic"]["status"], "skipped")
        self.assertEqual(run["steps"]["collab"]["status"], "waiting")

        start = len(self.world.calls)
        pipeline.executor.start(run["id"], "collab", only=True, publish=True)
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["collab"]["status"], "done", run["steps"]["collab"]["error"])
        calls = self.world.calls[start:]
        self.assertFalse(any(tool == "confluence_upload_attachment" for _, tool, _ in calls))
        self.assertNotIn('ri:filename="06-infographic', self.world.page_bodies[run["collab"]["title"]])

        pipeline.update_inputs(run["id"], use_infographic=True)
        self.assertEqual(pipeline.get(run["id"])["steps"]["infographic"]["status"], "stale")
        pipeline.executor.start(run["id"], "collab", only=True, publish=True)
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["infographic"]["status"], "done")
        self.assertEqual(run["steps"]["collab"]["status"], "done")
        self.assertIn('ri:filename="06-infographic.svg"', self.world.page_bodies[run["collab"]["title"]])

    def test_infographic_failure_does_not_block_publishing_in_skip_mode(self):
        settings.save({"ai_mode": "copilot", "review_before_publish": False})
        self.world.fail_infographic = True
        try:
            run = pipeline.create("INFOFAIL-1", [str(self.work / "app.log")], str(self.work / "src"))
            pipeline.executor.start(run["id"])
            self.wait()
        finally:
            self.world.fail_infographic = False
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["infographic"]["status"], "error")
        self.assertEqual(run["steps"]["collab"]["status"], "done", "the report is published; the image is optional")
        self.assertFalse(run["collab"]["infographic"])
        body = self.world.page_bodies[f"{run['issue']} - BT connect fails"]
        self.assertNotIn("06-infographic", body)

        # once the image exists, publishing again adds it to the same page
        pipeline.executor.start(run["id"], "infographic")
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["infographic"]["status"], "done", run["steps"]["infographic"]["error"])
        self.assertEqual(run["collab"]["action"], "updated")
        self.assertTrue(run["collab"]["infographic"])
        self.assertIn('ri:filename="06-infographic.svg"', self.world.page_bodies[run["collab"]["title"]])

    def test_infographic_failure_stops_for_a_decision_outside_skip_mode(self):
        settings.save({"ai_mode": "copilot", "review_before_publish": False})
        self.world.fail_infographic = True
        try:
            run = pipeline.create("INFOFAIL-2", gate="review")
            self.assertFalse(run["inputs"]["continue_on_optional_error"])
            pipeline.executor.start(run["id"])
            self.wait()
        finally:
            self.world.fail_infographic = False
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["infographic"]["status"], "error")
        self.assertEqual(run["steps"]["collab"]["status"], "idle")
        self.assertEqual(pipeline.resume_step(run), "infographic", "Continue retries the step that stopped the run")

        pipeline.executor.start(run["id"], "collab", publish=True)  # the user's choice: publish without it
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["collab"]["status"], "done", run["steps"]["collab"]["error"])
        self.assertIn("infographic", run["steps"]["collab"]["note"])

    def test_infographic_upload_failure_publishes_the_report_without_the_image(self):
        settings.save({"ai_mode": "copilot", "review_before_publish": False})
        self.world.fail_upload = True
        try:
            run = pipeline.create("INFOUPLOAD-1", [str(self.work / "app.log")], str(self.work / "src"),
                                  auto_publish_to_collab=True)
            pipeline.executor.start(run["id"])
            self.wait()
        finally:
            self.world.fail_upload = False
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["infographic"]["status"], "done")
        self.assertEqual(run["steps"]["collab"]["status"], "done", run["steps"]["collab"]["error"])
        self.assertFalse(run["collab"]["infographic"])
        self.assertNotIn("06-infographic", self.world.page_bodies[run["collab"]["title"]],
                         "a page must never reference an image that was not uploaded")

    def test_m365_image_failure_falls_back_to_the_local_svg(self):
        settings.save({"ai_mode": "m365", "review_before_publish": True})

        def no_image(cfg, prompt, files, image_dir, session_id=None):
            raise RuntimeError("image generation limit reached")
        original = pipeline.ai.ask_image
        pipeline.ai.ask_image = no_image
        try:
            run = pipeline.create("INFOFALLBACK-1")
            pipeline.executor.start(run["id"])
            self.wait()
        finally:
            pipeline.ai.ask_image = original
            settings.save({"ai_mode": "copilot"})
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["infographic"]["status"], "done", run["steps"]["infographic"]["error"])
        self.assertIn("SVG", run["steps"]["infographic"]["note"])
        self.assertTrue((pipeline.run_dir(run["id"]) / "06-infographic.svg").exists())
        self.assertIn("image generation limit reached", (pipeline.run_dir(run["id"]) / "run.log").read_text(encoding="utf-8"))

    def test_resume_and_until_run_exactly_the_requested_range(self):
        settings.save({"ai_mode": "copilot", "review_before_publish": True})
        run = pipeline.create("RANGE-1", [str(self.work / "app.log")], str(self.work / "src"), use_infographic=False)
        pipeline.executor.start(run["id"], until="analyze")
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["analyze"]["status"], "done")
        self.assertEqual(run["steps"]["format"]["status"], "idle", "nothing after the requested last step runs")
        self.assertEqual(pipeline.resume_step(run), "format")

        calls = len(self.world.prompts)
        pipeline.executor.start(run["id"], resume=True)
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["format"]["status"], "done")
        self.assertEqual(run["steps"]["collab"]["status"], "waiting")
        self.assertEqual(len(self.world.prompts), calls + 1, "Continue does not redo the finished steps")
        self.assertEqual(pipeline.resume_step(run), "collab")
        with self.assertRaises(ValueError):
            pipeline.executor.start(run["id"], "format", until="jira")

    def test_each_pass_keeps_its_own_token_record(self):
        settings.save({"ai_mode": "copilot", "review_before_publish": True})
        run = pipeline.create("PASSES-1", use_infographic=False)
        pipeline.executor.start(run["id"])
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(len(run["usage_passes"]), 1)
        first = run["usage_passes"][0]
        self.assertEqual((first["n"], first["from"], first["model_calls"]), (1, "jira", 3))
        self.assertTrue(first["estimated"], "no usage events from the backend: the tokens are an estimate")
        self.assertGreater(first["total_tokens"], 0)
        self.assertEqual(set(first["by_step"]), {"understand", "analyze", "format"})
        self.assertIsNotNone(first["ended"])

        pipeline.executor.start(run["id"], "analyze")  # running again adds a pass instead of overwriting
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual([record["n"] for record in run["usage_passes"]], [1, 2])
        self.assertEqual(run["usage_passes"][1]["from"], "analyze")
        total = run["usage_total"]
        self.assertEqual(total["passes"], 2)
        self.assertEqual(total["total_tokens"], sum(record["total_tokens"] for record in run["usage_passes"]))
        self.assertEqual(total["model_calls"], 5)

        pipeline.executor.start(run["id"], "collab", only=True, publish=True)  # no AI call: not a token pass
        self.wait()
        self.assertEqual(len(pipeline.get(run["id"])["usage_passes"]), 2)

    def test_ticket_time_in_a_stated_zone_is_shifted_to_a_utc_log(self):
        utc_log = self.work / "utc.log"
        utc_log.write_text(
            "10-10 05:20:00.000  1000  1001 I BluetoothService: boot\n"
            "10-10 05:30:01.200  1000  1001 E BluetoothService: connect failed status=133\n"
            "10-10 05:45:00.000  1000  1001 E AudioRouter: crash in mixer\n", encoding="utf-8")
        original_ask = pipeline.ai.ask

        def kst_ask(cfg, prompt, files, run_dir, session_id=None, small=False, effort=None, context=None, on_usage=None):
            names = [Path(f).name for f in files]
            if "understand.md" in names:
                return ('The ticket says 14:30 KST.\n```json\n{"time_start": "14:29:00", "time_end": "14:31:00", '
                        '"ticket_utc_offset": "+09:00", "incident_date": "2024-10-10", "confidence": "high", '
                        '"reason": "14:30 KST", "keywords": ["status=133"]}\n```')
            return self.world.ask(cfg, prompt, files, run_dir, session_id=session_id, small=small,
                                  effort=effort, context=context, on_usage=on_usage)

        pipeline.ai.ask = kst_ask
        try:
            run = pipeline.create("TZ-1", [str(utc_log)], str(self.work / "src"), use_codegraph=False, use_infographic=False)
            pipeline.executor.start(run["id"], until="log")
            self.wait()
        finally:
            pipeline.ai.ask = original_ask
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["log"]["status"], "done", run["steps"]["log"]["error"])
        self.assertEqual(run["focus"]["ticket_utc_offset"], "+09:00")
        alignment = run["log_stats"]["timeAlignment"]
        self.assertEqual((alignment["shift_minutes"], alignment["source"]), (-540, "log-utc"))
        self.assertEqual((alignment["time_start"], alignment["time_end"]), ("05:29:00", "05:31:00"))
        self.assertFalse(run["log_stats"]["fullTimeFallback"])
        brief = (pipeline.run_dir(run["id"]) / "03-log-brief.md").read_text(encoding="utf-8")
        self.assertIn("status=133", brief)
        self.assertNotIn("crash in mixer", brief, "the shifted window still filters")
        self.assertIn("-9h", run["steps"]["log"]["note"])
        self.assertIn("| -540 |", (pipeline.run_dir(run["id"]) / "03-log-span.md").read_text(encoding="utf-8"))

        # a shift typed by the user wins over the detection and re-slices
        pipeline.update_inputs(run["id"], log_shift_minutes=-525)
        self.assertEqual(pipeline.get(run["id"])["steps"]["log"]["status"], "stale")
        pipeline.executor.start(run["id"], "log", only=True)
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["log_stats"]["timeAlignment"]["source"], "manual")
        self.assertEqual(run["log_stats"]["timeAlignment"]["time_start"], "05:44:00")
        self.assertEqual(run["log_stats"]["timeWindow"]["start"], "05:44:00")
        self.assertEqual(run["log_stats"]["afterTime"], 1, "only the 05:45 line is inside the hand-set window")
        with self.assertRaises(ValueError):
            pipeline.update_inputs(run["id"], log_shift_minutes=99999)

    def _zoneless(self, issue, gate):
        """Run steps 1-3 for a ticket that says '14:30' without a zone, against a log printing UTC."""
        utc_log = self.work / f"{issue}.log"
        utc_log.write_text(
            "10-10 05:20:00.000  1000  1001 I BluetoothService: boot\n"
            "10-10 05:30:01.200  1000  1001 E BluetoothService: connect failed status=133\n"
            "10-10 05:45:00.000  1000  1001 E AudioRouter: crash in mixer\n", encoding="utf-8")
        original_ask = pipeline.ai.ask

        def zoneless_ask(cfg, prompt, files, run_dir, session_id=None, small=False, effort=None, context=None, on_usage=None):
            if "understand.md" in [Path(f).name for f in files]:
                return ('```json\n{"time_start": "14:29:00", "time_end": "14:31:00", "ticket_utc_offset": null, '
                        '"confidence": "medium", "reason": "says 14:30", "keywords": ["status=133"]}\n```')
            if "log-time.md" in [Path(f).name for f in files]:
                return '```json\n{"shift_minutes": null, "confidence": "none", "reason": "unclear"}\n```'
            return self.world.ask(cfg, prompt, files, run_dir, session_id=session_id, small=small,
                                  effort=effort, context=context, on_usage=on_usage)

        pipeline.ai.ask = zoneless_ask
        self.addCleanup(setattr, pipeline.ai, "ask", original_ask)
        run = pipeline.create(issue, [str(utc_log)], str(self.work / "src"), use_codegraph=False,
                              use_infographic=False, gate=gate)
        pipeline.executor.start(run["id"], until="log")
        self.wait()
        run = pipeline.get(run["id"])
        if gate != "skip":
            self.assertTrue(run.get("pending_gate"), "the evidence gate comes first")
            pipeline.executor.resolve_gate(run["id"], [], None, "14:29:00", "14:31:00")
            self.wait()
        return pipeline.get(run["id"])

    def test_unknown_ticket_zone_is_asked_in_review_mode(self):
        run = self._zoneless("TZ-ASK-1", "review")
        self.assertEqual(run["steps"]["understand"]["status"], "done")
        self.assertIn("⚠", run["steps"]["understand"]["note"])
        self.assertEqual(run["steps"]["log"]["status"], "waiting")
        pending = run["pending_tz"]
        self.assertEqual((pending["ticket_start"], pending["ticket_end"]), ("14:29:00", "14:31:00"))
        self.assertEqual(pending["spans"][0]["first"][:5], "05:20")
        self.assertTrue(pending["candidates"])
        self.assertFalse((pipeline.run_dir(run["id"]) / "03-log-brief.md").exists(), "nothing is sliced before the choice")

        # drop the time filter: the whole log is used
        pipeline.executor.resolve_timezone(run["id"], "bypass")
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["log"]["status"], "done", run["steps"]["log"]["error"])
        self.assertNotIn("pending_tz", run)
        self.assertIsNone(run["log_stats"]["timeWindow"])
        self.assertIsNone(run["log_stats"]["timeAlignment"])

        # a shift set by the user (ticket UTC+9, log UTC) re-slices to 05:29-05:31
        pipeline.executor.resolve_timezone(run["id"], "manual", -540, ticket_utc_offset="+09:00")
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["log_stats"]["timeAlignment"]["source"], "manual")
        self.assertEqual(run["log_stats"]["timeWindow"]["start"], "05:29:00")
        self.assertEqual(run["focus"]["ticket_utc_offset"], "+09:00")
        brief = (pipeline.run_dir(run["id"]) / "03-log-brief.md").read_text(encoding="utf-8")
        self.assertIn("status=133", brief)
        self.assertNotIn("crash in mixer", brief)
        with self.assertRaises(ValueError):
            pipeline.executor.resolve_timezone(run["id"], "manual")
        with self.assertRaises(ValueError):
            pipeline.executor.resolve_timezone(run["id"], "guess")

    def test_unknown_ticket_zone_is_aligned_automatically_in_skip_mode(self):
        run = self._zoneless("TZ-SKIP-1", "skip")
        self.assertEqual(run["steps"]["log"]["status"], "done", run["steps"]["log"]["error"])
        self.assertNotIn("pending_tz", run)
        log = (pipeline.run_dir(run["id"]) / "run.log").read_text(encoding="utf-8")
        self.assertIn("⚠", log)
        self.assertIsNotNone(run["log_stats"]["timeAlignment"])

    def test_ticket_keywords_seed_the_brief_when_there_is_no_source_and_no_time(self):
        quiet_log = self.work / "quiet.log"
        quiet_log.write_text(
            "10-10 12:20:00.000  1000  1001 I BluetoothService: boot\n"
            "10-10 12:30:01.200  1000  1001 I BluetoothService: link dropped reason=0x13\n", encoding="utf-8")
        original_ask = pipeline.ai.ask

        def keyword_ask(cfg, prompt, files, run_dir, session_id=None, small=False, effort=None, context=None, on_usage=None):
            names = [Path(f).name for f in files]
            if "understand.md" in names:
                return '```json\n{"time_start": null, "time_end": null, "keywords": ["reason=0x13"]}\n```'
            return self.world.ask(cfg, prompt, files, run_dir, session_id=session_id, small=small,
                                  effort=effort, context=context, on_usage=on_usage)

        pipeline.ai.ask = keyword_ask
        try:
            run = pipeline.create("KEYWORDS-1", [str(quiet_log)], use_infographic=False)
            pipeline.executor.start(run["id"], until="log")
            self.wait()
        finally:
            pipeline.ai.ask = original_ask
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["log"]["status"], "done", run["steps"]["log"]["error"])
        self.assertIn("reason=0x13", (pipeline.run_dir(run["id"]) / "03-log-brief.md").read_text(encoding="utf-8"),
                      "without a source catalog the ticket keywords are the only ticket-specific seed")

    def test_changing_inputs_validates_before_touching_the_run(self):
        run = pipeline.create("INPUTS-1", [str(self.work / "app.log")])
        with self.assertRaises(ValueError):
            pipeline.update_inputs(run["id"], log_files=[str(self.work / "missing.log")], source_dir=str(self.work / "src"))
        unchanged = pipeline.get(run["id"])["inputs"]
        self.assertEqual(unchanged["log_files"], [str(self.work / "app.log")])
        self.assertEqual(unchanged["source_dir"], "", "a rejected update leaves every input as it was")
        with self.assertRaises(ValueError):
            pipeline.update_inputs(run["id"], source_dir=str(self.work / "no-such-folder"))
        with self.assertRaises(ValueError):
            pipeline.update_inputs(run["id"], gate="whatever")
        with self.assertRaises(ValueError):
            pipeline.create("INPUTS-2", gate="whatever")
        changed = pipeline.update_inputs(run["id"], gate="review")
        self.assertEqual(changed["inputs"]["gate"], "review")
        self.assertFalse(changed["inputs"]["continue_on_optional_error"], "leaving Skip mode stops on a failed step again")

    def test_a_log_file_that_disappeared_is_skipped_not_fatal(self):
        gone = self.work / "gone.log"
        gone.write_text("10-10 12:30:01.200  1000  1001 E Other: failure in gone file\n", encoding="utf-8")
        run = pipeline.create("GONEFILE-1", [str(gone), str(self.work / "app.log")], use_infographic=False)
        gone.unlink()
        pipeline.executor.start(run["id"], until="log")
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["log"]["status"], "done", run["steps"]["log"]["error"])
        self.assertIn("status=133", (pipeline.run_dir(run["id"]) / "03-log-brief.md").read_text(encoding="utf-8"))

    def test_jira_only_reads_ticket_context_and_skips_log_and_graph(self):
        run = pipeline.create("XYZ-7")
        pipeline.executor.start(run["id"])
        self.wait()
        steps = pipeline.get(run["id"])["steps"]
        self.assertEqual(steps["understand"]["status"], "done")
        self.assertEqual([steps[s]["status"] for s in ("log", "graph")], ["skipped"] * 2)
        self.assertEqual(steps["format"]["status"], "done", steps["format"]["error"])

    def test_manual_pauses_then_scans_selects_and_slices(self):
        run = pipeline.create("MANUAL-1", [str(self.work / "app.log")], str(self.work / "src"),
                              use_codegraph=False, logsleuth_options={"mode": "manual", "budget_tokens": 4000})
        pipeline.executor.start(run["id"])
        self.wait()
        self.assertEqual(run["steps"]["log"]["status"], "waiting")
        self.assertEqual(run["steps"]["analyze"]["status"], "idle")
        self.assertFalse((pipeline.run_dir(run["id"]) / "03-log-brief.md").exists())

        pipeline.executor.start(run["id"], "log", only=True, catalog_only=True)
        self.wait()
        self.assertEqual(run["steps"]["log"]["status"], "waiting", run["steps"]["log"]["error"])
        self.assertTrue((pipeline.run_dir(run["id"]) / "03-catalog.md").exists())
        self.assertEqual(run["log_catalog"]["services"][0]["name"], "BluetoothService")
        self.assertEqual(run["inputs"]["logsleuth"]["catalog_path"], run["log_catalog"]["source_path"])
        pipeline.update_inputs(run["id"], logsleuth_options={
            "services": ["BluetoothService"], "time_enabled": True, "time_start": "12:30:01.250",
            "time_end": "12:30:01.350", "before": 0, "after": 0, "window_ms": 0,
        })
        pipeline.executor.start(run["id"], "log", only=True)
        self.wait()
        self.assertEqual(run["steps"]["log"]["status"], "done", run["steps"]["log"]["error"])
        brief = (pipeline.run_dir(run["id"]) / "03-log-brief.md").read_text(encoding="utf-8")
        self.assertIn("retry scheduled", brief)
        self.assertNotIn("connect failed status=133", brief)
        self.assertNotIn("crash in mixer", brief)
        self.assertEqual(run["log_stats"]["mode"], "manual")
        self.assertEqual(run["steps"]["analyze"]["status"], "idle")

        settings.save({"review_before_publish": True})
        pipeline.executor.start(run["id"], "graph")  # Manual Proceed: resume after the reviewed log brief.
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["log"]["status"], "done")
        self.assertEqual(run["steps"]["graph"]["status"], "skipped")
        self.assertEqual(run["steps"]["analyze"]["status"], "done")
        self.assertEqual(run["steps"]["collab"]["status"], "waiting")

    def test_continue_option_keeps_running_after_steps_2_to_4_fail(self):
        settings.save({"ai_mode": "copilot", "review_before_publish": True})

        def fail_step(run, cfg, ex):
            raise RuntimeError("simulated optional-step failure")

        with patch.dict(pipeline.RUNNERS, {step: fail_step for step in ("understand", "log", "graph")}):
            run = pipeline.create("CONTINUE-1", [str(self.work / "app.log")], str(self.work / "src"),
                                  use_infographic=False, continue_on_optional_error=True)
            pipeline.executor.start(run["id"])
            self.wait()

        run = pipeline.get(run["id"])
        for step in ("understand", "log", "graph"):
            self.assertEqual(run["steps"][step]["status"], "error")
            self.assertIn("simulated optional-step failure", run["steps"][step]["error"])
        self.assertEqual(run["steps"]["analyze"]["status"], "done")
        self.assertEqual(run["steps"]["format"]["status"], "done")
        self.assertEqual(run["steps"]["infographic"]["status"], "skipped")
        self.assertEqual(run["steps"]["collab"]["status"], "waiting")
        analysis_files = [names for prompt, names in self.world.prompts if "ticket-analysis.md" in names][-1]
        self.assertNotIn("02-understanding.md", analysis_files)
        self.assertNotIn("03-log-brief.md", analysis_files)
        self.assertNotIn("04-graph.simple.json", analysis_files)

    def test_optional_step_error_still_stops_when_continue_is_off(self):
        settings.save({"ai_mode": "copilot", "review_before_publish": True})

        def fail_understand(run, cfg, ex):
            raise RuntimeError("simulated understand failure")

        with patch.dict(pipeline.RUNNERS, {"understand": fail_understand}):
            run = pipeline.create("STOP-OPTIONAL-1", [str(self.work / "app.log")],
                                  continue_on_optional_error=False)
            pipeline.executor.start(run["id"])
            self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["understand"]["status"], "error")
        self.assertEqual(run["steps"]["log"]["status"], "idle")
        self.assertEqual(run["steps"]["analyze"]["status"], "idle")

    def test_rerun_clears_an_earlier_pass_error_on_steps_not_reached_yet(self):
        settings.save({"ai_mode": "copilot", "review_before_publish": True})

        def fail_graph(run, cfg, ex):
            raise RuntimeError("simulated graph failure")

        with patch.dict(pipeline.RUNNERS, {"graph": fail_graph}):
            run = pipeline.create("RERUN-ERR-1", [str(self.work / "app.log")], str(self.work / "src"),
                                  use_infographic=False, continue_on_optional_error=False)
            pipeline.executor.start(run["id"])
            self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["graph"]["status"], "error")

        seen = []
        real_jira = pipeline.RUNNERS["jira"]

        def watch_jira(run, cfg, ex):
            seen.append((run["steps"]["graph"]["status"], run["steps"]["graph"]["error"]))
            return real_jira(run, cfg, ex)

        with patch.dict(pipeline.RUNNERS, {"jira": watch_jira}):
            pipeline.executor.start(run["id"], until="jira")  # a new pass that never reaches step 4
            self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(seen, [("idle", "")], "the old failure is cleared as soon as the new pass starts")
        self.assertEqual((run["steps"]["graph"]["status"], run["steps"]["graph"]["error"]), ("idle", ""))
        self.assertEqual(run["steps"]["jira"]["status"], "done")

    def test_ticket_data_folder_holds_every_file_and_an_attachments_subfolder(self):
        with tempfile.TemporaryDirectory() as root, patch.object(paths, "RUNS", Path(root) / "runs"):
            folder = Path(root) / "tickets" / "DATA-1"
            run = pipeline.create("DATA-1", [str(self.work / "app.log")], use_codegraph=False, use_infographic=False,
                                  data_dir=str(folder))
            pipeline.executor.start(run["id"], until="log")
            self.wait()
            run = pipeline.get(run["id"])
            self.assertEqual(run["steps"]["log"]["status"], "done", run["steps"]["log"]["error"])
            self.assertEqual(pipeline.run_dir(run["id"]), folder)
            for name in ("01-jira.md", "02-focus.json", "03-log-brief.md", "run.log"):
                self.assertTrue((folder / name).is_file(), name)
            meta = Path(root) / "runs" / run["id"]
            self.assertTrue((meta / "run.json").is_file(), "History still finds the run")
            self.assertFalse((meta / "01-jira.md").exists())
            self.assertEqual([a["name"] for a in run["steps"]["jira"]["artifacts"]][:1], ["01-jira.md"])
            self.assertEqual(pipeline.attachment_folder(run), folder / "attachments")
            shown = pipeline.view(run)
            self.assertEqual((shown["work_dir"], shown["attachment_dir"]), (str(folder), str(folder / "attachments")))

            moved = Path(root) / "tickets" / "DATA-1-moved"
            pipeline.update_inputs(run["id"], data_dir=str(moved))
            self.assertTrue((moved / "03-log-brief.md").is_file(), "existing files follow the run")
            self.assertTrue((folder / "03-log-brief.md").is_file(), "copied, never removed")
            self.assertEqual(pipeline.get(run["id"])["steps"]["log"]["status"], "done")
            with self.assertRaises(ValueError):
                pipeline.update_inputs(run["id"], data_dir="relative/folder")
            pipeline.clear_runs()
            self.assertTrue((moved / "01-jira.md").is_file(), "clearing History never deletes a ticket data folder")

    def test_extra_time_ranges_typed_at_step_2_are_sliced_too(self):
        run = pipeline.create("WIN-1", [str(self.work / "app.log")], use_codegraph=False, use_infographic=False)
        pipeline.executor.start(run["id"], until="log")
        self.wait()
        brief = pipeline.run_dir(run["id"]) / "03-log-brief.md"
        self.assertIn("status=133", brief.read_text(encoding="utf-8"))
        self.assertNotIn("crash in mixer", brief.read_text(encoding="utf-8"), "13:00 is outside 12:29-12:31")

        focus = {**pipeline.get(run["id"])["focus"],
                 "extra_windows": [{"time_start": "12:59", "time_end": "13:01:00"}, {"time_start": "bad"}, {}]}
        pipeline.write_file(run["id"], "02-focus.json", json.dumps(focus))
        self.assertEqual(pipeline.get(run["id"])["focus"]["extra_windows"],
                         [{"time_start": "12:59:00", "time_end": "13:01:00"}], "invalid / empty rows are dropped")
        pipeline.executor.start(run["id"], "log", only=True)
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["log"]["status"], "done", run["steps"]["log"]["error"])
        text = brief.read_text(encoding="utf-8")
        self.assertIn("status=133", text)
        self.assertIn("crash in mixer", text, "a line inside the second range is kept")
        self.assertNotIn("I BluetoothService: boot", text, "12:20 is in no range")
        self.assertEqual([(w["start"], w["end"]) for w in run["log_stats"]["timeWindows"]],
                         [("12:29:00", "12:31:00"), ("12:59:00", "13:01:00")])
        self.assertIn("(+1)", run["steps"]["log"]["note"])

        pipeline.executor.start(run["id"], "understand", only=True)  # a fresh AI reading of the ticket
        self.wait()
        self.assertEqual(pipeline.get(run["id"])["focus"]["extra_windows"],
                         [{"time_start": "12:59:00", "time_end": "13:01:00"}], "typed ranges survive")

    def test_continue_after_understand_failure_uses_full_log_range(self):
        settings.save({"ai_mode": "copilot", "review_before_publish": True})

        def fail_understand(run, cfg, ex):
            raise RuntimeError("simulated understand failure")

        def fail_graph(run, cfg, ex):
            raise RuntimeError("simulated graph failure")

        with patch.dict(pipeline.RUNNERS, {"understand": fail_understand, "graph": fail_graph}):
            run = pipeline.create("CONTINUE-NOFOCUS-1", [str(self.work / "app.log")], str(self.work / "src"),
                                  continue_on_optional_error=True, use_infographic=False)
            pipeline.executor.start(run["id"])
            self.wait()

        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["understand"]["status"], "error")
        self.assertEqual(run["steps"]["log"]["status"], "done", run["steps"]["log"]["error"])
        self.assertEqual(run["log_stats"]["requestedFocus"]["filter_mode"], "full")
        self.assertIsNone(run["log_stats"]["timeWindow"])
        self.assertEqual(run["steps"]["graph"]["status"], "error")
        self.assertEqual(run["steps"]["analyze"]["status"], "done")
        self.assertEqual(run["steps"]["collab"]["status"], "waiting")
        analysis_files = [names for prompt, names in self.world.prompts if "ticket-analysis.md" in names][-1]
        self.assertNotIn("02-understanding.md", analysis_files)
        self.assertIn("03-log-brief.md", analysis_files)
        self.assertNotIn("04-graph.simple.json", analysis_files)

    def test_manual_empty_window_is_not_silently_widened(self):
        run = pipeline.create("MANEMPTY-1", [str(self.work / "app.log")], str(self.work / "src"),
                              logsleuth_options={"mode": "manual", "time_enabled": True,
                                                 "time_start": "03:00:00", "time_end": "03:00:01"})
        pipeline.executor.start(run["id"], "log", only=True, catalog_only=True)
        self.wait()
        pipeline.executor.start(run["id"], "log", only=True)
        self.wait()
        self.assertEqual(run["steps"]["log"]["status"], "done", run["steps"]["log"]["error"])
        self.assertEqual(run["log_stats"]["kept"], 0)
        self.assertFalse(run["log_stats"]["fullTimeFallback"])
        self.assertIsNotNone(run["log_stats"]["timeWindow"])

    def test_absent_focus_artifact_uses_full_catalog_and_time(self):
        run = pipeline.create("NOFOCUS-1", [str(self.work / "app.log")], str(self.work / "src"), use_codegraph=False)
        pipeline.executor.start(run["id"])
        self.wait()
        (pipeline.run_dir(run["id"]) / "02-focus.json").unlink()
        # Persisted in-memory time must not survive the missing step output.
        self.assertEqual(run["focus"]["filter_mode"], "window")
        pipeline.executor.start(run["id"], "log", only=True)
        self.wait()
        self.assertEqual(run["steps"]["log"]["status"], "done", run["steps"]["log"]["error"])
        self.assertIsNone(run["log_stats"]["timeWindow"])
        self.assertEqual(run["log_stats"]["requestedFocus"]["filter_mode"], "full")
        self.assertEqual(run["log_stats"]["afterTime"], 4)

    def test_auto_empty_time_window_retries_full_range(self):
        run = pipeline.create("AUTOEMPTY-1", [str(self.work / "app.log")], str(self.work / "src"), use_codegraph=False)
        pipeline.executor.start(run["id"])
        self.wait()
        pipeline.write_file(run["id"], "02-focus.json", json.dumps({"time_start": "03:00:00", "time_end": "03:00:01"}))
        pipeline.executor.start(run["id"], "log", only=True)
        self.wait()
        self.assertEqual(run["steps"]["log"]["status"], "done", run["steps"]["log"]["error"])
        self.assertTrue(run["log_stats"]["fullTimeFallback"])
        self.assertGreater(run["log_stats"]["kept"], 0)

    def test_analysis_rejects_legacy_oversized_brief(self):
        run = pipeline.create("OLDBRIEF-1")
        folder = pipeline.run_dir(run["id"])
        (folder / "01-jira.md").write_text("Ticket", encoding="utf-8")
        (folder / "03-log-brief.md").write_text("x" * 50001, encoding="utf-8")
        run["steps"]["log"]["status"] = "done"
        before = len(self.world.prompts)
        with self.assertRaises(RuntimeError):
            pipeline.step_analyze(run, settings.load(), pipeline.executor)
        self.assertEqual(len(self.world.prompts), before, "Oversized legacy artifact must never reach AI")

    def test_copilot_usage_aggregates_and_ignores_m365(self):
        run = pipeline.create("USAGE-1")
        run["ai_mode"] = "copilot"
        pipeline._record_copilot_usage(run, {
            "input_tokens": 120, "output_tokens": 24, "cache_read_tokens": 40,
            "cache_write_tokens": 5, "reasoning_tokens": 8, "model_calls": 1,
        })
        pipeline._record_copilot_usage(run, {
            "input_tokens": 30, "output_tokens": 6, "model_calls": 2,
        })
        usage = pipeline.get(run["id"])["copilot_usage"]
        self.assertEqual(usage["input_tokens"], 150)
        self.assertEqual(usage["output_tokens"], 30)
        self.assertEqual(usage["total_tokens"], 180)
        self.assertEqual(usage["cache_read_tokens"], 40)
        self.assertEqual(usage["model_calls"], 3)

        m365_run = pipeline.create("USAGE-M365-1")
        m365_run["ai_mode"] = "m365"
        pipeline._record_copilot_usage(m365_run, {"input_tokens": 999, "output_tokens": 999, "model_calls": 1})
        self.assertEqual(m365_run["copilot_usage"]["model_calls"], 0)
        self.assertEqual(m365_run["copilot_usage"]["total_tokens"], 0)

    def test_finish_step_accumulates_time_from_repeated_attempts(self):
        state = {"started": 100.0, "ended": None, "elapsed_seconds": 2.5}
        with patch.object(pipeline.time, "time", return_value=104.0):
            pipeline._finish_step(state, "done")
        self.assertEqual(state["elapsed_seconds"], 6.5)
        self.assertEqual(state["ended"], 104.0)


if __name__ == "__main__":
    unittest.main()
