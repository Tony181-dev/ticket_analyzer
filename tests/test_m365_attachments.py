"""M365 Copilot accepts at most 3 attachments per prompt: extra evidence is combined, never dropped."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app" / "m365_worker"))

from app import ai  # noqa: E402
import attachments as worker_attachments  # noqa: E402  (app/m365_worker/attachments.py)

REAL_ASK = ai.ask  # other test modules swap ai.ask for fakes at class setup


class PackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, name, text="x", data=None):
        path = self.dir / name
        if data is None:
            path.write_text(text, encoding="utf-8")
        else:
            path.write_bytes(data)
        return path

    def analyze_files(self):
        """The Analyze step's attachments: five evidence files, the instruction last."""
        return [self.make("01-jira.md", "JIRA " * 50), self.make("02-understanding.md", "UNDERSTAND " * 20),
                self.make("03-log-brief.md", "LOG " * 400), self.make("04-graph.simple.json", '{"nodes": []}' * 30),
                self.make("05-source-evidence.md", "SRC " * 100), self.make("ticket-analysis.md", "INSTRUCTION")]

    def test_three_or_fewer_files_are_sent_as_they_are(self):
        files = [self.make("a.md"), self.make("b.md"), self.make("c.md")]
        self.assertEqual(ai.m365_attachments(files, self.dir), (files, ""))

    def test_more_than_three_files_are_combined_into_three_attachments(self):
        files = self.analyze_files()
        sent, note = ai.m365_attachments(files, self.dir)
        self.assertEqual(len(sent), ai.M365_MAX_FILES)
        self.assertEqual(sent[-1], files[-1], "the instruction stays a file of its own")
        combined = "".join(Path(f).read_text(encoding="utf-8") for f in sent)
        for original in files:
            self.assertIn(original.read_text(encoding="utf-8"), combined, original.name + " must not be lost")
        for original in files[:-1]:
            self.assertIn(original.name, note, "the prompt says where each file went")
        bundles = [Path(f) for f in sent if Path(f).parent != self.dir]
        self.assertTrue(bundles)
        for bundle in bundles:
            text = bundle.read_text(encoding="utf-8")
            self.assertIn("===== FILE: ", text)
            self.assertIn(bundle.name, note)

    def test_files_keep_their_order_and_bundles_are_balanced(self):
        files = self.analyze_files()
        sent, _ = ai.m365_attachments(files, self.dir)
        order = []
        for f in sent:
            text = Path(f).read_text(encoding="utf-8")
            order += [o.name for o in files if o.read_text(encoding="utf-8") in text and o.name not in order]
        self.assertEqual(order, [f.name for f in files])
        # the 1.6 KB log brief is the biggest file: it must not end up with all the others
        self.assertLess(max(Path(f).stat().st_size for f in sent[:-1]), sum(f.stat().st_size for f in files[:-1]))

    def test_a_binary_file_is_never_merged_into_a_bundle(self):
        files = [self.make("a.md"), self.make("shot.png", data=b"\x89PNG\r\n\x1a\n\0\0data"), self.make("b.md"),
                 self.make("c.md"), self.make("instruction.md")]
        sent, _ = ai.m365_attachments(files, self.dir)
        self.assertEqual(len(sent), 3)
        self.assertIn(files[1], sent)

    def test_too_many_binary_files_is_a_clear_error(self):
        files = [self.make(f"{n}.png", data=b"\0\0" + bytes([n])) for n in range(4)]
        with self.assertRaisesRegex(RuntimeError, "3"):
            ai.m365_attachments(files, self.dir)

    def test_image_without_nul_bytes_is_not_mistaken_for_text(self):
        image = self.make("shot.jpg", data=b"\xff\xd8" + b"x" * 9000)
        self.assertFalse(ai._is_text(image))
        files = [self.make("a.md"), image, self.make("b.md"), self.make("instruction.md")]
        sent, _ = ai.m365_attachments(files, self.dir)
        self.assertIn(image, sent)

    def test_m365_ask_sends_at_most_three_files_and_the_note(self):
        files = self.analyze_files()
        with patch.object(ai.m365_worker, "chat", return_value="answer") as chat:
            self.assertEqual(REAL_ASK({"ai_mode": "m365"}, "Analyze.", files, self.dir), "answer")
        prompt, sent = chat.call_args.args[:2]
        self.assertLessEqual(len(sent), 3)
        self.assertTrue(prompt.startswith("Analyze."))
        self.assertIn("===== FILE: ", prompt)

    def test_worker_rejects_more_than_three_attachments(self):
        files = [self.make(f"{n}.md") for n in range(4)]
        with self.assertRaisesRegex(ValueError, "3"):
            worker_attachments.validate([{"path": str(f)} for f in files])
        self.assertEqual(len(worker_attachments.validate([{"path": str(f)} for f in files[:3]])), 3)

    def test_oversized_markdown_error_identifies_the_file_and_actual_size(self):
        file = self.make('large-log-brief.md', data=b'x' * (10485760 + 1))
        with self.assertRaisesRegex(ValueError, r'large-log-brief.md.*10,485,761 B.*10 MiB'):
            worker_attachments.validate([{'path': str(file)}])

    def test_reported_112617_byte_markdown_is_preserved_in_txt_transport(self):
        original = self.make('01-jira.md', data=b'# Jira ticket\n' + b'a' * (112617 - 14))
        validated = worker_attachments.validate([{'path': str(original)}])
        sent, note = worker_attachments.transport_files(validated, self.dir / 'transport')
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].endswith('.md.txt'))
        self.assertEqual(Path(sent[0]).read_bytes(), original.read_bytes())
        self.assertEqual(original.stat().st_size, 112617)
        self.assertIn('01-jira.md -> ', note)

    def test_transport_retains_binary_files_and_distinguishes_duplicate_names(self):
        first = self.make('same.md', 'First ticket — tiếng Việt')
        other = self.dir / 'another' / 'same.md'
        other.parent.mkdir()
        other.write_text('Second ticket', encoding='utf-8')
        image = self.make('screen.png', data=b'\x89PNG')
        sent, _ = worker_attachments.transport_files([first, other, image], self.dir / 'transport')
        self.assertNotEqual(sent[0], sent[1])
        self.assertEqual(Path(sent[0]).read_bytes(), first.read_bytes())
        self.assertEqual(Path(sent[1]).read_bytes(), other.read_bytes())
        self.assertEqual(sent[2], str(image))

    def test_sensitive_original_is_rejected_before_transport(self):
        sensitive = self.make('credentials.json', '{"secret": "not for upload"}')
        with self.assertRaisesRegex(ValueError, 'sensitive'):
            worker_attachments.validate([{'path': str(sensitive)}])


if __name__ == "__main__":
    unittest.main()
