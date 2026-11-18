"""Skipping a step continues safely, including cancellation and evidence/publishing gates."""
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault('TA_DATA_DIR', tempfile.mkdtemp(prefix='ta-skip-'))
from app import paths, pipeline, settings


class StepSkipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = {**settings.DEFAULTS, 'ai_mode': 'copilot', 'lang': 'en'}
        self.executor = pipeline.Executor()
        self.calls = []
        runners = {step: self.runner(step) for step in pipeline.ORDER}
        for patcher in (patch.object(paths, 'RUNS', Path(self.tmp.name) / 'runs'),
                        patch.object(pipeline, '_runs', {}), patch.object(settings, 'load', return_value=self.cfg),
                        patch.object(pipeline, 'executor', self.executor), patch.object(pipeline, 'RUNNERS', runners),
                        patch.object(pipeline, 'APPLICABLE', {step: lambda run: True for step in pipeline.ORDER}),
                        patch.object(pipeline.ai, 'cancel')):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.run = self.create('SKIP-1')

    def create(self, issue):
        return pipeline.create(issue, gate='skip', auto_publish_to_collab=True)

    def runner(self, step):
        def run(*args):
            self.calls.append(step)
            return 'ok'
        return run

    def wait(self):
        if self.executor.thread:
            self.executor.thread.join(5)
        self.assertFalse(self.executor.busy)

    def test_each_idle_step_can_be_skipped_without_executing_it(self):
        for index, step in enumerate(pipeline.ORDER):
            with self.subTest(step=step):
                run = self.create(f'SKIP-{index + 10}')
                self.calls.clear()
                self.executor.skip_step(run['id'], step)
                self.wait()
                self.assertEqual(self.calls, pipeline.ORDER[index + 1:])
                self.assertEqual(run['steps'][step]['status'], 'skipped')
                self.assertTrue(run['steps'][step]['skipped_by_user'])

    def test_running_step_finishes_unwinding_before_next_step_starts(self):
        entered, release, unwound = threading.Event(), threading.Event(), threading.Event()

        def slow(run, cfg, executor):
            self.calls.append('analyze')
            entered.set()
            executor.cancel.wait(3)
            release.wait(3)  # simulate tool cleanup after cancellation
            unwound.set()
            executor.check()

        def next_step(*args):
            self.assertTrue(unwound.is_set())
            self.calls.append('format')
            return 'ok'

        with patch.dict(pipeline.RUNNERS, analyze=slow, format=next_step):
            self.executor.start(self.run['id'], 'analyze', only=True)
            self.assertTrue(entered.wait(2))
            self.executor.skip_step(self.run['id'], 'analyze')
            self.assertEqual(self.calls, ['analyze'])
            self.assertTrue(self.executor.busy)
            release.set()
            self.wait()
        self.assertEqual(self.calls, ['analyze', 'format', 'infographic', 'collab'])
        self.assertEqual(self.run['steps']['analyze']['status'], 'skipped')
        self.assertFalse(self.executor.cancel.is_set())

    def test_stop_overrides_a_pending_skip(self):
        entered, release = threading.Event(), threading.Event()

        def slow(run, cfg, executor):
            entered.set()
            executor.cancel.wait(3)
            release.wait(3)
            executor.check()

        with patch.dict(pipeline.RUNNERS, analyze=slow):
            self.executor.start(self.run['id'], 'analyze')
            self.assertTrue(entered.wait(2))
            self.executor.skip_step(self.run['id'], 'analyze')
            self.executor.stop()
            release.set()
            self.wait()
        self.assertEqual(self.calls, [])
        self.assertFalse(self.run['steps']['analyze'].get('skipped_by_user'))

    def test_skip_ignores_stale_prerequisites_and_survives_input_refresh(self):
        self.run['steps']['jira']['status'] = 'stale'
        self.executor.skip_step(self.run['id'], 'understand')
        self.wait()
        self.assertNotIn('jira', self.calls)
        self.assertNotIn('understand', self.calls)
        pipeline._refresh_skipped(self.run)
        self.assertEqual(self.run['steps']['understand']['status'], 'skipped')
        self.calls.clear()
        self.executor.start(self.run['id'])
        self.wait()
        self.assertIn('understand', self.calls, 'an explicit full rerun tries skipped steps again')

    def test_waiting_log_gate_and_timezone_are_removed_on_skip(self):
        self.run['inputs']['gate'] = 'review'
        self.run['steps']['log']['status'] = 'waiting'
        self.run['pending_gate'] = {'mode': 'review'}
        self.run['pending_tz'] = {'mode': 'manual'}
        self.executor.skip_step(self.run['id'], 'log')
        self.wait()
        self.assertEqual(self.calls, ['graph', 'analyze', 'format', 'infographic', 'collab'])
        self.assertNotIn('pending_gate', self.run)
        self.assertNotIn('pending_tz', self.run)

    def test_skip_to_collab_respects_manual_publish_and_last_step_has_no_runner(self):
        self.run['inputs']['auto_publish_to_collab'] = False
        self.executor.skip_step(self.run['id'], 'infographic')
        self.wait()
        self.assertEqual(self.run['steps']['collab']['status'], 'waiting')
        self.assertEqual(self.calls, [])
        self.executor.skip_step(self.run['id'], 'collab')
        self.wait()
        self.assertEqual(self.run['steps']['collab']['status'], 'skipped')
        self.assertEqual(self.calls, [])

    def test_skipping_does_not_hide_a_failure_in_the_next_step(self):
        with patch.dict(pipeline.RUNNERS, format=lambda *args: (_ for _ in ()).throw(RuntimeError('Missing analysis'))):
            self.executor.skip_step(self.run['id'], 'analyze')
            self.wait()
        self.assertEqual(self.run['steps']['format']['status'], 'error')
        self.assertIn('Missing analysis', self.run['steps']['format']['error'])
        self.assertEqual(self.calls, [])

    def test_other_step_or_run_cannot_be_skipped_while_a_step_runs(self):
        entered, release = threading.Event(), threading.Event()

        def slow(*args):
            entered.set()
            release.wait(3)
            return 'ok'

        other = self.create('OTHER-1')
        with patch.dict(pipeline.RUNNERS, analyze=slow):
            self.executor.start(self.run['id'], 'analyze', only=True)
            self.assertTrue(entered.wait(2))
            with self.assertRaises(RuntimeError):
                self.executor.skip_step(other['id'], 'analyze')
            with self.assertRaises(RuntimeError):
                self.executor.skip_step(self.run['id'], 'format')
            release.set()
            self.wait()
        with self.assertRaises(ValueError):
            self.executor.skip_step(self.run['id'], 'unknown')


if __name__ == '__main__':
    unittest.main()
