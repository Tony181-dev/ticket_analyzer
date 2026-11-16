"""Languages: every server message has a Korean text, t() follows settings["lang"], reports follow the UI."""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("TA_DATA_DIR", tempfile.mkdtemp(prefix="ta-test-"))
sys.path.insert(0, str(ROOT))

from app import i18n, pipeline, settings  # noqa: E402
from app.report import refine_pipeline  # noqa: E402


class I18nTest(unittest.TestCase):
    def tearDown(self):
        settings.save({"lang": "vi"})

    def test_every_message_has_korean_with_the_same_placeholders(self):
        self.assertEqual(set(i18n.MESSAGES) - set(i18n.KO), set())
        holes = lambda s: set(re.findall(r"\{(\w+)", s))  # noqa: E731
        bad = [k for k, (_, en) in i18n.MESSAGES.items() if holes(i18n.KO[k]) != holes(en)]
        self.assertEqual(bad, [])

    def test_t_follows_the_ui_language(self):
        for code, expected in (("vi", "Đăng Collab"), ("en", "Publish to Collab"), ("ko", "Collab 게시")):
            settings.save({"lang": code})
            self.assertEqual(i18n.t("step.collab"), expected)
        self.assertEqual(i18n.language_name(), "Korean")
        with self.assertRaises(ValueError):
            settings.save({"lang": "fr"})

    def test_report_prompts_and_diagram_heading_follow_the_language(self):
        settings.save({"lang": "en"})
        self.assertIn("English", pipeline._write_in())
        settings.save({"lang": "ko"})
        self.assertIn("Korean", pipeline._write_in())
        self.assertEqual(pipeline.DIAGRAM_HEADINGS[pipeline._lang_code()], "진단 다이어그램")
        for heading in ("Diagnostic Diagram", "Sơ đồ chẩn đoán", "진단 다이어그램"):
            self.assertTrue(refine_pipeline._DIAGRAM_HEADING_RE.search(f"<h2>{heading}</h2>"), heading)
        macro = ('<ac:structured-macro ac:name="plantuml"><ac:plain-text-body><![CDATA[@startuml\nparticipant A\n'
                 'participant B\nA -> B: x\n@enduml]]></ac:plain-text-body></ac:structured-macro>')
        repaired = refine_pipeline.repair_required_plantuml("<h2>진단 다이어그램</h2>" + macro)
        self.assertNotIn("Diagnostic Diagram", repaired, "a localized heading is not duplicated in English")


if __name__ == "__main__":
    unittest.main()
