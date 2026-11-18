"""Public CLI trace persistence, run isolation and reset behavior (no external AI calls)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('TA_DATA_DIR', tempfile.mkdtemp(prefix='ta-reasoning-'))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import pipeline, paths, reasoning, settings
from app.ai import copilot


class ReasoningTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        for target, name, value in [(paths, 'RUNS', Path(self.folder.name) / 'runs'),
                                    (pipeline, '_runs', {})]:
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg = {**settings.DEFAULTS, 'ai_mode': 'copilot'}
        patcher = patch.object(settings, 'load', return_value=self.cfg)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.run = pipeline.create('TRACE-1')

    def emit(self, callback, key='r1', text='Inspecting evidence'):
        callback({'id': key, 'call_id': 'c1', 'kind': 'reasoning', 'time': 10, 'text': text})

    def test_step_and_pass_are_captured_at_call_start_and_snapshots_replace(self):
        self.run['current_step'] = 'analyze'
        self.run['usage_passes'] = [{'n': 2}]
        callback = pipeline.reasoning_callback(self.run)
        self.run['current_step'] = 'format'
        self.emit(callback, text='Inspecting')
        self.emit(callback, text='Inspecting evidence')
        records = pipeline.reasoning_events(self.run['id'])
        self.assertEqual(len(records), 1)
        self.assertEqual((records[0]['step'], records[0]['pass']), ('analyze', 2))
        self.assertEqual(records[0]['text'], 'Inspecting evidence')
        # A fresh load from run.json uses the same journal.
        pipeline._save(self.run)
        pipeline._runs.clear()
        self.assertEqual(pipeline.reasoning_events(self.run['id']), records)

    def test_reset_removes_trace_and_drops_late_events_from_old_calls(self):
        callback = pipeline.reasoning_callback(self.run, 'assistant')
        self.emit(callback)
        self.assertTrue(pipeline.reasoning_events(self.run['id']))
        pipeline.reset_workflow(self.run['id'])
        self.emit(callback, key='late')
        self.assertEqual(pipeline.reasoning_events(self.run['id']), [])
        self.assertFalse((pipeline.run_dir(self.run['id']) / reasoning.NAME).exists())

    def test_delete_does_not_recreate_journal_and_other_runs_stay_isolated(self):
        callback = pipeline.reasoning_callback(self.run)
        other = pipeline.create('TRACE-2')
        self.emit(callback)
        self.assertEqual(pipeline.reasoning_events(other['id']), [])
        pipeline.delete_run(self.run['id'])
        self.emit(callback)
        self.assertFalse((paths.RUNS / self.run['id']).exists())

    def test_m365_web_has_no_cli_trace_but_local_assistant_can_emit(self):
        self.run['ai_mode'] = 'm365'
        with patch.object(pipeline.ai, 'ask', return_value='Web answer'):
            self.assertEqual(pipeline._ask_ai(self.run, {'ai_mode': 'm365'}, 'prompt', [], self.folder.name), 'Web answer')
        self.assertEqual(pipeline.reasoning_events(self.run['id']), [])
        with copilot.observe(pipeline.reasoning_callback(self.run, 'assistant')):
            progress = copilot._Progress(copilot._observer.get())
            progress.line(json.dumps({'type': 'assistant.intent', 'data': {'intent': 'Reading local context'}}))
        self.assertEqual(pipeline.reasoning_events(self.run['id'])[0]['step'], 'assistant')

    def test_journal_is_bounded_even_with_large_snapshots_and_recovers_invalid_lines(self):
        folder = Path(self.folder.name)
        with patch.object(reasoning, 'MAX_BYTES', 1000), patch.object(reasoning, 'MAX_EVENTS', 10):
            for n in range(60):
                reasoning.append(folder, {'id': str(n), 'text': 'x' * 200})
                self.assertLessEqual((folder / reasoning.NAME).stat().st_size, 1000)
            with (folder / reasoning.NAME).open('a', encoding='utf-8') as stream:
                stream.write('not json\n')
            records = reasoning.read(folder)
            self.assertEqual(records[-1]['id'], '59')
            self.assertLessEqual(len(records), 10)


if __name__ == '__main__':
    unittest.main()
