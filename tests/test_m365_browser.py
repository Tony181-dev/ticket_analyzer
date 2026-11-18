"""M365 composer: the prompt must really be in the box before Enter (attachments alone are never sent)."""
import sys
import tempfile
from unittest import mock
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app" / "m365_worker"))

try:
    import browser  # noqa: E402
except ImportError:  # Playwright not installed: nothing to test here
    browser = None


class FakeKeyboard:
    def __init__(self, box):
        self.box = box

    def insert_text(self, text):
        if self.box.accept_insert:
            self.box.text = text


class FakePage:
    def __init__(self, box):
        self.keyboard = FakeKeyboard(box)

    def wait_for_timeout(self, ms):
        pass


class FakeBox:
    """A rich editor that may silently ignore fill() / insert_text()."""
    def __init__(self, accept_fill=True, accept_insert=True, accept_keys=True):
        self.text, self.accept_fill, self.accept_insert, self.accept_keys = "", accept_fill, accept_insert, accept_keys

    def click(self):
        pass

    def fill(self, text):
        if self.accept_fill:
            self.text = text

    def press(self, key):
        if key == "Backspace":
            self.text = ""

    def press_sequentially(self, text, delay=0):
        if self.accept_keys:
            self.text = text

    def evaluate(self, script):
        return "DIV"

    def inner_text(self):
        return self.text


@unittest.skipIf(browser is None, "playwright not installed")
class ComposerTest(unittest.TestCase):
    def type(self, box, prompt="Jira issue ABC-1. Follow understand.md."):
        b = browser.Browser.__new__(browser.Browser)
        return b.type_prompt(FakePage(box), box, prompt), box.text

    def test_fill_that_lands_is_enough(self):
        self.assertEqual(self.type(FakeBox()), (True, "Jira issue ABC-1. Follow understand.md."))

    def test_ignored_fill_falls_back_to_insert_then_keys(self):
        self.assertTrue(self.type(FakeBox(accept_fill=False))[0])
        self.assertTrue(self.type(FakeBox(accept_fill=False, accept_insert=False))[0])

    def test_an_empty_composer_is_reported(self):
        ok, text = self.type(FakeBox(accept_fill=False, accept_insert=False, accept_keys=False))
        self.assertFalse(ok)
        self.assertEqual(text, "")

    def test_default_prompt_is_not_empty(self):
        self.assertTrue(browser.DEFAULT_PROMPT.strip())

    def test_a_truncated_prompt_is_not_accepted(self):
        class TruncatedBox(FakeBox):
            def fill(self, text):
                self.text = text[:30]

        self.assertTrue(self.type(TruncatedBox())[0], "a partial fill must be replaced by complete keyboard input")
        self.assertFalse(self.type(TruncatedBox(accept_insert=False, accept_keys=False))[0])

    def test_closed_before_send_recovers_once_with_the_same_model_and_conversation(self):
        worker = browser.Browser()
        with mock.patch.object(worker, '_chat_once', side_effect=[browser.BrowserClosedError('closed'), ('answer', 'url', 'model')]) as chat:
            result = worker.chat('prompt', 'https://m365.cloud.microsoft/chat/conversation', ['ticket.txt'], False, 'Chosen model')
        self.assertEqual(result[0], 'answer')
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(chat.call_args.args[:5], ('prompt', 'https://m365.cloud.microsoft/chat/conversation', ['ticket.txt'], False, 'Chosen model'))

    def test_closed_after_send_never_automatically_resends(self):
        worker = browser.Browser()

        def closed_after_send(*args):
            worker._submission_attempted = True
            raise browser.BrowserClosedError('closed')

        with mock.patch.object(worker, '_chat_once', side_effect=closed_after_send) as chat:
            with self.assertRaisesRegex(RuntimeError, 'M365_SEND_UNCERTAIN'):
                worker.chat('prompt')
        self.assertEqual(chat.call_count, 1)

    def test_selecting_chat_keeps_other_tabs_and_authentication_popups_open(self):
        worker = browser.Browser()
        chat = mock.Mock(url='https://m365.cloud.microsoft/chat/conversation')
        login = mock.Mock(url='https://login.microsoftonline.com/oauth2/authorize')
        for page in (chat, login):
            page.is_closed.return_value = False
        worker.ctx = mock.Mock(pages=[login, chat])
        self.assertIs(worker._pick_page(), chat)
        chat.close.assert_not_called()
        login.close.assert_not_called()


FIXTURE = ROOT / "tests" / "fixtures" / "m365_composer.html"


@unittest.skipIf(browser is None, "playwright not installed")
class LivePageTest(unittest.TestCase):
    """The worker against a copy of the real picker / composer markup, in a real browser."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.browser = None
        for options in ({}, {"channel": "msedge"}, {"channel": "chrome"}):
            try:
                cls.browser = cls.pw.chromium.launch(**options)
                break
            except Exception:
                continue
        if cls.browser is None:
            cls.pw.stop()
            raise unittest.SkipTest("no Chromium-based browser available")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_content(FIXTURE.read_text(encoding="utf-8"))
        self.worker = browser.Browser.__new__(browser.Browser)

    def tearDown(self):
        self.page.close()

    def clicks(self):
        return self.page.evaluate("window.clicks")

    def test_localized_labels_match_and_the_active_model_is_left_alone(self):
        self.assertEqual(browser._model_key("GPT-5.6 Sol Tư duy sâu"), browser._model_key("GPT-5.6 Sol Think Deeper"))
        self.assertNotEqual(browser._model_key("GPT-5.6 Sol Tư duy"), browser._model_key("GPT-5.6 Sol Think Deeper"))
        self.worker.select_model(self.page, "GPT-5.6 Sol Think Deeper")
        self.assertEqual(self.clicks(), [], "already active (shown in Vietnamese): nothing is clicked")

    def test_a_model_in_the_vendor_submenu_is_selected(self):
        self.worker.select_model(self.page, "GPT-5.6 Sol Think")
        self.assertEqual(self.clicks(), ["GPT-5.6 Sol Tư duy"])
        self.assertEqual(self.page.inner_text("#current"), "GPT-5.6 Sol Tư duy")

    def test_mode_radios_and_the_plus_menu_are_never_clicked(self):
        with self.assertRaises(RuntimeError):
            self.worker.select_model(self.page, "Claude Opus", attempts=1)
        self.assertEqual([c for c in self.clicks() if c in ("Tự động", "Phản hồi nhanh", "Tư duy sâu", "PLUS")], [])

    def test_composer_prompt_and_send_button(self):
        box = self.worker.composer(self.page)
        self.assertEqual(box.get_attribute("id"), "m365-chat-editor-target-element")
        self.assertTrue(self.worker.type_prompt(self.page, box, "Thực hiện phân tích đi. Jira issue ABC-1."))
        self.assertIn("Thực hiện phân tích", self.worker.composer_text(box))
        self.assertTrue(self.worker.click_send(self.page))
        self.assertEqual(self.clicks(), ["SEND"])

    def test_markdown_upload_waits_for_send_to_enable_and_clicks_instead_of_newline(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / '01-jira.md'
            file.write_text('# Jira evidence\nBluetooth fails after pairing.', encoding='utf-8')
            self.worker.upload(self.page, str(file), timeout=5)
        self.page.evaluate('enableSending({readyAfter: 1200, remount: true})')
        self.worker.send_prompt(self.page, 'Analyze Jira ABC-1 using the attached Markdown.', [], timeout=5)
        self.assertEqual(self.page.evaluate('window.sentPrompts'), ['Analyze Jira ABC-1 using the attached Markdown.'])
        self.assertEqual(self.clicks(), ['SEND'], 'Enter is a newline in this tenant; use the enabled Send button')
        self.assertEqual(self.worker.draft_state(self.page)['attachments'], 0)

    def test_prompt_is_present_before_upload_and_remains_when_upload_fails(self):
        from unittest import mock

        prompt = 'Analyze Jira ABC-1 using the attached Markdown.'

        def blocked_upload(page, file):
            self.assertEqual(self.worker.composer_text(self.worker.composer(page)), prompt)
            raise RuntimeError('Attachment upload did not complete: large.md (2,000,000 B)')

        with mock.patch.object(self.worker, 'open_chat_page', return_value=self.page), \
                mock.patch.object(self.worker, 'select_model', return_value='Test model'), \
                mock.patch.object(self.worker, 'upload', side_effect=blocked_upload):
            with self.assertRaisesRegex(RuntimeError, 'large.md'):
                self.worker.chat(prompt, files=['large.md'])
        self.assertEqual(self.worker.composer_text(self.worker.composer(self.page)), prompt)
        self.assertEqual(self.clicks(), [])

    def test_upload_rejection_reports_filename_and_size_without_waiting_for_send(self):
        self.page.evaluate("""() => {
          window.rejectUploads = true;
          const alert = document.createElement('div');
          alert.role = 'alert'; alert.textContent = 'File too large to upload';
          document.querySelector('.fai-BebopLiteChatInput__inputWrapper').appendChild(alert);
        }""")
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'large.md'
            file.write_text('large', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, r'Attachment rejected: large.md \(5 B\)'):
                self.worker.upload(self.page, str(file), timeout=3)
        self.assertEqual(self.clicks(), [])

    def test_reported_remove_attachments_tooltip_is_detected_without_a_file_chip(self):
        self.page.evaluate("""() => {
          window.rejectUploads = true;
          const send = document.querySelector('.fai-SendButton');
          send.disabled = true;
          send.title = 'Remove attachments to send';
        }""")
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / '01-jira.md'
            file.write_bytes(b'x' * 112617)
            with self.assertRaisesRegex(RuntimeError, r'M365_ATTACHMENT_BLOCKED:.*01-jira.md.*112,617 B.*Remove attachments to send'):
                self.worker.upload(self.page, str(file), timeout=2)
        self.assertEqual(self.clicks(), [])

    def test_wrong_image_picker_is_not_used_for_ticket_text(self):
        self.page.evaluate("""() => {
          document.getElementById('fileInput').accept = '.txt,.md';
          const images = document.createElement('input');
          images.type = 'file'; images.accept = 'image/*';
          document.body.appendChild(images);
        }""")
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / '01-jira.md.txt'
            file.write_text('Jira content', encoding='utf-8')
            self.worker.upload(self.page, str(file), timeout=4)
        self.assertEqual(self.page.locator('#fileInput').evaluate('el => el.files[0].name'), '01-jira.md.txt')

    def test_lazy_send_tooltip_outside_the_composer_is_revealed_and_reported(self):
        self.page.evaluate("""() => {
          window.rejectUploads = true;
          const send = document.querySelector('.fai-SendButton');
          send.disabled = true;
          send.addEventListener('mouseenter', () => {
            const tooltip = document.createElement('div');
            tooltip.role = 'tooltip'; tooltip.textContent = 'Remove attachments to send';
            document.body.appendChild(tooltip);
          });
        }""")
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / '01-jira.md.txt'
            file.write_text('Jira evidence', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'M365_ATTACHMENT_BLOCKED:.*Remove attachments to send'):
                self.worker.upload(self.page, str(file), timeout=4)

    def test_blocker_phrase_inside_the_prompt_is_not_a_ui_rejection(self):
        self.page.evaluate('enableSending()')
        prompt = 'Investigate the user report: Remove attachments to send.'
        self.worker.send_prompt(self.page, prompt, [], timeout=4)
        self.assertEqual(self.page.evaluate('window.sentPrompts'), [prompt])

    def test_upload_on_a_closed_page_fails_immediately(self):
        self.page.close()
        with self.assertRaises(browser.BrowserClosedError):
            self.worker.upload(self.page, '01-jira.md', timeout=60)

    def test_unrelated_busy_indicator_does_not_mean_the_prompt_was_sent(self):
        self.page.evaluate('enableSending({acknowledge: false})')
        self.assertFalse(self.worker.busy(self.page), 'a busy history widget is not assistant generation')
        with self.assertRaisesRegex(RuntimeError, 'PROMPT_NOT_SENT'):
            self.worker.send_prompt(self.page, 'Analyze the attached file.', [], timeout=1)
        self.assertEqual(self.page.evaluate('window.sentPrompts'), [])

    def test_a_missing_editor_after_click_is_not_a_successful_send(self):
        self.page.evaluate("() => { document.querySelector('.fai-SendButton').onclick = () => document.getElementById('m365-chat-editor-target-element').remove(); }")
        with self.assertRaisesRegex(RuntimeError, 'PROMPT_NOT_SENT'):
            self.worker.send_prompt(self.page, 'Analyze the attached file.', [], timeout=1)

    def test_disabled_send_is_never_pressed_as_a_keyboard_fallback(self):
        self.page.evaluate('enableSending({readyAfter: 60000})')
        with self.assertRaisesRegex(RuntimeError, 'PROMPT_NOT_SENT'):
            self.worker.send_prompt(self.page, 'Analyze the attached file.', [], timeout=1)
        self.assertEqual(self.clicks(), [])

    def test_actual_assistant_streaming_is_busy(self):
        self.page.evaluate("""() => {
          const response = document.createElement('div');
          response.dataset.messageAuthorRole = 'assistant';
          response.setAttribute('aria-busy', 'true');
          response.textContent = 'Thinking';
          document.body.appendChild(response);
        }""")
        self.assertTrue(self.worker.busy(self.page))

    def test_upload_still_processing_blocks_submission(self):
        self.page.evaluate("""() => {
          enableSending();
          document.querySelector('.fai-BebopLiteChatInput__attachments').innerHTML = '<div role="progressbar">Processing file</div>';
        }""")
        with self.assertRaisesRegex(RuntimeError, 'PROMPT_NOT_SENT: files still processing'):
            self.worker.send_prompt(self.page, 'Analyze the attached file.', [], timeout=1)
        self.assertEqual(self.clicks(), [])

    def test_editor_remounted_during_typing_is_reacquired(self):
        self.page.evaluate("""() => {
          enableSending();
          const editor = document.getElementById('m365-chat-editor-target-element');
          editor.addEventListener('input', () => {
            const replacement = editor.cloneNode(false);
            replacement.innerHTML = '<p><br></p>';
            editor.replaceWith(replacement);
          }, {once: true});
        }""")
        self.worker.send_prompt(self.page, 'Analyze the attached file.', [], timeout=5)
        self.assertEqual(self.page.evaluate('window.sentPrompts'), ['Analyze the attached file.'])

    def test_visible_text_with_empty_application_state_is_reentered_via_keyboard(self):
        self.page.evaluate("""() => {
          enableSending({readyAfter: 60000});
          let inputs = 0;
          document.getElementById('m365-chat-editor-target-element').addEventListener('input', () => {
            if (++inputs > 1) document.querySelector('.fai-SendButton').disabled = false;
          });
        }""")
        self.worker.send_prompt(self.page, 'Analyze the attached file.', [], timeout=8)
        self.assertEqual(self.page.evaluate('window.sentPrompts'), ['Analyze the attached file.'])

    def test_unrelated_submit_buttons_are_not_used(self):
        self.page.evaluate("""() => {
          const other = document.createElement('button');
          other.type = 'submit'; other.textContent = 'Save settings';
          other.onclick = () => window.clicks.push('WRONG_FORM');
          document.body.prepend(other);
          document.querySelector('.fai-SendButton').removeAttribute('class');
          document.querySelector('[aria-label="Gửi"]').removeAttribute('aria-label');
          document.querySelector('.fai-BebopLiteChatInput__inputWrapper button[type=submit]').onclick = () => window.clicks.push('RIGHT_FORM');
        }""")
        self.assertTrue(self.worker.click_send(self.page))
        self.assertEqual(self.clicks(), ['RIGHT_FORM'])

    def test_chat_waits_for_submission_before_collecting_the_answer(self):
        from unittest import mock

        self.page.evaluate("""() => {
          enableSending({readyAfter: 700});
          document.querySelector('.fai-SendButton').addEventListener('click', () => {
            if (!window.sentPrompts.length) return;
            const answer = document.createElement('div');
            answer.dataset.messageAuthorRole = 'assistant';
            answer.textContent = 'Analysis completed from Markdown evidence.';
            document.body.appendChild(answer);
          });
        }""")
        with mock.patch.object(self.worker, 'open_chat_page', return_value=self.page), \
                mock.patch.object(self.worker, 'select_model', return_value='Test model'), \
                mock.patch.object(self.worker, 'conversation_url', return_value='https://example.test/chat'):
            answer, _, _ = self.worker.chat('Analyze the attached Markdown.')
        self.assertEqual(answer, 'Analysis completed from Markdown evidence.')
        self.assertEqual(self.page.evaluate('window.sentPrompts'), ['Analyze the attached Markdown.'])


if __name__ == "__main__":
    unittest.main()
