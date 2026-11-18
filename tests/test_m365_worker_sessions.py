"""Exercise the worker JSON protocol with a fake browser and real persisted session records."""
import atexit
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from app.m365_worker.sessions import Store, clean_unicode

WORKER = Path(__file__).resolve().parent.parent / 'app' / 'm365_worker' / 'worker.py'
MODEL = 'GPT-5.6 Sol Think Deeper'


class WorkerSessionModelTest(unittest.TestCase):
    def request(self, number, session='workflow-one', model=MODEL):
        return {'id': number, 'action': 'chat', 'payload': {
            'session': {'id': session}, 'create_session': True, 'm365_model': model,
            'messages': [{'role': 'user', 'content': 'Analyze this step'}]}}

    def execute(self, folder, requests, fail_first=False):
        calls, forgotten = [], []

        class FakeBrowser:
            images = []

            def close(self):
                pass

            def auth(self, **kwargs):
                return True

            def forget_session_model(self, session_id):
                forgotten.append(session_id)

            def chat(self, prompt, url, files, headless, model, resume, image_dir, **kwargs):
                calls.append({'model': model, 'url': url, **kwargs})
                if fail_first and len(calls) == 1:
                    raise RuntimeError('Model selection failed')
                chosen = kwargs.get('selected_model') or model
                return 'Answer', 'https://m365.cloud.microsoft/chat/' + kwargs['session_id'], chosen

        browser_module = types.ModuleType('browser')
        browser_module.Browser, browser_module.PROFILE, browser_module.DEFAULT_MODEL = FakeBrowser, folder / 'profile', MODEL
        browser_module.PROFILE.mkdir(exist_ok=True)
        session_module = types.ModuleType('sessions')
        session_module.Store, session_module.clean_unicode = Store, clean_unicode
        attachment_module = types.ModuleType('attachments')
        attachment_module.validate = lambda files: []
        attachment_module.transport_files = lambda files, folder: ([], '')
        output = io.StringIO()
        output.buffer = io.BytesIO()
        with patch.dict(sys.modules, browser=browser_module, sessions=session_module, attachments=attachment_module), \
                patch.dict(os.environ, M365_SESSIONS_DIR=str(folder / 'sessions')), \
                patch.object(sys, 'stdin', io.StringIO('\n'.join(json.dumps(request) for request in requests))), \
                patch.object(sys, 'stdout', output), patch.object(sys, 'stderr', io.StringIO()):
            namespace = runpy.run_path(str(WORKER), run_name='__worker_session_test__')
        atexit.unregister(namespace['b'].close)
        replies = [json.loads(line) for line in output.buffer.getvalue().decode('utf-8').splitlines()]
        return replies, calls, forgotten

    def test_first_prompt_selects_then_later_steps_and_worker_restart_keep_confirmed_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            replies, calls, forgotten = self.execute(folder, [self.request(1), self.request(2, model='Changed settings')])
            self.assertTrue(all(reply['ok'] for reply in replies), replies)
            self.assertIsNone(calls[0]['selected_model'])
            self.assertEqual(calls[1]['selected_model'], MODEL)
            self.assertEqual(calls[0]['session_id'], calls[1]['session_id'])
            self.assertEqual(forgotten, ['workflow-one'])
            replies, calls, forgotten = self.execute(folder, [self.request(3, model='Another setting')])
            self.assertTrue(replies[0]['ok'], replies)
            self.assertEqual(calls[0]['selected_model'], MODEL)
            self.assertEqual(replies[0]['result']['m365_model'], MODEL)
            self.assertEqual(forgotten, [])

    def test_failed_first_selection_is_not_treated_as_confirmed_and_new_session_selects_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            replies, calls, forgotten = self.execute(Path(temporary), [
                self.request(1), self.request(2), self.request(3), self.request(4, session='workflow-two')], fail_first=True)
            self.assertFalse(replies[0]['ok'])
            self.assertTrue(all(reply['ok'] for reply in replies[1:]), replies)
            self.assertEqual([call['selected_model'] for call in calls], [None, None, MODEL, None])
            self.assertEqual(forgotten, ['workflow-one', 'workflow-two'])


if __name__ == '__main__':
    unittest.main()
