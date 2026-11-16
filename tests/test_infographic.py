import json
import re
import unittest
from xml.etree import ElementTree

from app.report.infographic import parse_ai_summary, render_svg


class InfographicTest(unittest.TestCase):
    def test_fenced_json_renders_escaped_svg(self):
        response = '''```json
{"title":"Crash <summary>","summary":"Ticket says A & B failed.","root_cause":"<script>alert(1)</script>",
 "timeline":[{"time":"12:30","event":"Error recorded."}],"evidence":["status=133"],"actions":["Inspect retry path"]}
```'''
        data = parse_ai_summary(response)
        svg = render_svg(data, "ABC-123")
        root = ElementTree.fromstring(svg)
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        self.assertIn("Crash &lt;summary&gt;", svg)
        self.assertIn("A &amp; B", svg)
        self.assertIn("&lt;script&gt;", svg)
        self.assertNotIn("<script>", svg)

    def test_full_contract_renders_badges_facts_and_code(self):
        data = parse_ai_summary(json.dumps({
            "title": "BT connect fails with status 133", "subtitle": "BluetoothService · build 1.2.3",
            "severity": "HIGH", "confidence": "medium", "summary": "Pairing fails after resume.",
            "root_cause": "Hypothesis: retry races with teardown.",
            "key_facts": [{"label": "Error", "value": "status=133"}, {"label": "First seen", "value": "12:30:01"},
                          {"label": "Rate", "value": 7}, "not a fact"],
            "timeline": [{"time": f"12:30:0{i}", "event": f"event {i}"} for i in range(7)],
            "evidence": ["main.log 12:30:01 connect failed status=133"],
            "code_locations": ["src/bt/Conn.kt:88 · Conn.retry"],
            "actions": ["Guard retry against teardown"],
        }))
        self.assertEqual(data["severity"], "high")
        self.assertEqual(len(data["timeline"]), 5)
        self.assertEqual(data["key_facts"][2], {"label": "Rate", "value": "7"})
        svg = render_svg(data, "BT-1")
        ElementTree.fromstring(svg)
        for text in ("status=133", "12:30:01", "src/bt/Conn.kt:88 · Conn.retry", "BluetoothService · build 1.2.3"):
            self.assertIn(text, svg)

    def test_canvas_grows_with_long_content_and_tolerates_old_contract(self):
        short = render_svg(parse_ai_summary('{"title":"T","summary":"S"}'), "A-1")
        long_items = ["word " * 40] * 4
        tall = render_svg(parse_ai_summary(json.dumps({
            "title": "T", "summary": "S " * 100, "root_cause": "R " * 100, "evidence": long_items,
            "actions": long_items, "timeline": [{"time": "1", "event": "e " * 60}] * 5})), "A-1")
        height = lambda svg: int(re.search(r'height="(\d+)"', svg).group(1))
        self.assertEqual(height(short), 900)
        self.assertGreater(height(tall), height(short))
        legacy = {"title": "T", "summary": "S", "root_cause": "", "timeline": [], "evidence": [], "actions": []}
        ElementTree.fromstring(render_svg(legacy, "A-1"))  # dicts saved before the new fields still render

    def test_invalid_response_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "JSON object"):
            parse_ai_summary("not JSON")


if __name__ == "__main__":
    unittest.main()