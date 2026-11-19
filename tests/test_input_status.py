"""Input edits may be undone until real work changes the saved results."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import paths, pipeline, settings


class InputStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / 'app.log'
        self.log.write_text('12:00:00 failure\n', encoding='utf-8')
        self.executor = pipeline.Executor()
        self.calls = []
        self.cfg = {**settings.DEFAULTS, 'ai_mode': 'copilot', 'lang': 'en'}
        for patcher in (patch.object(paths, 'RUNS', self.root / 'runs'),
                        patch.object(pipeline, '_runs', {}),
                        patch.object(settings, 'load', return_value=self.cfg),
                        patch.object(pipeline, 'executor', self.executor),
                        patch.object(pipeline, 'RUNNERS', {step: self.runner(step) for step in pipeline.ORDER})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def runner(self, step):
        def run(run, cfg, executor):
            self.calls.append(step)
            name = pipeline.ARTIFACTS[step][0][0]
            pipeline._write(run, name, f'{step}: result {len(self.calls)}')
            return f'{step} complete'
        return run

    def wait(self):
        self.executor.thread.join(5)
        self.assertFalse(self.executor.busy)

    def completed(self, graph=True):
        run = pipeline.create('INPUT-1', [str(self.log)], str(self.root), auto_attachments=False,
                              use_codegraph=graph, gate='skip', auto_publish_to_collab=False)
        self.executor.start(run['id'])
        self.wait()
        self.assertEqual(run['steps']['collab']['status'], 'waiting')
        self.calls.clear()
        return run

    def test_graph_round_trip_restores_completed_steps_notes_times_and_files(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                run = self.completed(enabled)
                original = deepcopy(run['steps'])
                session = run['session_id']
                usage = deepcopy(run['copilot_usage'])
                for _ in range(3):
                    pipeline.update_inputs(run['id'], use_codegraph=not enabled)
                    self.assertEqual(run['steps']['analyze']['status'], 'stale')
                    self.assertEqual(run['steps']['log'], original['log'])
                    pipeline.update_inputs(run['id'], use_codegraph=enabled)
                    self.assertEqual(run['steps'], original)
                self.assertEqual(self.calls, [])
                self.assertEqual(run['session_id'], session)
                self.assertEqual(run['copilot_usage'], usage)

    def test_undo_one_option_keeps_other_changed_option_stale(self):
        run = self.completed()
        original = deepcopy(run['steps'])
        pipeline.update_inputs(run['id'], use_codegraph=False)
        pipeline.update_inputs(run['id'], use_infographic=False)
        pipeline.update_inputs(run['id'], use_codegraph=True)
        for step in pipeline.ORDER[:6]:
            self.assertEqual(run['steps'][step], original[step])
        self.assertEqual(run['steps']['infographic']['status'], 'skipped')
        self.assertEqual(run['steps']['collab']['status'], 'stale')
        pipeline.update_inputs(run['id'], use_infographic=True)
        self.assertEqual(run['steps'], original)

    def test_undo_preserves_existing_staleness_errors_and_user_skips(self):
        run = self.completed()
        run['steps']['analyze']['status'] = 'stale'
        run['steps']['format'].update(status='error', error='Earlier failure')
        run['steps']['infographic'].update(status='skipped', skipped_by_user=True, note='User skipped')
        original = deepcopy(run['steps'])
        pipeline.update_inputs(run['id'], use_codegraph=False)
        pipeline.update_inputs(run['id'], use_codegraph=True)
        self.assertEqual(run['steps'], original)

    def test_noop_inputs_do_not_invalidate_or_clear_catalog(self):
        run = self.completed()
        run['log_catalog'] = {'path': 'existing catalog'}
        original = deepcopy(run['steps'])
        pipeline.update_inputs(run['id'], source_dir=run['inputs']['source_dir'],
                               log_files=run['inputs']['log_files'], logsleuth_options=run['inputs']['logsleuth'])
        self.assertEqual(run['steps'], original)
        self.assertEqual(run['log_catalog'], {'path': 'existing catalog'})

    def test_undo_survives_reload_and_baseline_is_private(self):
        run = self.completed()
        original = deepcopy(run['steps'])
        pipeline.update_inputs(run['id'], use_codegraph=False)
        self.assertNotIn('_input_baseline', pipeline.view(run))
        pipeline._runs.pop(run['id'])
        reloaded = pipeline.update_inputs(run['id'], use_codegraph=True)
        self.assertEqual(reloaded['steps'], original)

    def test_executing_changed_workflow_prevents_restoring_previous_results(self):
        run = self.completed()
        pipeline.update_inputs(run['id'], use_codegraph=False)
        self.executor.start(run['id'], 'graph')
        self.wait()
        self.assertIn('analyze', self.calls)
        pipeline.update_inputs(run['id'], use_codegraph=True)
        self.assertEqual(run['steps']['graph']['status'], 'idle')
        self.assertEqual(run['steps']['analyze']['status'], 'stale')

    def test_output_edit_cannot_be_undone_by_reverting_an_option(self):
        run = self.completed()
        pipeline.update_inputs(run['id'], use_codegraph=False)
        pipeline.write_file(run['id'], '05-analysis.md', 'New manual evidence')
        pipeline.update_inputs(run['id'], use_codegraph=True)
        self.assertEqual(run['steps']['format']['status'], 'stale')
        self.assertEqual(pipeline.read_file(run['id'], '05-analysis.md').read_text(), 'New manual evidence')

    def test_attachment_option_and_log_options_round_trip_together(self):
        run = self.completed()
        original = deepcopy(run['steps'])
        run['gate_state'] = {'skip_logs': True, 'resolved': True}
        pipeline.update_inputs(run['id'], auto_attachments=True)
        pipeline.update_inputs(run['id'], log_shift_minutes=60)
        pipeline.update_inputs(run['id'], auto_attachments=False)
        self.assertEqual(run['steps']['log']['status'], 'stale')
        pipeline.update_inputs(run['id'], log_shift_minutes=None)
        self.assertEqual(run['steps'], original)
        self.assertEqual(run['gate_state'], {'skip_logs': True, 'resolved': True})

    def test_processing_mode_round_trip_restores_pending_review(self):
        run = self.completed()
        run['inputs']['gate'] = 'review'
        run['steps']['log'].update(status='waiting', note='Review evidence')
        run['pending_gate'] = {'candidates': [], 'problem': ''}
        run['gate_state'] = {'resolved': True}
        original = deepcopy(run['steps'])
        pipeline.update_inputs(run['id'], gate='manual')
        self.assertNotIn('pending_gate', run)
        pipeline.update_inputs(run['id'], gate='review')
        self.assertEqual(run['steps'], original)
        self.assertIn('pending_gate', run)
        self.assertTrue(run['gate_state']['resolved'])


if __name__ == '__main__':
    unittest.main()
