"""Unit tests for the evidence helpers: clock alignment, code graph languages/focus, settings validation."""
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("TA_DATA_DIR", tempfile.mkdtemp(prefix="ta-evidence-"))
sys.path.insert(0, str(ROOT))

from app import paths, settings  # noqa: E402
from app.codegraph import langs  # noqa: E402
from app.codegraph.graph import CodeGraph  # noqa: E402
from app.evidence import graph as codegraph  # noqa: E402
from app.evidence import logsleuth, timealign  # noqa: E402
from app.evidence.timestamps import format_offset, normalize_focus, normalize_shift, parse_offset  # noqa: E402

SPAN_UTC = [{"name": "main.log", "first": "05:10:00", "last": "05:45:12", "wraps": 0}]


class OffsetTest(unittest.TestCase):
    def test_offsets_people_write_in_tickets(self):
        for raw, minutes in (("+09:00", 540), ("UTC+9", 540), ("KST", 540), ("-0530", -330), ("Z", 0), ("utc", 0),
                             ("GMT-5", -300), (7, 420), ("+00:00", 0)):
            self.assertEqual(parse_offset(raw), minutes, raw)
        for raw in (None, "", "CST", "tomorrow", "+25:00", True, "+09:75"):
            self.assertIsNone(parse_offset(raw), raw)
        self.assertEqual(format_offset(-330), "-05:30")
        self.assertEqual(format_offset(None), "")

    def test_manual_shift_bounds(self):
        self.assertEqual(normalize_shift("-540"), -540)
        self.assertIsNone(normalize_shift(None))
        self.assertIsNone(normalize_shift(""))
        for bad in ("abc", 14 * 60 + 1, -100000):
            with self.assertRaises(ValueError):
                normalize_shift(bad)

    def test_focus_keeps_zone_date_and_attachment_picks(self):
        focus = normalize_focus({"time_start": "14:29", "time_end": "14:31", "ticket_utc_offset": "KST",
                                 "incident_date": "2024-10-10", "attachments": ["a.log", {"filename": "b.zip", "reason": "r"},
                                                                                {"filename": "A.LOG"}, 5]})
        self.assertEqual(focus["ticket_utc_offset"], "+09:00")
        self.assertEqual(focus["incident_date"], "2024-10-10")
        self.assertEqual(focus["attachments"], [{"filename": "a.log", "reason": ""}, {"filename": "b.zip", "reason": "r"}])
        missing = normalize_focus({"ticket_utc_offset": "somewhere", "incident_date": "10 Oct"})
        self.assertIsNone(missing["ticket_utc_offset"])
        self.assertIsNone(missing["incident_date"])
        self.assertIsNone(missing["attachments"], "no list from the AI is different from an empty list")
        self.assertEqual(normalize_focus({"attachments": []})["attachments"], [])


class AlignTest(unittest.TestCase):
    FOCUS = {"time_start": "14:29:00", "time_end": "14:31:00", "ticket_utc_offset": "+09:00"}

    def test_clock_shift_wraps_midnight_and_keeps_precision(self):
        self.assertEqual(timealign.shift_clock("00:10:00", -30), "23:40:00")
        self.assertEqual(timealign.shift_clock("23:59:59.900", 1), "00:00:59.900")
        self.assertIsNone(timealign.shift_clock(None, 60))

    def test_window_coverage(self):
        self.assertTrue(timealign.window_covered(SPAN_UTC, "05:29:00", "05:31:00"))
        self.assertFalse(timealign.window_covered(SPAN_UTC, "14:29:00", "14:31:00"))
        self.assertTrue(timealign.window_covered(SPAN_UTC, "05:44:00", None), "an open window is tested at its one endpoint")
        self.assertIsNone(timealign.window_covered([{"name": "x", "first": "", "last": ""}], "05:29:00", "05:31:00"))
        over_midnight = [{"first": "23:50:00", "last": "00:20:00", "wraps": 0}]
        self.assertTrue(timealign.window_covered(over_midnight, "00:05:00", "00:06:00"))
        self.assertFalse(timealign.window_covered(over_midnight, "12:00:00", "12:01:00"))
        self.assertTrue(timealign.window_covered([{"first": "10:00:00", "last": "11:00:00", "wraps": 2}], "03:00:00", "03:01:00"),
                        "a file that crosses midnight cannot exclude a time of day")

    def test_ticket_zone_and_utc_log(self):
        result = timealign.align(self.FOCUS, SPAN_UTC)
        self.assertEqual((result["shift_minutes"], result["source"]), (-540, "log-utc"))
        self.assertEqual((result["time_start"], result["time_end"]), ("05:29:00", "05:31:00"))

    def test_same_clock_is_kept_when_it_fits(self):
        local = [{"first": "14:00:00", "last": "15:00:00", "wraps": 0}]
        self.assertEqual(timealign.align(self.FOCUS, local)["shift_minutes"], 0)

    def test_jira_zone_is_only_a_fallback_candidate(self):
        focus = {"time_start": "14:29:00", "time_end": "14:31:00"}
        self.assertEqual(timealign.align(focus, SPAN_UTC, jira_offset=540)["source"], "log-utc-jira-zone")
        self.assertEqual([c["source"] for c in timealign.candidates(self.FOCUS, 420)], ["same-clock", "log-utc"],
                         "a zone stated in the ticket is never overridden by Jira's")

    def test_manual_shift_always_wins(self):
        result = timealign.align(self.FOCUS, SPAN_UTC, manual_shift=60)
        self.assertEqual((result["shift_minutes"], result["source"], result["time_start"]), (60, "manual", "15:29:00"))

    def test_ai_breaks_a_tie_only_with_a_window_inside_the_logs(self):
        whole_day = [{"first": "00:00:01", "last": "23:59:58", "wraps": 0}]
        asked = []

        def ai(options):
            asked.append(options)
            return {"shift_minutes": -540, "reason": "upload time matches"}
        result = timealign.align(self.FOCUS, whole_day, ask_ai=ai)
        self.assertEqual((result["shift_minutes"], result["source"]), (-540, "ai"))
        self.assertEqual(len(asked[0]), 2)

        nowhere = {"time_start": "14:29:00", "time_end": "14:31:00"}
        rejected = timealign.align(nowhere, SPAN_UTC, ask_ai=lambda options: {"shift_minutes": 30})
        self.assertEqual((rejected["shift_minutes"], rejected["covered"]), (0, False),
                         "an AI shift whose window misses every log file is not applied")
        broken = timealign.align(nowhere, SPAN_UTC, ask_ai=lambda options: 1 / 0)
        self.assertEqual(broken["shift_minutes"], 0, "the tie-break is optional and never fails the step")

    def test_no_time_and_unknown_spans(self):
        self.assertEqual(timealign.align({}, SPAN_UTC)["source"], "none")
        unknown = timealign.align(self.FOCUS, [])
        self.assertEqual((unknown["shift_minutes"], unknown["covered"]), (0, None))

    def test_jira_offset_and_report(self):
        self.assertEqual(timealign.jira_utc_offset('{"created": "2024-10-10T14:50:11.000+0900"}'), 540)
        self.assertEqual(timealign.jira_utc_offset('{"updated": "2024-10-10T05:50:11Z"}'), 0)
        self.assertIsNone(timealign.jira_utc_offset('{"created": "10 Oct"}'))
        result = timealign.align(self.FOCUS, SPAN_UTC)
        text = timealign.report(self.FOCUS, SPAN_UTC, [{"filename": "main.log", "created": "2024-10-10T14:50:11+0900"}],
                                540, result["candidates"])
        self.assertIn("| -540 |", text)
        self.assertIn("main.log", text)

    def test_span_bridge_reports_midnight_crossing(self):
        folder = Path(tempfile.mkdtemp(prefix="ta-span-"))
        log = folder / "night.log"
        log.write_text("10-10 23:59:00.000  1  1 I A: before\n10-11 00:01:00.000  1  1 I A: after\n", encoding="utf-8")
        span = logsleuth.span([log])[0]
        self.assertEqual((span["first"], span["last"], span["wraps"]), ("23:59:00", "00:01:00", 1))
        self.assertIn("23:59:00", span["firstTs"])


JAVA = textwrap.dedent('''
    package com.example.bt;
    import com.example.audio.AudioRouter;
    // class Commented {}
    public class BluetoothService extends BaseService implements Connectable, Closeable {
        private final AudioRouter router;
        public BluetoothService(AudioRouter router) { this.router = router; }
        @Override
        public void connect(Device dev) throws IOException {
            String s = "class Fake { void nope() {} }";
            if (dev == null) { retry(dev); }
            router.route(dev);
        }
        private void retry(Device dev) { connect(dev); }
        static class Inner { void ping() { } }
    }
''')
KOTLIN = textwrap.dedent('''
    package com.example.audio
    import com.example.bt.BluetoothService
    class AudioRouter(private val svc: BluetoothService) : Router(), Closeable {
        fun route(dev: Device): Boolean {
            return mix(dev)
        }
        private fun mix(dev: Device) = true
    }
    fun topLevel() { AudioRouter(null).route(null) }
''')
TS = textwrap.dedent('''
    import { helper } from './util';
    const fs = require('fs');
    export class ConnManager extends Base {
      constructor(private url: string) { super(); }
      async connect(retries: number): Promise<void> {
        if (retries > 0) { helper(this.url); }
      }
    }
    export function start() { new ConnManager('x').connect(1); }
    export const stop = async (code: number) => { helper(String(code)); };
''')


class CodeGraphLanguageTest(unittest.TestCase):
    def names(self, nodes, kind=None):
        return sorted(n["name"] for n in nodes if kind is None or n["kind"] == kind)

    def test_java(self):
        nodes, edges = langs.javaparse("bt/BluetoothService.java", JAVA)
        self.assertEqual(self.names(nodes, "class"), ["BluetoothService", "Inner"])
        self.assertEqual(self.names(nodes, "method"), ["connect", "ping", "retry"])
        self.assertEqual(self.names(nodes, "constructor"), ["BluetoothService"])
        self.assertNotIn("Fake", self.names(nodes), "code inside string literals and comments is not structure")
        self.assertNotIn("Commented", self.names(nodes))
        inherits = {e["target_name"] for e in edges if e["kind"] == "INHERITS"}
        self.assertEqual(inherits, {"BaseService", "Connectable", "Closeable"})
        self.assertIn({"source": "bt/BluetoothService.java", "target_name": "AudioRouter", "kind": "USES"}, edges)
        connect = next(n for n in nodes if n["name"] == "connect")
        self.assertEqual(connect["id"], "bt/BluetoothService.java::BluetoothService::connect")
        self.assertEqual(connect["line"], JAVA.count("\n", 0, JAVA.index("public void connect")) + 1)
        calls = {e["target_name"] for e in edges if e["kind"] == "CALLS" and e["source"] == connect["id"]}
        self.assertEqual(calls, {"retry", "route"})

    def test_kotlin(self):
        nodes, edges = langs.kotlinparse("audio/AudioRouter.kt", KOTLIN)
        self.assertEqual(self.names(nodes, "class"), ["AudioRouter"])
        self.assertEqual(self.names(nodes, "method"), ["mix", "route"])
        self.assertEqual(self.names(nodes, "function"), ["topLevel"])
        self.assertEqual({e["target_name"] for e in edges if e["kind"] == "INHERITS"}, {"Router", "Closeable"})
        route = next(n for n in nodes if n["name"] == "route")
        self.assertIn({"source": route["id"], "target_name": "mix", "kind": "CALLS"}, edges)

    def test_typescript(self):
        nodes, edges = langs.jsparse("net/conn.ts", TS)
        self.assertEqual(self.names(nodes, "class"), ["ConnManager"])
        self.assertEqual(self.names(nodes, "method"), ["connect"])
        self.assertEqual(self.names(nodes, "constructor"), ["constructor"])
        self.assertEqual(self.names(nodes, "function"), ["start", "stop"])
        imports = [e for e in edges if e["kind"] == "IMPORTS"]
        self.assertEqual(len(imports), 1, "only relative modules are part of this source tree")
        self.assertIn("net/util.ts", imports[0]["candidates"])

    def test_graph_links_files_across_languages_and_ranks_ticket_symbols_first(self):
        root = Path(tempfile.mkdtemp(prefix="ta-graph-"))
        (root / "bt").mkdir()
        (root / "audio").mkdir()
        (root / "net").mkdir()
        (root / "bt" / "BluetoothService.java").write_text(JAVA, encoding="utf-8")
        (root / "audio" / "AudioRouter.kt").write_text(KOTLIN, encoding="utf-8")
        (root / "net" / "conn.ts").write_text(TS, encoding="utf-8")
        (root / "net" / "util.ts").write_text("export function helper(x: string) { return x; }\n", encoding="utf-8")
        (root / "net" / "bundle.min.js").write_text("function a(){}" * 4000, encoding="utf-8")
        cg = CodeGraph(str(root)).build()
        self.assertEqual(cg.meta["files"], 5)
        edges = {(e["source"], e["target"], e["kind"]) for e in cg.edges}
        self.assertIn(("net/conn.ts", "net/util.ts", "IMPORTS"), edges)
        self.assertIn(("audio/AudioRouter.kt", "bt/BluetoothService.java::BluetoothService", "USES"), edges)
        self.assertIn(("bt/BluetoothService.java::BluetoothService::connect", "audio/AudioRouter.kt::AudioRouter::route", "CALLS"), edges)
        self.assertFalse([n for n in cg.nodes if n["file"] == "net/bundle.min.js" and n["kind"] != "file"],
                         "a minified bundle is listed as a file but not parsed")

        plain = cg.to_simple(2000)
        focused = cg.to_simple(2000, focus={"terms": ["retry"], "files": ["net/conn.ts"]})
        self.assertNotIn("focus", plain["meta"])
        self.assertEqual(focused["nodes"][0]["name"], "retry", "an exact symbol match outranks plain connectivity")
        self.assertGreaterEqual(focused["meta"]["focus"]["matched_nodes"], 2)
        top = [n["id"] for n in focused["nodes"][:8]]
        self.assertTrue(any(i.startswith("net/conn.ts") for i in top), "files named by the log evidence are pulled up")

    def test_build_can_be_cancelled_and_cache_notices_new_parsers(self):
        root = Path(tempfile.mkdtemp(prefix="ta-graph-cancel-"))
        (root / "A.java").write_text("class A { void run() {} }\n", encoding="utf-8")

        class Stop(Exception):
            pass

        def stop():
            raise Stop()
        with self.assertRaises(Stop):
            codegraph.build(root, cancel=stop)
        out, meta = codegraph.build(root, token_budget=5000)
        self.assertEqual(meta["nodes"], 3)
        manifest = json.loads((out / ".cache.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["source_fingerprint"].startswith(f"v{codegraph.PARSER_VERSION}:"))
        matched = codegraph.write_focused(out, out / "focused.json", 5000, {"terms": ["run"], "files": []})
        self.assertEqual(matched, 1)


class SettingsValidationTest(unittest.TestCase):
    def test_bad_values_are_rejected_with_a_reason(self):
        paths.ensure_dirs()
        for patch in ({"default_gate": "auto"}, {"ai_mode": "gpt"}, {"lang": "fr"}, {"axel_connections": 0},
                      {"axel_connections": "30"}, {"m365_headless": "yes"}, {"graph_token_budget": 10},
                      {"auto_download_max_mb": -1}, {"ai_retries": 9}, {"collab_space_key": 5}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                settings.save(patch)
        saved = settings.save({"default_gate": "review", "auto_download_max_mb": 0, "collab_space_key": "  DEV  ",
                               "unknown_key": 1})
        self.assertEqual((saved["default_gate"], saved["auto_download_max_mb"], saved["collab_space_key"]), ("review", 0, "DEV"))
        self.assertNotIn("unknown_key", saved)
        settings.save({"default_gate": "skip", "auto_download_max_mb": 2000, "collab_space_key": "~"})

    def test_a_hand_edited_file_never_breaks_loading(self):
        paths.ensure_dirs()
        before = paths.SETTINGS_FILE.read_text(encoding="utf-8") if paths.SETTINGS_FILE.exists() else None
        try:
            paths.SETTINGS_FILE.write_text(json.dumps({"settings_version": 4, "default_gate": "nonsense", "ai_retries": 99,
                                                       "axel_connections": "many", "lang": "en"}), encoding="utf-8")
            loaded = settings.load()
            self.assertEqual(loaded["default_gate"], "skip")
            self.assertEqual(loaded["ai_retries"], 3, "an out-of-range number is clamped")
            self.assertEqual(loaded["axel_connections"], settings.DEFAULTS["axel_connections"])
            self.assertEqual(loaded["lang"], "en")
            # 30 was the default before v5 (stored as if chosen): it moves to the new default; a 30 saved
            # under v5 is the user's own choice and stays.
            paths.SETTINGS_FILE.write_text(json.dumps({"settings_version": 4, "axel_connections": 30}), encoding="utf-8")
            self.assertEqual(settings.load()["axel_connections"], 8)
            paths.SETTINGS_FILE.write_text(json.dumps({"settings_version": 5, "axel_connections": 30}), encoding="utf-8")
            self.assertEqual(settings.load()["axel_connections"], 30)
            paths.SETTINGS_FILE.write_text("[]", encoding="utf-8")
            self.assertEqual(settings.load()["ai_mode"], "m365")
        finally:
            if before is None:
                paths.SETTINGS_FILE.unlink(missing_ok=True)
            else:
                paths.SETTINGS_FILE.write_text(before, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
