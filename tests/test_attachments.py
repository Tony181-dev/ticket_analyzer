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
        settings.save({"download_dir": str(cls.download_root), "ai_mode": "copilot"})

        def fake_call(role, tool, args, timeout=120):
            if tool == "jira_get_issue":
                return {"content": [{"type": "text", "text": cls.issue_json}]}
            raise AssertionError(tool)
        pipeline.mcp.call = fake_call

        def fake_ask(cfg, prompt, files, run_dir, session_id=None, small=False):
            names = [Path(f).name for f in files]
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

    def test_auto_attachments_feed_log_step(self):
        run = pipeline.create("ATT-1", auto_attachments=True)
        pipeline.executor.start(run["id"], "jira", only=False)
        self.wait()
        run = pipeline.get(run["id"])
        errors = {k: v["error"] for k, v in run["steps"].items() if v["error"]}
        self.assertEqual(errors, {})
        self.assertEqual(len(run["attachments"]), 2)
        self.assertEqual([Path(f).name for f in run["inputs"]["log_files"]], ["main.log"], "png inside zip must be ignored")
        self.assertTrue(str(run["inputs"]["log_files"][0]).startswith(str(self.download_root / "ATT-1")))
        self.assertEqual(run["steps"]["understand"]["status"], "done")
        self.assertEqual(run["steps"]["log"]["status"], "done")
        self.assertIn("status=133", (pipeline.run_dir(run["id"]) / "03-log-brief.md").read_text(encoding="utf-8"))
        self.assertNotIn("/secure/attachment/2/shot.png", HITS, "auto mode only fetches logs/archives")

        # manual download of the image; the zip is already there -> not fetched again
        before = HITS.count("/secure/attachment/1/logs.zip")
        pipeline.executor.fetch_attachments(run["id"], ["logs.zip", "shot.png"], [], None)
        self.wait()
        self.assertEqual(HITS.count("/secure/attachment/1/logs.zip"), before)
        self.assertIn("/secure/attachment/2/shot.png", HITS)
        run = pipeline.get(run["id"])
        self.assertTrue(all(a.get("path") for a in run["attachments"]))

    def test_zip_slip_refused(self):
        bad = self.download_root / "evil.zip"
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("../../escape.log", "x")
        self.assertIsNone(attachments.extract(bad, log=lambda m: None))
        self.assertFalse((self.download_root.parent / "escape.log").exists())

    def test_kinds(self):
        self.assertEqual(attachments.kind_of("a.tar.gz"), "archive")
        self.assertEqual(attachments.kind_of("app.log.3"), "log")
        self.assertEqual(attachments.kind_of("trace.DLT"), "log")
        self.assertEqual(attachments.kind_of("video.mp4"), "other")


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
    def test_legacy_default_model_migrates_once(self):
        paths.SETTINGS_FILE.write_text(json.dumps({"m365_model": "GPT 5.6 Sol Think deeper"}), encoding="utf-8")
        self.assertEqual(settings.load()["m365_model"], "GPT 6 Luna")
        settings.save({"m365_model": "GPT 5.6 Sol Think deeper"})  # an explicit user choice must stick
        self.assertEqual(settings.load()["m365_model"], "GPT 5.6 Sol Think deeper")

    def test_copilot_config_with_comments_is_parsed(self):
        f = Path(tempfile.mkdtemp()) / "config.json"
        f.write_text('// managed automatically\n{\n  "logged_in_users": [{"login": "me"}]\n}\n', encoding="utf-8")
        self.assertEqual(doctor._jsonc(f)["logged_in_users"][0]["login"], "me")


if __name__ == "__main__":
    unittest.main()
