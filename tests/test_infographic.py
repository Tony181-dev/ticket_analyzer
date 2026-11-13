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

    def test_invalid_response_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "JSON object"):
            parse_ai_summary("not JSON")


if __name__ == "__main__":
    unittest.main()