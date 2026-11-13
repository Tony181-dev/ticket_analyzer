"""Real Node bridge tests; no Jira/AI/network dependencies."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

os.environ.setdefault("TA_DATA_DIR", tempfile.mkdtemp(prefix="ta-log-tests-"))

from app import paths, settings
from app.evidence import logsleuth
from app.evidence.timestamps import normalize_clock, normalize_focus, parse_focus


class TimestampContractTest(unittest.TestCase):
    def test_canonical_clocks(self):
        for raw, expected in (("9:05", "09:05:00"), ("23:59:59.5", "23:59:59.500"),
                              ("00:00:01,25", "00:00:01.250"), ("12:30:00", "12:30:00")):
            self.assertEqual(normalize_clock(raw), expected)
        for raw in (None, "", "24:00:00", "12:60:00", "12:30:60", "12:30:01.1234", "2026-10-20T12:30:00Z"):
            self.assertIsNone(normalize_clock(raw))

    def test_invalid_ai_shapes_default_to_full(self):
        for answer in ("no timestamp", "```json\n[]\n```", '{"time_start":"25:00","time_end":"12:30"}',
                       '{"time_start":123}', '{"keywords":"do not iterate characters"}'):
            focus = parse_focus(answer)
            self.assertEqual(focus["schema_version"], 1)
            self.assertEqual(focus["time_basis"], "log-local-clock")
            self.assertEqual(focus["filter_mode"], "full")
            self.assertIsNone(focus["time_start"])
            self.assertIsNone(focus["time_end"])

    def test_fractional_midnight_and_open_windows(self):
        focus = parse_focus('ticket\n```json\n{"time_start":"23:59:59.9","time_end":"00:01"}\n```')
        self.assertEqual(focus["filter_mode"], "window")
        self.assertEqual(focus["time_start"], "23:59:59.900")
        self.assertEqual(focus["time_end"], "00:01:00")
        self.assertEqual(normalize_focus({"time_end": "12:30"})["filter_mode"], "window")


class LogSleuthOptionsTest(unittest.TestCase):
    def test_defaults_match_original_tool(self):
        opt = logsleuth.normalize_options()
        self.assertEqual((opt["before"], opt["after"], opt["window_ms"], opt["max_repeat"]), (5, 12, 300, 3))
        self.assertEqual(opt["parser"], "auto")
        self.assertEqual(opt["budget_tokens"], 50000)

    def test_invalid_manual_options_are_rejected(self):
        for patch in ({"mode": "guess"}, {"services": "svc"}, {"budget_tokens": 50001},
                      {"budget_tokens": -1}, {"before": 1.5}, {"time_start": "25:00:00"},
                      {"parser": "guess"}, {"extra_grep": [123]}, {"time_enabled": "yes"}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                logsleuth.normalize_options(patch)

    def test_settings_ceiling_is_validated(self):
        for value in (1000000, 0, "50000", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings.save({"log_budget_tokens": value})


@unittest.skipUnless(shutil.which("node"), "Node.js required")
class LogSleuthEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ta-logsleuth-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "src"
        self.source.mkdir()
        (self.source / "service.py").write_text(
            'import logging\nlog = logging.getLogger("Svc")\n'
            'log.error("decisive failure value=%d", 1)\n', encoding="utf-8")
        self.log = self.root / "trace.log"
        self.log.write_text(
            "10-20 12:30:00.100  1 2 E Svc: decisive failure value=1\n"
            "10-20 12:30:00.200  1 2 E Svc: decisive failure value=2\n"
            "10-20 13:00:00.100  1 2 E Svc: decisive failure value=3\n", encoding="utf-8")
        self.catalog = logsleuth.catalog_for(self.source)
        self.out = self.root / "brief.md"

    def test_catalog_services_and_manual_slice(self):
        info = logsleuth.catalog_info(self.catalog)
        self.assertEqual(info["services"][0]["name"], "Svc")
        stats = logsleuth.slice_logs([self.log], self.out, self.catalog,
            {"time_start": "12:30:00.150", "time_end": "12:30:00.250"}, 4000,
            {"mode": "manual", "services": ["Svc"], "before": 0, "after": 0, "window_ms": 0})
        text = self.out.read_text(encoding="utf-8")
        self.assertIn("value=2", text)
        self.assertNotIn("value=1", text)
        self.assertNotIn("value=3", text)
        self.assertEqual(stats["afterTime"], 1)
        self.assertEqual(stats["tokenUpperBound"], len(text.encode("utf-8")))
        self.assertLessEqual(stats["tokenUpperBound"], 4000)

    def test_manual_unknown_service_does_not_silently_expand(self):
        with self.assertRaisesRegex(logsleuth.LogSleuthError, "No catalog entries"):
            logsleuth.slice_logs([self.log], self.out, self.catalog, options={"mode": "manual", "services": ["WrongService"]})

    def test_all_markdown_is_capped_with_unicode(self):
        self.log.write_text("\n".join(
            f"10-20 12:30:00.200  1 2 E Svc: decisive failure value={i} " + "\u1ec7\u7d50\u679c" * 20
            for i in range(1000)), encoding="utf-8")
        stats = logsleuth.slice_logs([self.log], self.out, self.catalog, budget_tokens=2500,
                                    options={"max_repeat": 100, "window_ms": 0})
        self.assertLessEqual(self.out.stat().st_size, 2500)
        self.assertEqual(stats["tokens"], self.out.stat().st_size)
        self.assertIn("Output cap: 2500", self.out.read_text(encoding="utf-8"))
        self.assertGreater(stats["kept"], 0)
        self.assertTrue(stats["outputTruncated"])

    def test_global_ceiling_overrides_per_run_budget(self):
        stats = logsleuth.slice_logs([self.log], self.out, self.catalog, budget_tokens=2500,
                                    options={"budget_tokens": 50000})
        self.assertEqual(stats["budgetTokens"], 2500)
        self.assertLessEqual(self.out.stat().st_size, 2500)

    def test_original_semantics_and_output_guard(self):
        test = Path(__file__).with_name("test_logsleuth.js")
        result = subprocess.run([shutil.which("node"), str(test)], capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace"))