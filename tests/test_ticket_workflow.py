"""Ticket context, durable downloads, nested extraction and individual history deletion."""
import io
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

os.environ.setdefault("TA_DATA_DIR", tempfile.mkdtemp(prefix="ta-workflow-"))

from app import attachments, paths, pipeline, settings


def zipped(name, content):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(name, content)
    return stream.getvalue()


class TicketWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ta-workflow-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        runs = self.root / "runs"
        runs.mkdir()
        cfg = {**settings.load(), "lang": "en", "download_dir": str(self.root / "downloads")}
        for patch in (mock.patch.object(paths, "RUNS", runs), mock.patch.object(pipeline, "_runs", {}),
                      mock.patch.object(settings, "load", return_value=cfg),
                      mock.patch.object(pipeline, "executor", pipeline.Executor())):
            patch.start()
            self.addCleanup(patch.stop)

    def wait(self):
        pipeline.executor.thread.join(timeout=5)
        self.assertFalse(pipeline.executor.busy)

    def test_understanding_keeps_prose_and_internal_focus_separate(self):
        run = pipeline.create("CONTEXT-1")
        pipeline._write(run, "01-jira.md", "Bluetooth fails after pairing")
        answer = '# Initial understanding\nPairing succeeds, then the connection drops.\n\n```json\n' + json.dumps(
            {"time_start": "12:30:00", "time_end": "12:31:00", "reason": "Incident time in ticket"}) + '\n```'
        with mock.patch.object(pipeline, "_ask_ai", return_value=answer) as ask:
            pipeline.step_understand(run, settings.load(), pipeline.executor)
        text = (pipeline.run_dir(run["id"]) / "02-understanding.md").read_text(encoding="utf-8")
        self.assertIn("Pairing succeeds", text)
        self.assertNotIn("```json", text)
        self.assertNotIn('"time_start"', text)
        self.assertEqual(run["focus"]["time_start"], "12:30:00")
        self.assertIn("English", ask.call_args.args[2])
        pipeline._save(run)
        self.assertEqual([a["name"] for a in run["steps"]["understand"]["artifacts"]], ["02-understanding.md"])

    def test_json_only_legacy_answer_does_not_leak_into_readable_tab(self):
        text = pipeline._understanding_text('{"reason": "No time given"}', {"issue": "CTX-1"}, {"reason": "No time given"})
        self.assertIn("No time given", text)
        self.assertNotIn('{"reason"', text)

    def test_nested_archive_is_discoverable_and_extracts_into_log_inputs(self):
        run = pipeline.create("NESTED-1")
        folder = pipeline.attachment_folder(run)
        folder.mkdir(parents=True)
        archive = folder / "outer.zip"
        archive.write_bytes(zipped("inner.zip", zipped("main.log", "12:30:00 E Failure\n")))
        out = attachments.extract(archive, log=lambda _: None)
        run["attachments"] = [{"filename": archive.name, "kind": "archive", "path": str(archive)}]
        inner = out / "inner.zip"
        run["inputs"]["log_files"] = [str(inner)]
        files = pipeline.attachment_files(run)
        self.assertTrue(next(f for f in files if f["path"] == str(inner))["extractable"])
        self.assertNotIn(attachments.EXTRACT_MARKER, [f["name"] for f in files])
        with self.assertRaisesRegex(ValueError, "not found"):
            pipeline.executor.extract_attachment(run["id"], str(self.root / "unregistered.zip"))
        pipeline.executor.extract_attachment(run["id"], str(inner))
        self.wait()
        self.assertEqual(run["att_result"]["files"][0]["state"], "ready")
        self.assertEqual([Path(f).name for f in run["inputs"]["log_files"]], ["main.log"])
        self.assertTrue(next(f for f in pipeline.attachment_files(run) if f["path"] == str(inner))["extracted"])
        self.assertEqual([p.name for p in attachments.logs_in(out)], ["main.log"],
                         "reusing the outer archive must not feed both the inner archive and its logs")

    def test_incomplete_or_changed_extraction_is_not_reused(self):
        archive = self.root / "retry.zip"
        archive.write_bytes(zipped("main.log", "complete"))
        out = archive.with_name(archive.name + "_extracted")
        out.mkdir()
        (out / "half.log").write_text("partial", encoding="utf-8")
        self.assertEqual(attachments.extract(archive, log=lambda _: None), out)
        self.assertFalse((out / "half.log").exists())
        self.assertEqual(attachments.logs_in(out), [out / "main.log"])
        archive.write_bytes(zipped("new.log", "different"))
        self.assertIsNone(attachments.extracted_folder(archive))
        attachments.extract(archive, log=lambda _: None)
        self.assertEqual(attachments.logs_in(out), [out / "new.log"])

    def test_stop_keeps_successful_download_and_durable_progress(self):
        run = pipeline.create("STOPFILES-1")
        run["attachments"] = [{"filename": "first.log", "size": 4, "kind": "log"},
                              {"filename": "later.zip", "size": 20, "kind": "archive"}]

        def stopped(issue, items, analyze, folder, log, cancel, progress):
            folder.mkdir(parents=True)
            (folder / "first.log").write_text("okay", encoding="utf-8")
            progress("downloaded", 0, 2, "first.log", 100)
            progress("download", 1, 2, "later.zip", 20)
            raise pipeline.Cancelled()

        with mock.patch.object(attachments, "collect", side_effect=stopped):
            with self.assertRaises(pipeline.Cancelled):
                pipeline._fetch_attachments(run, {"first.log", "later.zip"}, set(), lambda: None)
        self.assertEqual(Path(run["attachments"][0]["path"]).read_text(encoding="utf-8"), "okay")
        self.assertEqual([f["state"] for f in run["att_result"]["files"]], ["done", "cancelled"])
        self.assertEqual(len(pipeline.attachment_files(run)), 1)
        self.assertNotIn("att_progress", run)

    def test_failed_extraction_is_failed_and_can_be_retried(self):
        run = pipeline.create("BADZIP-1")
        folder = pipeline.attachment_folder(run)
        folder.mkdir(parents=True)
        archive = folder / "bad.zip"
        archive.write_bytes(b"not a zip")
        item = {"filename": archive.name, "kind": "archive", "size": archive.stat().st_size}
        run["attachments"] = [item]
        with mock.patch.object(attachments, "download", return_value={archive.name: archive}):
            pipeline._fetch_attachments(run, {archive.name}, {archive.name}, lambda: None)
        self.assertEqual(run["att_result"]["files"][0]["state"], "failed")
        self.assertFalse(item.get("analyzed"))
        self.assertTrue(archive.exists())
        self.assertEqual(run["inputs"]["log_files"], [])

    def test_delete_keeps_active_run_other_history_and_external_files(self):
        external = self.root / "ticket-data"
        run = pipeline.create("DELETE-1", data_dir=str(external))
        other = pipeline.create("DELETE-2")
        (external / "keep.md").write_text("keep", encoding="utf-8")
        folder = pipeline.attachment_folder(run)
        folder.mkdir(parents=True)
        (folder / "keep.log").write_text("keep", encoding="utf-8")
        with mock.patch.object(pipeline, "executor") as active:
            active.busy = True
            active.run_id = run["id"]
            self.assertFalse(pipeline.delete_run(run["id"]))
        self.assertTrue(pipeline.delete_run(run["id"]))
        self.assertIsNone(pipeline.get(run["id"]))
        self.assertIsNotNone(pipeline.get(other["id"]))
        self.assertTrue((external / "keep.md").exists())
        self.assertTrue((folder / "keep.log").exists())
        with self.assertRaisesRegex(ValueError, "Invalid run id"):
            pipeline.delete_run("../outside")

    def test_history_reads_remain_consistent_during_progress_saves(self):
        run = pipeline.create("PROGRESS-1")

        def work(index):
            for _ in range(15):
                if index % 2:
                    pipeline._save(run)
                else:
                    self.assertEqual([item["id"] for item in pipeline.list_runs()], [run["id"]])

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(work, range(4)))

    def test_m365_blocked_attachment_or_uncertain_send_is_not_blindly_retried(self):
        run = pipeline.create('M365-RETRY-1')
        for code in ('M365_ATTACHMENT_BLOCKED', 'M365_ATTACHMENT_INPUT', 'M365_SEND_UNCERTAIN',
                     'M365_DRAFT_NOT_CLEARED', 'M365_ATTACHMENT_MANIFEST'):
            with self.subTest(code=code), mock.patch.object(pipeline.ai, 'ask', side_effect=RuntimeError(code)) as ask:
                with self.assertRaisesRegex(RuntimeError, code):
                    pipeline._ask_ai(run, settings.load(), 'prompt', [], self.root, retries=2)
                self.assertEqual(ask.call_count, 1)

    def test_reset_clears_outputs_tokens_time_and_session_but_preserves_attachments(self):
        run = pipeline.create('RESET-1', data_dir=str(self.root / 'ticket'))
        folder = pipeline.attachment_folder(run)
        folder.mkdir(parents=True)
        archive = folder / 'logs.zip'
        archive.write_bytes(b'keep archive')
        log = folder / 'logs.zip_extracted' / 'main.log'
        log.parent.mkdir(); log.write_text('keep log', encoding='utf-8')
        run['attachments'] = [{'filename': 'logs.zip', 'path': str(archive), 'analyzed': True}]
        run['inputs']['log_files'] = [str(log)]
        run.update(summary='Old summary', collab={'page_id': '123'}, focus={'time_start': '12:30'},
                   pending_gate={'problem': 'old'}, report_lang='vi',
                   usage_passes=[{'n': 1, 'input_tokens': 100, 'output_tokens': 50, 'model_calls': 1}],
                   copilot_usage={'input_tokens': 100, 'output_tokens': 50, 'model_calls': 1})
        for state in run['steps'].values():
            state.update(status='done', started=1, ended=61, elapsed_seconds=60)
        for name in ('01-jira.md', '02-focus.json', '05-analysis.md', '06-infographic.svg', 'run.log', '01-attachments.md'):
            pipeline._write(run, name, 'old output')
        note = pipeline._write(run, 'my-notes.md', 'keep my note')
        scratch = pipeline.run_dir(run['id']) / 'logs-prepared'
        scratch.mkdir(); (scratch / 'prepared.log').write_text('scratch', encoding='utf-8')
        old_session = run['session_id']
        pipeline._save(run)
        self.assertGreater(run['usage_total']['total_tokens'], 0)
        result = pipeline.reset_workflow(run['id'])
        self.assertEqual(result['usage_total']['total_tokens'], 0)
        self.assertEqual(result['usage_passes'], [])
        self.assertEqual(result['copilot_usage']['input_tokens'], 0)
        self.assertTrue(all(state['status'] == 'idle' and state['elapsed_seconds'] == 0 for state in result['steps'].values()))
        self.assertNotEqual(result['session_id'], old_session)
        self.assertEqual(result['collab'], {})
        self.assertNotIn('pending_gate', result)
        self.assertTrue(archive.exists() and log.exists() and note.exists())
        self.assertEqual(result['inputs']['log_files'], [str(log)])
        self.assertTrue(pipeline._path(run, '01-attachments.md').exists())
        self.assertFalse(pipeline._path(run, '05-analysis.md').exists())
        self.assertFalse(pipeline._path(run, '06-infographic.svg').exists())
        self.assertFalse(scratch.exists())
        pipeline._runs.pop(run['id'])
        self.assertEqual(pipeline.get(run['id'])['usage_total']['total_tokens'], 0)

    def test_reset_is_rejected_while_busy_and_never_deletes_downloads_in_a_work_folder(self):
        run = pipeline.create('RESET-2')
        output = pipeline._write(run, '05-analysis.md', 'old analysis')
        with mock.patch.object(pipeline, 'executor') as active:
            active.busy = True
            with self.assertRaises(RuntimeError):
                pipeline.reset_workflow(run['id'])
        self.assertTrue(output.exists())
        unusual = pipeline.run_dir(run['id']) / 'ctx' / 'download.zip'
        unusual.parent.mkdir(); unusual.write_bytes(b'keep')
        run['attachments'] = [{'filename': unusual.name, 'path': str(unusual)}]
        pipeline.reset_workflow(run['id'])
        self.assertTrue(unusual.exists(), 'an attachment wins over scratch-folder cleanup')


if __name__ == "__main__":
    unittest.main()
