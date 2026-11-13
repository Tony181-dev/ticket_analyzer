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

        def fake_call(role, tool, args, timeout=120):
            if tool == "jira_get_issue":
                return {"content": [{"type": "text", "text": cls.issue_json}]}
            raise AssertionError(tool)
        pipeline.mcp.call = fake_call

        def fake_ask(cfg, prompt, files, run_dir, session_id=None, small=False, effort=None, context=None, on_usage=None):
            names = [Path(f).name for f in files]
            if names == ["05-analysis.md"]:
                return json.dumps({"title": "BT failure", "summary": "Connection fails.", "root_cause": "Status 133.",
                                   "timeline": [], "evidence": [], "actions": []})
            if "understand.md" in names:
                return '```json\n{"time_start": "12:29:00", "time_end": "12:31:00", "keywords": ["status=133"]}\n```'
            if "ticket-analysis.md" in names:
                return "# Analysis"
            return "```json\n" + json.dumps('<ac:structured-macro ac:name="info"><ac:rich-text-body><p>x</p>'
                                            '</ac:rich-text-body></ac:structured-macro><p>ok</p>') + "\n```"
        pipeline.ai.ask = fake_ask

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def wait(self):
        for _ in range(300):
            if not pipeline.executor.busy:
                return
            time.sleep(0.1)
        self.fail("timeout")

    def test_attachment_menu_feeds_log_step(self):
        run = pipeline.create("ATT-1", source_dir=str(self.source_dir), auto_attachments=True, use_codegraph=False)
        pipeline.executor.start(run["id"], "jira", only=False)
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(len(run["attachments"]), 2)
        self.assertEqual(run["steps"]["jira"]["status"], "waiting", "run pauses for the attachment menu")
        self.assertTrue(run["pending_attachments"]["resume"])
        self.assertEqual(run["steps"]["understand"]["status"], "idle")
        self.assertNotIn("/secure/attachment/1/logs.zip", HITS, "nothing is downloaded before the user chooses")

        pipeline.executor.fetch_attachments(run["id"], ["logs.zip"], None)
        self.wait()
        run = pipeline.get(run["id"])
        errors = {k: v["error"] for k, v in run["steps"].items() if v["error"]}
        self.assertEqual(errors, {})
        self.assertNotIn("pending_attachments", run)
        self.assertEqual(run["steps"]["jira"]["status"], "done")
        self.assertEqual([Path(f).name for f in run["inputs"]["log_files"]], ["main.log"], "png inside zip must be ignored")
        self.assertTrue(str(run["inputs"]["log_files"][0]).startswith(str(self.download_root / "ATT-1")))
        self.assertEqual(run["steps"]["understand"]["status"], "done")
        self.assertEqual(run["steps"]["log"]["status"], "done")
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

    def test_progress_is_reported_and_stop_keeps_the_menu(self):
        run = pipeline.create("ATT-1", source_dir=str(self.source_dir), auto_attachments=True, use_codegraph=False)
        pipeline.executor.start(run["id"], "jira", only=False)
        self.wait()
        seen = []
        real = pipeline.attachments.collect

        def stopping(issue, items, analyze, folder, log, cancel, progress):
            progress("download", 0, len(items), items[0]["filename"])
            seen.append((dict(pipeline.get(run["id"])["att_progress"]), pipeline.get(run["id"])["steps"]["jira"]["status"]))
            pipeline.executor.stop()
            return real(issue, items, analyze, folder, log=log, cancel=cancel, progress=progress)

        pipeline.attachments.collect = stopping
        try:
            pipeline.executor.fetch_attachments(run["id"], ["logs.zip", "shot.png"], None)
            self.wait()
        finally:
            pipeline.attachments.collect = real
        self.assertEqual(seen[0][0]["total"], 2)
        self.assertEqual(seen[0][1], "running", "Jira card shows the running download")
        run = pipeline.get(run["id"])
        self.assertNotIn("att_progress", run)
        self.assertEqual(run["steps"]["jira"]["status"], "waiting", "stopped: back to the menu")
        self.assertIn("pending_attachments", run)
        self.assertEqual(run["inputs"]["log_files"], [])
        self.assertEqual(run["steps"]["understand"]["status"], "idle")

        # the same run can still be completed afterwards
        pipeline.executor.fetch_attachments(run["id"], ["logs.zip"], None)
        self.wait()
        self.assertEqual(pipeline.get(run["id"])["steps"]["log"]["status"], "done")

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

    def test_skipping_the_menu_resumes_the_run(self):
        run = pipeline.create("ATT-1", auto_attachments=True)
        pipeline.executor.start(run["id"], "jira", only=False)
        self.wait()
        pipeline.executor.fetch_attachments(run["id"], [], None)
        self.wait()
        run = pipeline.get(run["id"])
        self.assertEqual(run["steps"]["jira"]["status"], "done")
        self.assertEqual(run["steps"]["understand"]["status"], "skipped")
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
