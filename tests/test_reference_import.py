"""ZIP image ingestion and shared reference metadata."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine import Engine
from app.pngutil import write_png
from app.reference_import import import_reference_zip
from app.server import make_handler
from app.store import Store


def zip_bytes(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


class ReferenceZipTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.environment = patch.dict(os.environ, {"MP_CONTENT_HOME": self.folder.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = Store(Path(self.folder.name) / "app.sqlite3")
        self.addCleanup(self.store.close)
        image = Path(self.folder.name) / "source.png"
        write_png(image, 2, 2, lambda horizontal, vertical: (10, 20, 30, 255))
        self.png = image.read_bytes()

    def test_nested_images_share_metadata_and_duplicate_names_stay_separate(self):
        raw = zip_bytes([("风景/同名.png", self.png), ("其他/同名.PNG", self.png), ("说明.txt", b"text"), ("__MACOSX/._同名.png", b"metadata")])
        result = import_reference_zip(self.store, io.BytesIO(raw), " 风景 ", ["海边", "海边", " 竖图 "], "共同说明\n下一行")
        self.assertEqual(result, {"ok": True, "created": 2, "skipped": 2})
        records = self.store.list_elements()
        self.assertEqual(len({record["payload"]["path"] for record in records}), 2)
        for record in records:
            self.assertEqual(record["name"], "同名")
            self.assertEqual(record["group"], "风景")
            self.assertEqual(record["tags"], ["海边", "竖图"])
            self.assertEqual(record["payload"]["note"], "共同说明\n下一行")
            self.assertEqual(record["payload"]["role"], "风格")
            self.assertEqual(record["status"], "启用")
            self.assertEqual((Path(self.folder.name) / record["payload"]["path"]).read_bytes(), self.png)

    def test_metadata_can_all_be_empty(self):
        import_reference_zip(self.store, io.BytesIO(zip_bytes([("image.png", self.png)])))
        record = self.store.list_elements()[0]
        self.assertEqual((record["group"], record["tags"], record["payload"]["note"]), ("", [], ""))

    def test_bad_later_image_rolls_back_records_and_files(self):
        raw = zip_bytes([("good.png", self.png), ("bad.jpg", b"not an image")])
        with self.assertRaisesRegex(ValueError, "bad.jpg"):
            import_reference_zip(self.store, io.BytesIO(raw))
        self.assertEqual(self.store.list_elements(), [])
        self.assertEqual(list((Path(self.folder.name) / "uploads").rglob("*.png")), [])
        self.assertEqual(list((Path(self.folder.name) / "uploads/reference-images").iterdir()), [])

    def test_database_failure_rolls_back_files_and_records(self):
        original = self.store.upsert_element
        def fail_second(record):
            if record["name"] == "second":
                raise RuntimeError("database failure")
            return original(record)
        with patch.object(self.store, "upsert_element", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                import_reference_zip(self.store, io.BytesIO(zip_bytes([("first.png", self.png), ("second.png", self.png)])))
        self.assertEqual(self.store.list_elements(), [])
        self.assertEqual(list((Path(self.folder.name) / "uploads").rglob("*.png")), [])

    def test_invalid_archives_and_empty_archives_are_rejected(self):
        for raw in (b"not a zip", zip_bytes([]), zip_bytes([("text.txt", b"text")])):
            with self.subTest(raw=raw[:20]):
                with self.assertRaises(ValueError):
                    import_reference_zip(self.store, io.BytesIO(raw))
        self.assertEqual(self.store.list_elements(), [])

    def test_paths_cannot_escape_and_symlinks_are_rejected(self):
        for name in ("../outside.png", "folder/../../outside.png", "C:\\outside.png", "/outside.png"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "路径"):
                    import_reference_zip(self.store, io.BytesIO(zip_bytes([(name, self.png)])))
        link = zipfile.ZipInfo("link.png")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        with self.assertRaisesRegex(ValueError, "路径"):
            import_reference_zip(self.store, io.BytesIO(zip_bytes([(link, b"../target.png")])))

    def test_size_count_limits_and_bad_metadata(self):
        raw = zip_bytes([("first.png", self.png), ("second.png", self.png)])
        for constant, limit in (("MAX_IMAGE_BYTES", 1), ("MAX_EXPANDED_BYTES", 1), ("MAX_IMAGES", 1)):
            with self.subTest(constant=constant):
                with patch("app.reference_import." + constant, limit):
                    with self.assertRaises(ValueError):
                        import_reference_zip(self.store, io.BytesIO(raw))
        with self.assertRaisesRegex(ValueError, "标签"):
            import_reference_zip(self.store, io.BytesIO(raw), tags={"bad": "value"})
        self.assertEqual(self.store.list_elements(), [])

    def test_chinese_metadata_http_upload_and_image_preview(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store, Engine(self.store)))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        query = urlencode({"name": "参考图.zip", "group": "新分组", "tags": json.dumps(["标签"], ensure_ascii=False), "note": "说明 & 特殊字符 +\n下一行"})
        base = "http://127.0.0.1:" + str(server.server_port)
        request = Request(base + "/api/reference-images/import-zip?" + query, data=zip_bytes([("参考图.png", self.png)]), method="POST")
        with urlopen(request) as response:
            self.assertEqual(json.load(response)["created"], 1)
        record = self.store.list_elements()[0]
        self.assertEqual(record["payload"]["note"], "说明 & 特殊字符 +\n下一行")
        with urlopen(base + "/media/" + record["payload"]["path"]) as response:
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertEqual(response.read(), self.png)


if __name__ == "__main__":
    unittest.main()
