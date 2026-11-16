"""Attachments (fake Jira over local HTTP), settings migration, Copilot config parsing."""
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("TA_DATA_DIR", tempfile.mkdtemp(prefix="ta-test-"))
os.environ["TA_AXEL"] = ""  # tests use the plain HTTP path; the axel test enables it explicitly
sys.path.insert(0, str(ROOT))

from app import attachments, doctor, mcp, paths, pipeline, settings  # noqa: E402

LOG = ("10-10 12:30:01.200  1000  1001 E BluetoothService: connect failed status=133\n"
       "10-10 12:30:01.300  1000  1001 W BluetoothService: retry scheduled\n")


def make_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("logs/main.log", LOG)
        z.writestr("logs/screen.png", b"\x89PNG not a log")
    return buf.getvalue()


FILES = {"/secure/attachment/1/logs.zip": make_zip(), "/secure/attachment/2/shot.png": b"\x89PNG"}
HITS = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        HITS.append(self.path)
        body = FILES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AttachmentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        paths.ensure_dirs()
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.httpd.server_port}"
        cls.issue_json = json.dumps({"key": "ATT-1", "summary": "BT fails", "attachments": [
            {"id": "1", "filename": "logs.zip", "size": len(FILES["/secure/attachment/1/logs.zip"]),
             "url": base + "/secure/attachment/1/logs.zip"},
            {"id": "2", "filename": "shot.png", "size": 4, "url": base + "/secure/attachment/2/shot.png"},
        ]})
        cls.download_root = Path(tempfile.mkdtemp(prefix="ta-dl-"))
        cls.source_dir = Path(tempfile.mkdtemp(prefix="ta-source-"))
        (cls.source_dir / "BluetoothService.py").write_text(
            'import logging\nlogger = logging.getLogger("BluetoothService")\n'
            'logger.error("connect failed status=%d", 133)\nlogger.warning("retry scheduled")\n',
            encoding="utf-8")
        settings.save({"download_dir": str(cls.download_root), "ai_mode": "copilot"})

        cls.broken_json = json.dumps({"key": "ATT-404", "summary": "BT fails", "attachments": [
            {"id": "9", "filename": "gone.zip", "size": 10, "url": base + "/secure/attachment/9/gone.zip"}]})

        def fake_call(role, tool, args, timeout=120):
            if tool == "jira_get_issue":
                text = cls.broken_json if args["issue_key"] == "ATT-404" else cls.issue_json
                return {"content": [{"type": "text", "text": text}]}
            raise AssertionError(tool)
        pipeline.mcp.call = fake_call

        def fake_ask(cfg, prompt, files, run_dir, session_id=None, small=False, effort=None, context=None, on_usage=None):
            names = [Path(f).name for f in files]
            if "infographic.md" in names:
                return json.dumps({"title": "BT failure", "summary": "Connection fails.", "root_cause": "Status 133.",
                                   "timeline": [], "evidence": [], "actions": []})
            if "understand.md" in names:
                cls.understand_files.append(names)
                wanted = "gone.zip" if "ATT-404" in prompt else "logs.zip"
                picks = (', "attachments": [{"filename": "%s", "reason": "the log archive"}]' % wanted
                         if "01-attachments.md" in names else "")
                return '```json\n{"time_start": "12:29:00", "time_end": "12:31:00", "keywords": ["status=133"]' + picks + '}\n```'
            if "ticket-analysis.md" in names:
                return "# Analysis"
            return "```json\n" + json.dumps('<ac:structured-macro ac:name="info"><ac:rich-text-body><p>x</p>'
                                            '</ac:rich-text-body></ac:structured-macro><p>ok</p>') + "\n```"
        pipeline.ai.ask = fake_ask
        cls.understand_files = []

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def wait(self):
        for _ in range(300):
            if not pipeline.executor.busy:
                return
            time.sleep(0.1)
        self.fail("timeout")

    def test_review_gate_downloads_what_the_user_confirms(self):
        run = pipeline.create("ATT-1", source_dir=str(self.source_dir), auto_attachments=True, use_codegraph=False,
                              gate="review")
        before = HITS.count("/secure/attachment/1/logs.zip")
        pipeline.executor.start(run["id"])
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(len(run["attachments"]), 2)
        self.assertEqual(run["steps"]["jira"]["status"], "done")
        self.assertEqual(run["steps"]["understand"]["status"], "done", run["steps"]["understand"]["error"])
        self.assertIn("01-attachments.md", self.understand_files[-1], "the AI chooses from the attachment list")
        self.assertEqual(run["steps"]["log"]["status"], "waiting", "the run pauses at the evidence gate")
        self.assertEqual(run["pending_gate"]["mode"], "review")
        self.assertEqual(run["pending_gate"]["suggested"], ["logs.zip"], "the AI's pick is pre-selected")
        self.assertEqual(run["pending_gate"]["reasons"]["logs.zip"], "the log archive")
        self.assertEqual(run["steps"]["analyze"]["status"], "idle")
        self.assertEqual(HITS.count("/secure/attachment/1/logs.zip"), before, "nothing is downloaded before the user confirms")

        pipeline.executor.resolve_gate(run["id"], ["logs.zip"], None, "12:29", "12:31")
        self.wait()
        run = pipeline.get(run["id"])
        # the ticket states no time zone: Review asks before slicing, here "same clock as the ticket"
        self.assertEqual(run["steps"]["log"]["status"], "waiting")
        self.assertIn("pending_tz", run)
        pipeline.executor.resolve_timezone(run["id"], "suggested", 0)
        self.wait()
        run = pipeline.get(run["id"])
        errors = {k: v["error"] for k, v in run["steps"].items() if v["error"]}
        self.assertEqual(errors, {})
        self.assertNotIn("pending_gate", run)
        self.assertEqual([Path(f).name for f in run["inputs"]["log_files"]], ["main.log"], "png inside zip must be ignored")
        self.assertTrue(str(run["inputs"]["log_files"][0]).startswith(str(self.download_root / "ATT-1")))
        self.assertEqual(run["steps"]["log"]["status"], "done")
        self.assertEqual(run["steps"]["analyze"]["status"], "done")
        self.assertIn("status=133", (pipeline.run_dir(run["id"]) / "03-log-brief.md").read_text(encoding="utf-8"))
        self.assertNotIn("/secure/attachment/2/shot.png", HITS, "only the ticked files are fetched")

        # manual download of the image afterwards; the zip is already there -> not fetched again
        before = HITS.count("/secure/attachment/1/logs.zip")
        pipeline.executor.fetch_attachments(run["id"], ["logs.zip", "shot.png"], None)
        self.wait()
        self.assertEqual(HITS.count("/secure/attachment/1/logs.zip"), before)
        self.assertIn("/secure/attachment/2/shot.png", HITS)
        run = pipeline.get(run["id"])
        self.assertTrue(all(a.get("path") for a in run["attachments"]))
        with self.assertRaises(ValueError):
            pipeline.executor.fetch_attachments(run["id"], [], None)

    def test_skip_mode_downloads_the_ai_picks_without_asking(self):
        run = pipeline.create("ATT-1", source_dir=str(self.source_dir), auto_attachments=True, use_codegraph=False,
                              use_infographic=False, gate="skip")
        self.assertTrue(run["inputs"]["continue_on_optional_error"], "Skip mode keeps going past a failed optional step")
        pipeline.executor.start(run["id"])
        self.wait()
        run = pipeline.get(run["id"])
        errors = {k: v["error"] for k, v in run["steps"].items() if v["error"]}
        self.assertEqual(errors, {})
        self.assertNotIn("pending_gate", run)
        self.assertEqual([Path(f).name for f in run["inputs"]["log_files"]], ["main.log"])
        self.assertEqual(run["steps"]["log"]["status"], "done")
        self.assertEqual(run["steps"]["collab"]["status"], "waiting", "only the publish review is left")
        self.assertTrue(next(a for a in run["attachments"] if a["filename"] == "logs.zip")["analyzed"])
        self.assertFalse(next(a for a in run["attachments"] if a["filename"] == "shot.png").get("path"),
                         "a screenshot is never downloaded as log evidence")

    def test_skip_mode_goes_on_without_logs_when_the_download_fails(self):
        run = pipeline.create("ATT-404", auto_attachments=True, use_infographic=False, gate="skip")
        pipeline.executor.start(run["id"])
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["understand"]["status"], "done")
        self.assertEqual(run["steps"]["log"]["status"], "skipped", run["steps"]["log"]["error"])
        self.assertEqual(run["steps"]["log"]["error"], "")
        self.assertEqual(run["steps"]["analyze"]["status"], "done", "the ticket is still analyzed, without log evidence")
        self.assertEqual(run["steps"]["collab"]["status"], "waiting")
        log = (pipeline.run_dir(run["id"]) / "run.log").read_text(encoding="utf-8")
        self.assertIn("gone.zip", log)

    def test_review_gate_reopens_when_the_download_fails(self):
        run = pipeline.create("ATT-404", auto_attachments=True, use_infographic=False, gate="review")
        pipeline.executor.start(run["id"])
        self.wait()
        self.assertEqual(pipeline.get(run["id"])["steps"]["log"]["status"], "waiting")
        pipeline.executor.resolve_gate(run["id"], ["gone.zip"])
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["log"]["status"], "waiting", "the user is asked again instead of losing the run")
        self.assertIn("gone.zip", run["pending_gate"]["problem"])
        self.assertEqual(run["steps"]["analyze"]["status"], "idle")

        pipeline.executor.resolve_gate(run["id"], skip_logs=True)  # "continue without logs"
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["log"]["status"], "skipped")
        self.assertEqual(run["steps"]["analyze"]["status"], "done")

    def test_manual_gate_asks_the_user_instead_of_the_ai(self):
        asked = len(self.understand_files)
        run = pipeline.create("ATT-1", source_dir=str(self.source_dir), auto_attachments=True, use_codegraph=False,
                              use_infographic=False, gate="manual")
        pipeline.executor.start(run["id"])
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["understand"]["status"], "skipped", "Manual: no AI triage")
        self.assertEqual(len(self.understand_files), asked)
        self.assertEqual(run["steps"]["log"]["status"], "waiting")
        self.assertEqual(run["pending_gate"]["mode"], "manual")
        self.assertEqual(run["pending_gate"]["suggested"], [], "Manual pre-selects nothing")
        with self.assertRaises(ValueError):
            pipeline.executor.resolve_gate(run["id"], ["logs.zip"], None, "25:99", "")

        pipeline.executor.resolve_gate(run["id"], ["logs.zip"], None, "12:30:01.250", "12:30:01.350", 0)
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["log"]["status"], "done", run["steps"]["log"]["error"])
        self.assertEqual(run["inputs"]["log_shift_minutes"], 0)
        brief = (pipeline.run_dir(run["id"]) / "03-log-brief.md").read_text(encoding="utf-8")
        self.assertIn("retry scheduled", brief, "the window typed by the user is the one that is applied")
        self.assertNotIn("connect failed status=133", brief)
        self.assertEqual(run["log_stats"]["timeAlignment"]["source"], "manual")

    def test_progress_is_reported_and_stop_returns_to_the_gate(self):
        run = pipeline.create("ATT-1", source_dir=str(self.source_dir), auto_attachments=True, use_codegraph=False,
                              gate="review")
        pipeline.executor.start(run["id"])
        self.wait()
        seen = []
        real = pipeline.attachments.collect

        def stopping(issue, items, analyze, folder, log, cancel, progress):
            progress("download", 0, len(items), items[0]["filename"])
            seen.append((dict(pipeline.get(run["id"])["att_progress"]), pipeline.get(run["id"])["steps"]["log"]["status"]))
            pipeline.executor.stop()
            return real(issue, items, analyze, folder, log=log, cancel=cancel, progress=progress)

        pipeline.attachments.collect = stopping
        try:
            pipeline.executor.resolve_gate(run["id"], ["logs.zip", "shot.png"])
            self.wait()
        finally:
            pipeline.attachments.collect = real
        self.assertEqual(seen[0][0]["total"], 2)
        self.assertEqual(seen[0][1], "running", "the Log card shows the running download")
        run = pipeline.get(run["id"])
        self.assertNotIn("att_progress", run)
        self.assertEqual(run["steps"]["log"]["status"], "waiting", "stopped: back to the gate")
        self.assertIn("pending_gate", run)
        self.assertEqual(run["inputs"]["log_files"], [])
        self.assertEqual(run["steps"]["analyze"]["status"], "idle")

        # the same run can still be completed afterwards
        pipeline.executor.resolve_gate(run["id"], ["logs.zip"])
        self.wait()
        self.assertEqual(pipeline.get(run["id"])["steps"]["log"]["status"], "done")

    def test_download_limit_keeps_the_most_relevant_picks(self):
        mb = 1024 ** 2
        run = {"attachments": [{"filename": "a.zip", "size": 60 * mb}, {"filename": "b.7z.001", "size": 30 * mb},
                               {"filename": "b.7z.002", "size": 30 * mb}, {"filename": "c.log", "size": 5 * mb}]}
        picks = [{"filename": name} for name in ("a.zip", "b.7z.001", "c.log")]
        self.assertEqual(pipeline._within_budget(run, picks, 100), (["a.zip", "c.log"], ["b.7z.001"]),
                         "a split archive counts with all its volumes")
        self.assertEqual(pipeline._within_budget(run, picks, 0), (["a.zip", "b.7z.001", "c.log"], []), "0 = no limit")

    def test_credentials_follow_the_extension_rules(self):
        def auth(env):
            real = attachments.mcp.load
            attachments.mcp.load = lambda: {"jira": {"env": {"JIRA_URL": "http://j/", **env}}}
            try:
                return attachments._jira_auth()
            finally:
                attachments.mcp.load = real
        self.assertEqual(auth({"JIRA_PERSONAL_TOKEN": "pat"})[1], {"Authorization": "Bearer pat"})
        self.assertTrue(auth({"JIRA_USERNAME": "u", "JIRA_API_TOKEN": "t"})[1]["Authorization"].startswith("Basic "))
        self.assertEqual(auth({"JIRA_API_TOKEN": "t"})[1], {"Authorization": "Bearer t"})
        self.assertEqual(auth({"JIRA_PERSONAL_TOKEN": "${input:jira}"})[1], {})

    def test_failed_download_reports_the_reason(self):
        item = {"id": "9", "filename": "missing.zip", "size": 0, "kind": "archive",
                "url": f"http://127.0.0.1:{self.httpd.server_port}/secure/attachment/9/missing.zip"}
        folder = Path(tempfile.mkdtemp(prefix="ta-fail-"))
        with self.assertRaisesRegex(RuntimeError, "missing.zip.*404"):
            attachments.download("ATT-1", [item], folder, log=lambda m: None)
        self.assertEqual(list(folder.iterdir()), [])

    def test_bundled_axel_is_found_and_downloads(self):
        os.environ.pop("TA_AXEL")
        try:
            exe = attachments.axel_path()
            if not exe:
                self.skipTest("no usable axel on this machine")
            self.assertTrue(Path(exe).samefile(paths.AXEL_DIR / "axel.exe"), "the copy shipped with the tool wins")
            item = {"id": "1", "filename": "logs.zip", "size": len(FILES["/secure/attachment/1/logs.zip"]), "kind": "archive",
                    "url": f"http://127.0.0.1:{self.httpd.server_port}/secure/attachment/1/logs.zip"}
            folder = Path(tempfile.mkdtemp(prefix="ta-axel-"))
            got = attachments.download("ATT-1", [item], folder, log=lambda m: None)
            self.assertEqual(got["logs.zip"].read_bytes(), FILES["/secure/attachment/1/logs.zip"])
            self.assertEqual([p.name for p in folder.iterdir()], ["logs.zip"], "no .part / .st leftovers")
            settings.save({"axel_command": str(folder / "nope.exe")})  # an unusable configured path falls through
            self.assertTrue(Path(attachments.axel_path()).samefile(exe))
        finally:
            settings.save({"axel_command": ""})
            os.environ["TA_AXEL"] = ""

    def test_stop_interrupts_a_download_and_extraction(self):
        folder = Path(tempfile.mkdtemp(prefix="ta-stop-"))
        base = f"http://127.0.0.1:{self.httpd.server_port}"
        item = {"id": "1", "filename": "logs.zip", "size": 0, "url": base + "/secure/attachment/1/logs.zip", "kind": "archive"}

        class Stop(Exception):
            pass

        def stop():
            raise Stop()
        with self.assertRaises(Stop):
            attachments.download("ATT-1", [item], folder, log=lambda m: None, cancel=stop)
        self.assertEqual(list(folder.iterdir()), [], "no partial file is left behind")

        archive = folder / "x.zip"
        archive.write_bytes(FILES["/secure/attachment/1/logs.zip"])
        with self.assertRaises(Stop):
            attachments.extract(archive, log=lambda m: None, cancel=stop)
        self.assertFalse((folder / "x.zip_extracted").exists())

    def test_ticket_without_usable_attachments_is_a_ticket_only_run(self):
        real = self.__class__.issue_json
        self.__class__.issue_json = json.dumps({"key": "ATT-1", "summary": "BT fails", "attachments": [
            {"id": "2", "filename": "shot.png", "size": 4, "url": "http://127.0.0.1:1/shot.png"}]})
        try:
            run = pipeline.create("ATT-1", auto_attachments=True, use_infographic=False, gate="review")
            pipeline.executor.start(run["id"])
            self.wait()
        finally:
            self.__class__.issue_json = real
        run = pipeline.get(run["id"])
        self.assertEqual([run["steps"][s]["status"] for s in ("understand", "log")], ["skipped"] * 2,
                         "a screenshot is not log evidence: no AI triage, no gate")
        self.assertNotIn("pending_gate", run)
        self.assertEqual(run["steps"]["analyze"]["status"], "done")

    def test_zip_slip_refused(self):
        bad = self.download_root / "evil.zip"
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("../../escape.log", "x")
        self.assertIsNone(attachments.extract(bad, log=lambda m: None))
        self.assertFalse((self.download_root.parent / "escape.log").exists())

    def test_kinds(self):
        self.assertEqual(attachments.kind_of("a.tar.gz"), "archive")
        self.assertEqual(attachments.kind_of("분할재압축.7z.001"), "archive")
        self.assertFalse(attachments.is_log_file("분할재압축.7z.001"))
        self.assertEqual(attachments.kind_of("app.log.3"), "log")
        self.assertEqual(attachments.kind_of("trace.DLT"), "log")
        self.assertEqual(attachments.kind_of("video.mp4"), "other")

    def test_selecting_a_7z_volume_includes_all_parts(self):
        names = attachments.expand_7z_volumes(
            ["capture.7z.002"], ["capture.7z.001", "capture.7z.002", "capture.7z.003", "other.7z.001"])
        self.assertEqual(names, {"capture.7z.001", "capture.7z.002", "capture.7z.003"})

        parts = [f"capture.7z.{index:03d}" for index in range(1, 19)]
        expanded = attachments.expand_7z_volumes(parts[:3], parts)
        self.assertEqual(len(parts[:3]), 3)
        self.assertEqual(len(expanded), 18, "selected split volumes expand to the full archive")


class PerFileProgressTest(unittest.TestCase):
    def test_each_file_has_its_own_progress_row(self):
        run = pipeline.create("PROG-1", use_infographic=False)
        run["attachments"] = [{"id": "1", "filename": "a.zip", "size": 2048, "kind": "archive"},
                              {"id": "2", "filename": "b.log", "size": "77 kB", "kind": "log"},
                              {"id": "3", "filename": "c.png", "size": 10, "kind": "other"}]
        seen = []

        def fake_collect(issue, items, analyze, folder, log, cancel, progress):
            for call in (("download", 0, 3, "a.zip", 40), ("downloaded", 0, 3, "a.zip", 100),
                         ("download", 1, 3, "b.log"), ("failed", 1, 3, "b.log"),
                         ("present", 2, 3, "c.png", 100), ("extract", 0, 1, "a.zip"), ("extracted", 0, 1, "a.zip")):
                progress(*call)
                seen.append({f["name"]: (f["state"], f["pct"]) for f in run["att_progress"]["files"]})
            return [], {}

        with mock.patch.object(pipeline.attachments, "collect", side_effect=fake_collect):
            pipeline._fetch_attachments(run, {"a.zip", "b.log", "c.png"}, set(), lambda: None)
        self.assertEqual(seen[0], {"a.zip": ("download", 40), "b.log": ("queued", 0), "c.png": ("queued", 0)})
        self.assertEqual(seen[3]["b.log"][0], "failed")
        self.assertEqual(seen[4]["c.png"], ("present", 100))
        self.assertEqual(seen[5]["a.zip"][0], "extract")
        self.assertEqual(seen[-1], {"a.zip": ("ready", 100), "b.log": ("failed", 0), "c.png": ("present", 100)})
        self.assertNotIn("att_progress", run, "the rows disappear once the job is over")


class SizeParsingTest(unittest.TestCase):
    def test_human_readable_sizes(self):
        p = attachments.parse_size
        self.assertEqual(p(12345), 12345)
        self.assertEqual(p("12345"), 12345)
        self.assertEqual(p("77 kB"), 77 * 1024)
        self.assertEqual(p("1.5 MB"), int(1.5 * 1024 ** 2))
        self.assertEqual(p("1,5 MB"), int(1.5 * 1024 ** 2))
        self.assertEqual(p("12,345 B"), 12345)
        self.assertEqual(p("2GB"), 2 * 1024 ** 3)
        for bad in (None, "", "?", "77 parsecs", True):
            self.assertEqual(p(bad), 0)

    def test_issue_json_with_string_size(self):
        payload = json.dumps({"attachments": [{"id": "1", "filename": "log.zip", "size": "77 kB"}]})
        self.assertEqual(attachments.from_issue_json(payload)[0]["size"], 77 * 1024)


class HistoryResetTest(unittest.TestCase):
    def test_clear_runs_keeps_the_running_one(self):
        keep = pipeline.create("KEEP-1")
        gone = pipeline.create("GONE-1")
        pipeline.clear_runs(keep=keep["id"])
        self.assertTrue(pipeline.run_dir(keep["id"]).exists())
        self.assertFalse(pipeline.run_dir(gone["id"]).exists())
        self.assertIsNone(pipeline.get(gone["id"]))
        self.assertEqual([r["id"] for r in pipeline.list_runs()], [keep["id"]])


class SettingsAndDoctorTest(unittest.TestCase):
    def test_previous_default_models_migrate_once(self):
        paths.SETTINGS_FILE.write_text(json.dumps({"m365_model": "GPT 6 Luna", "copilot_model": "", "settings_version": 2}), encoding="utf-8")
        self.assertEqual(settings.load()["m365_model"], "GPT-5.6 Sol Think Deeper")
        self.assertEqual(settings.load()["copilot_model"], "gpt-6-luna")
        settings.save({"m365_model": "GPT 6 Luna", "copilot_model": "gpt-5"})  # an explicit user choice must stick
        self.assertEqual(settings.load()["m365_model"], "GPT 6 Luna")
        self.assertEqual(settings.load()["copilot_model"], "gpt-5")

    def test_copilot_config_with_comments_is_parsed(self):
        f = Path(tempfile.mkdtemp()) / "config.json"
        f.write_text('// managed automatically\n{\n  "logged_in_users": [{"login": "me"}]\n}\n', encoding="utf-8")
        self.assertEqual(doctor._jsonc(f)["logged_in_users"][0]["login"], "me")


if __name__ == "__main__":
    unittest.main()
