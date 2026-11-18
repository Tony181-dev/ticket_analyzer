"""Regression coverage for attachment planning, multipart budgets and image-only evidence."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("TA_DATA_DIR", tempfile.mkdtemp(prefix="ta-selection-"))

from app import attachments, paths, pipeline, settings


MB = 1024 ** 2


def item(name, mb=1):
    return {"filename": name, "size": mb * MB, "kind": attachments.kind_of(name)}


class EvidenceSelectionTest(unittest.TestCase):
    def run_with(self, *items, mode="skip", picks=None):
        return {"issue": "PICK-1", "attachments": list(items),
                "inputs": {"gate": mode, "auto_attachments": True, "log_files": []},
                "focus": {"attachments": picks}, "steps": {"understand": {"status": "done"}}}

    def test_auto_does_not_treat_empty_ai_choice_as_a_veto(self):
        run = self.run_with(item("capture.zip"), item("other.log"), picks=[])
        picks, _ = pipeline._attachment_picks(run)
        self.assertEqual({p["filename"] for p in picks}, {"capture.zip", "other.log"})

    def test_images_are_evidence_and_rank_before_bulk_logs(self):
        run = self.run_with(item("capture.zip", 10), item("screen.png"), picks=[])
        picks, _ = pipeline._attachment_picks(run)
        self.assertEqual(picks[0]["filename"], "screen.png")
        self.assertEqual(pipeline._within_budget(run, picks, 2)[0], ["screen.png"])

    def test_ai_ranking_is_kept_and_auto_fills_the_remaining_budget(self):
        run = self.run_with(item("first.zip", 3), item("second.log"), item("third.log"),
                            picks=[{"filename": "first.zip", "reason": "reporter names inner/main.log"}])
        picks, source = pipeline._attachment_picks(run)
        self.assertEqual(source, "ai")
        self.assertEqual(picks[0]["filename"], "first.zip")
        self.assertEqual(set(pipeline._within_budget(run, picks, 5)[0]),
                         {"first.zip", "second.log", "third.log"})

    def test_nineteen_volumes_are_all_or_nothing_and_not_double_counted(self):
        parts = [item(f"capture.7z.{n:03}") for n in range(1, 20)]
        run = self.run_with(*parts, item("small.log"))
        picks = [{"filename": p["filename"]} for p in parts] + [{"filename": "small.log"}]
        chosen, _ = pipeline._within_budget(run, picks, 20)
        self.assertEqual(attachments.expand_7z_volumes(chosen, [p["filename"] for p in parts]),
                         {p["filename"] for p in parts} | {"small.log"})
        self.assertEqual(pipeline._within_budget(run, picks, 18)[0], ["small.log"])

    def test_incomplete_or_unknown_size_groups_are_not_auto_downloaded(self):
        run = self.run_with(item("capture.7z.001"), item("capture.7z.003"), item("unknown.zip", 0), item("ok.log"))
        picks = [{"filename": p["filename"]} for p in run["attachments"]]
        self.assertEqual(pipeline._within_budget(run, picks, 10)[0], ["ok.log"])

    def test_numbered_zip_and_part_rar_expand_as_atomic_sets(self):
        for names in ([f"capture.zip.{n:03}" for n in range(1, 4)],
                      [f"capture.part{n:02}.rar" for n in range(1, 4)]):
            run = self.run_with(*(item(n) for n in names))
            self.assertEqual(attachments.expand_7z_volumes([names[1]], names), set(names))
            self.assertEqual(pipeline._within_budget(run, [{"filename": names[1]}], 2)[0], [])

    def test_review_suggestions_obey_budget_and_expand_parts(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(paths, "RUNS", Path(folder)), \
                patch.object(settings, "load", return_value={**settings.DEFAULTS, "auto_download_max_mb": 2}):
            run = pipeline.create("REVIEW-BUDGET-1", gate="review", auto_attachments=True)
            run["attachments"] = [item("huge.zip", 3), item("small.log"), item("screen.png")]
            run["focus"] = {"attachments": [{"filename": "huge.zip", "reason": "AI preferred"}]}
            run["steps"]["understand"]["status"] = "done"
            pipeline._open_gate(run)
            self.assertEqual(set(run["pending_gate"]["suggested"]), {"small.log", "screen.png"})
            gate = run["pending_gate"]
            self.assertEqual([d["filename"] for d in gate["over_budget"]], ["huge.zip"],
                             "what the limit left out is offered to the user, not silently dropped")
            self.assertEqual(gate["over_bytes"], 3 * MB)
            self.assertNotIn("huge.zip", gate["excluded"], "excluded = missing parts / unknown size only")

    def test_gate_records_a_utc_zone_not_a_clock_shift(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(paths, "RUNS", Path(folder)):
            run = pipeline.create("ZONE-GATE-1", gate="manual")
            with patch.object(pipeline.executor, "_download"):
                pipeline.executor.resolve_gate(run["id"], time_start="19:38", time_end="19:40",
                                                ticket_utc_offset="UTC+7")
            self.assertEqual(run["focus"]["ticket_utc_offset"], "+07:00")
            self.assertEqual(run["focus"]["time_start"], "19:38:00")
            self.assertIsNone(run["inputs"]["log_shift_minutes"])
            with patch.object(pipeline.executor, "_download"), self.assertRaises(ValueError):
                pipeline.executor.resolve_gate(run["id"], ticket_utc_offset="UTC+25")

    def test_confirmed_image_only_run_skips_log_processing(self):
        run = self.run_with(item("screen.png"), mode="review")
        run["gate_state"] = {"resolved": True}
        with self.assertRaises(pipeline.Skip):
            pipeline.step_log(run, settings.DEFAULTS, Mock())

    def test_downloaded_images_reach_analysis_without_entering_logsleuth(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(paths, "RUNS", Path(folder)):
            run = pipeline.create("IMAGE-EVIDENCE-1", use_codegraph=False)
            image = Path(folder) / "shot.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n")
            run["attachments"] = [{**item("shot.png"), "path": str(image)}]
            pipeline._write(run, "01-jira.md", "Screenshots show the codec version.")
            with patch.object(pipeline, "_ask_ai", return_value="# Visual findings") as ask:
                pipeline.step_analyze(run, settings.DEFAULTS, Mock())
            self.assertIn(image, ask.call_args.args[3])
            self.assertIn("image", ask.call_args.args[2].lower())

    def test_many_images_are_read_in_three_file_batches_before_final_analysis(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(paths, "RUNS", Path(folder)):
            run = pipeline.create("IMAGE-BATCH-1", use_codegraph=False)
            pipeline._write(run, "01-jira.md", "Screenshots show the error.")
            run["attachments"] = []
            for n in range(5):
                image = Path(folder) / f"shot-{n}.png"
                image.write_bytes(b"\x89PNG\r\n\x1a\n")
                run["attachments"].append({**item(image.name), "path": str(image)})
            with patch.object(pipeline, "_ask_ai", return_value="Visible version: 20260923") as ask:
                pipeline.step_analyze(run, {**settings.DEFAULTS, "ai_mode": "m365"}, Mock())
            calls = ask.call_args_list
            self.assertEqual(len(calls), 3, "five M365 images use two vision calls plus final analysis")
            self.assertEqual([len(call.args[3]) for call in calls[:-1]], [3, 2])
            for call in calls[:-1]:
                self.assertTrue(all(file.suffix == '.png' for file in call.args[3]), 'all upload slots carry images')
                self.assertIn('Cross-image findings', call.args[2])
                self.assertIn('Ticket context excerpt', call.args[2])
            self.assertIn(pipeline._path(run, "05-image-evidence.md"), calls[-1].args[3])
            self.assertFalse(any(f.suffix == ".png" for f in calls[-1].args[3]))
            self.assertIn("shot-4.png", pipeline._path(run, "05-image-evidence.md").read_text(encoding="utf-8"))

    def test_cli_image_processing_keeps_the_existing_per_image_calls(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(paths, 'RUNS', Path(folder)):
            run = pipeline.create('CLI-IMAGES-1')
            pipeline._write(run, '01-jira.md', 'Ticket context')
            images = [Path(folder) / f'cli-{index}.png' for index in range(4)]
            with patch.object(pipeline, '_ask_ai', return_value='Visible facts') as ask:
                pipeline._image_brief(run, {**settings.DEFAULTS, 'ai_mode': 'copilot'}, Mock(), images)
            self.assertEqual(ask.call_count, 4)
            self.assertTrue(all(call.args[3][-1].name == 'image-analysis.md' for call in ask.call_args_list))

    def test_failed_m365_image_group_is_explicit_without_losing_the_other_group(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(paths, 'RUNS', Path(folder)):
            run = pipeline.create('M365-IMAGES-1')
            pipeline._write(run, '01-jira.md', 'Ticket context')
            images = [Path(folder) / f'image-{index}.png' for index in range(4)]
            with patch.object(pipeline, '_ask_ai', side_effect=[RuntimeError('vision unavailable'), 'Visible version: V2']) as ask:
                result = pipeline._image_brief(run, {**settings.DEFAULTS, 'ai_mode': 'm365'}, Mock(), images)
            text = result.read_text(encoding='utf-8')
            self.assertEqual(ask.call_count, 2)
            self.assertIn('no visual claims', text)
            self.assertIn('Visible version: V2', text)
            self.assertTrue(all(image.name in text for image in images))

    def test_natural_language_ai_output_is_normalized_without_converting_clock(self):
        focus = pipeline._parse_focus('```json\n' + json.dumps({
            "time_start": "19:38", "time_end": "19:40", "ticket_utc_offset": "UTC+7",
            "reason": "bug occurred at 19:38 - 19:40 UTC+7", "attachments": []}) + '\n```')
        self.assertEqual((focus["time_start"], focus["time_end"]), ("19:38:00", "19:40:00"))
        self.assertEqual(focus["ticket_utc_offset"], "+07:00")

    def test_auto_acquisition_works_with_no_excluded_files(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(paths, "RUNS", Path(folder)):
            run = pipeline.create("ACQUIRE-1", auto_attachments=True, gate="skip")
            run["attachments"] = [item("logs.zip"), item("shot.png")]
            run["steps"]["understand"]["status"] = "done"
            run["focus"] = {"attachments": []}
            with patch.object(pipeline, "_fetch_attachments") as fetch:
                pipeline._acquire_logs(run, {**settings.DEFAULTS, "auto_download_max_mb": 2}, Mock())
            self.assertEqual(fetch.call_args.args[1], {"logs.zip", "shot.png"})
            self.assertEqual(fetch.call_args.kwargs["max_bytes"], 2 * MB)

    def test_step_two_note_includes_effective_plan_even_if_ai_picked_nothing(self):
        from app.i18n import t

        with tempfile.TemporaryDirectory() as folder, patch.object(paths, "RUNS", Path(folder)):
            run = pipeline.create("STEP-TWO-1", auto_attachments=True)
            run["attachments"] = [item("capture.zip"), item("shot.png")]
            pipeline._write(run, "01-jira.md", "Bug happened at 19:38 - 19:40 UTC+7; see inner/main.log.")
            pipeline._write(run, "01-attachments.md", pipeline._attachment_listing(run, settings.DEFAULTS))
            answer = json.dumps({"time_start": "19:38", "time_end": "19:40", "ticket_utc_offset": "UTC+7",
                                 "attachments": []})
            with patch.object(pipeline, "_ask_ai", return_value=answer):
                note = pipeline.step_understand(run, settings.DEFAULTS, Mock())
            self.assertIn(t("note.evidence_plan", n=2), note)
            self.assertEqual(run["focus"]["ticket_utc_offset"], "+07:00")


if __name__ == "__main__":
    unittest.main()
