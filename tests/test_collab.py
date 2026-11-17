"""Collab attachment transport and truthful success reporting."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.report import collab


def wrapped(data):
    text = json.dumps(data)
    return {"isError": False, "content": [{"type": "text", "text": text}],
            "structuredContent": {"result": text}}


class CollabAttachmentTest(unittest.TestCase):
    def test_nested_failure_is_not_reported_as_success(self):
        result = wrapped({"message": "Attachment upload failed", "attachment": {
            "success": False, "error": "Path traversal detected: outside allowed directory"}})
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "06-infographic.svg"
            image.write_bytes(b"<svg/>")
            log = []
            with patch.object(collab.mcp, "call", return_value=result):
                self.assertFalse(collab.attach_image("123", image, log=log.append))
            self.assertTrue(any("upload failed" in line for line in log))
            self.assertFalse(any("attached to page" in line for line in log))

    def test_upload_sends_bytes_not_server_local_path_and_verifies_attachment(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "06-infographic.svg"
            payload = '<svg xmlns="http://www.w3.org/2000/svg"><text>Đã sửa → 확인</text></svg>'.encode("utf-8")
            image.write_bytes(payload)
            uploaded = wrapped({"attachment": {"success": True, "id": "456", "filename": image.name}})
            listed = wrapped({"success": True, "attachments": [{
                "id": "456", "title": image.name, "extensions": {"fileSize": len(payload)}}]})
            with patch.object(collab.mcp, "call", side_effect=[uploaded, listed]) as call:
                self.assertTrue(collab.attach_image("123", image, log=lambda _: None))
            args = call.call_args_list[0].args[2]
            self.assertNotIn("file_path", args)
            self.assertEqual(args["filename"], image.name)
            self.assertEqual(base64.b64decode(args["content_base64"]), payload)
            self.assertEqual(call.call_args_list[1].args[1], "confluence_get_attachments")
            self.assertEqual(call.call_args_list[1].args[2]["filename"], image.name)

    def test_missing_or_wrong_size_attachment_is_not_success(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "06-infographic.svg"
            image.write_bytes(b"<svg/>")
            for entries in ([], [{"id": "456", "title": image.name, "extensions": {"fileSize": 1}}]):
                with self.subTest(entries=entries), patch.object(collab.mcp, "call", side_effect=[
                        wrapped({"attachment": {"success": True, "id": "456", "filename": image.name}}),
                        wrapped({"success": True, "attachments": entries})]):
                    self.assertFalse(collab.attach_image("123", image, log=lambda _: None))

    def test_unstructured_empty_upload_result_is_not_success(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "06-infographic.svg"
            image.write_bytes(b"<svg/>")
            with patch.object(collab.mcp, "call", return_value=wrapped({})):
                self.assertFalse(collab.attach_image("123", image, log=lambda _: None))

    def test_missing_local_file_is_reported_without_raising(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(collab.mcp, "call") as call:
                self.assertFalse(collab.attach_image("123", Path(folder) / "missing.svg", log=lambda _: None))
            call.assert_not_called()


if __name__ == "__main__":
    unittest.main()