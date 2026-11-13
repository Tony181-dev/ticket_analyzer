"""Log preparation: archives, compression, encodings, JSON arrays."""
import bz2
import gzip
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

import multivolumefile
import py7zr

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("TA_DATA_DIR", tempfile.mkdtemp(prefix="ta-test-"))
sys.path.insert(0, str(ROOT))

from app import logprep  # noqa: E402
from app.evidence import logsleuth  # noqa: E402

LOG = ("10-10 12:30:01.200  1000  1001 E BluetoothService: connect failed status=133\n"
       "10-10 12:30:01.300  1000  1001 W BluetoothService: retry scheduled\n")


class LogPrepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ta-prep-"))
        self.work = self.tmp / "work"

    def prep(self, *files):
        return [Path(p) for p in logprep.prepare([str(f) for f in files], self.work, log=lambda m: None)]

    def write(self, name, data):
        p = self.tmp / name
        p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return p

    def test_plain_text_is_untouched(self):
        p = self.write("a.log", LOG)
        self.assertEqual(self.prep(p), [p])

    def test_gzip_bz2_and_nested_zip(self):
        gz = self.write("a.log.gz", gzip.compress(LOG.encode()))
        bz = self.write("b.log.bz2", bz2.compress(LOG.encode()))
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as z:
            z.writestr("deep/c.log", LOG)
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w") as z:
            z.writestr("inner.zip", inner.getvalue())
            z.writestr("shot.png", b"\x89PNG")
            z.writestr("syslog", LOG)
        zp = self.write("bundle.zip", outer.getvalue())
        out = self.prep(gz, bz, zp)
        self.assertEqual(sorted(p.name for p in out), ["a.log", "b.log", "c.log", "syslog"])
        for p in out:
            self.assertEqual(p.read_text(encoding="utf-8"), LOG)

    def test_tar_gz(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = LOG.encode()
            info = tarfile.TarInfo("x/main.log")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        out = self.prep(self.write("logs.tgz", buf.getvalue()))
        self.assertEqual([p.name for p in out], ["main.log"])

    def test_split_7z_with_unicode_filename(self):
        source = self.write("source.log", LOG * 100)
        base = self.tmp / "분할재압축.7z"
        with multivolumefile.MultiVolume(base, mode="wb", volume=4096, ext_digits=3) as stream:
            with py7zr.SevenZipFile(stream, mode="w", filters=[{"id": py7zr.FILTER_COPY}]) as archive:
                archive.write(source, arcname="nested/main.log")
        parts = sorted(self.tmp.glob(base.name + ".*"))
        self.assertGreater(len(parts), 1, "fixture must be split into multiple volumes")

        out = self.prep(*parts)
        self.assertEqual([p.name for p in out], ["main.log"])
        self.assertEqual(out[0].read_text(encoding="utf-8"), LOG * 100)

    def test_encodings_become_utf8(self):
        korean = "10-10 12:30:01.200  1  2 E Svc: 연결 실패\n"
        for name, data in (("k.log", korean.encode("cp949")), ("u16.log", korean.encode("utf-16-le")),
                           ("w.log", "10-10 12:30:01.200  1  2 E Svc: caf\xe9 d\xe9j\xe0\n".encode("cp1252"))):
            p = self.prep(self.write(name, data))[0]
            text = p.read_text(encoding="utf-8")
            self.assertNotIn("\ufffd", text, name)
            self.assertIn("Svc", text)
        self.assertIn("연결 실패", self.prep(self.tmp / "k.log")[0].read_text(encoding="utf-8"))

    def test_json_array_becomes_json_lines(self):
        records = [{"time": "2025-01-01T10:00:00Z", "level": "error", "message": "boom"},
                   {"time": "2025-01-01T10:00:01Z", "level": "info", "message": "ok"}]
        out = self.prep(self.write("events.json", json.dumps({"events": records}, indent=2)))[0]
        lines = out.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(x)["message"] for x in lines], ["boom", "ok"])

    def test_json_lines_untouched(self):
        p = self.write("e.jsonl", '{"message": "a"}\n{"message": "b"}\n')
        self.assertEqual(self.prep(p), [p])

    def test_binary_reported_by_logsleuth(self):
        p = self.write("blob.dat", os.urandom(4000))
        good = self.write("a.log", LOG)
        spans = logsleuth.span(self.prep(p, good))
        self.assertEqual({s["name"]: s["format"] for s in spans}["blob.dat"], "binary")


if __name__ == "__main__":
    unittest.main()
