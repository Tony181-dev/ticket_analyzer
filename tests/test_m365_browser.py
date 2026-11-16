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


if __name__ == "__main__":
    unittest.main()
