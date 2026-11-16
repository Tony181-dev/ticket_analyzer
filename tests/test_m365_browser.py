"""M365 composer: the prompt must really be in the box before Enter (attachments alone are never sent)."""
import sys
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


if __name__ == "__main__":
    unittest.main()
