"""Spreadsheet round trips, validation, and batch storage."""

from __future__ import annotations

import csv
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import Workbook, load_workbook

from app.catalog import ELEMENT_DEFAULTS, blank_payload
from app.store import Store
from app.tables import export_table, import_table, read_table


def csv_bytes(rows):
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    return output.getvalue().encode("utf-8-sig")


class TableTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.environment = patch.dict(os.environ, {"MP_CONTENT_HOME": self.folder.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = Store(Path(self.folder.name) / "app.sqlite3")
        self.addCleanup(self.store.close)

    def test_every_element_type_round_trips_excel_and_csv(self):
        for kind in ELEMENT_DEFAULTS:
            payload = blank_payload(kind)
            if kind == "prompt":
                payload["template"] = '=literal,"引号"\n第二行 {{theme}}'
            elif kind == "model_profile":
                payload["concurrency"] = 5
                payload["maxTokens"] = 1024
            elif kind == "reference_image":
                payload["useCount"] = 7
            record = self.store.upsert_element({"id": kind, "type": kind, "name": "名称", "group": "分组", "tags": ["a|b", "标签"], "status": "停用", "fields": [{"key": "key", "value": "a|b=c\n下一行"}], "payload": payload})
            for format_name in ("xlsx", "csv"):
                with self.subTest(kind=kind, format=format_name):
                    raw, filename, content_type = export_table(self.store, {"kind": "elements", "type": kind, "ids": [kind], "format": format_name})
                    self.store.delete_element(kind)
                    imported = import_table(self.store, "elements", kind, raw, filename)
                    self.assertEqual(imported["created"], 1)
                    saved = self.store.get_element(kind)
                    for key in ("name", "group", "tags", "status", "fields", "payload"):
                        self.assertEqual(saved[key], record[key], key)

    def test_invalid_later_row_leaves_all_records_unchanged(self):
        raw = csv_bytes([["名称", "状态"], ["有效", "启用"], ["无效", "未知"]])
        with self.assertRaisesRegex(ValueError, "第 3 行"):
            import_table(self.store, "elements", "prompt", raw, "test.csv")
        self.assertEqual(self.store.list_elements(), [])

    def test_database_error_rolls_back_entire_import(self):
        original = self.store.upsert_element
        def fail_second(record):
            if record["name"] == "第二行":
                raise RuntimeError("写入失败")
            return original(record)
        with patch.object(self.store, "upsert_element", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                import_table(self.store, "elements", "prompt", csv_bytes([["名称"], ["第一行"], ["第二行"]]), "test.csv")
        self.assertEqual(self.store.list_elements(), [])

    def test_missing_columns_preserve_values_and_blank_cells_clear(self):
        self.store.upsert_element({"id": "p", "type": "prompt", "name": "名称", "group": "原分组", "payload": {"template": "原文", "purpose": "article"}})
        import_table(self.store, "elements", "prompt", csv_bytes([["id", "名称", "模板"], ["p", "更新", ""]]), "test.csv")
        saved = self.store.get_element("p")
        self.assertEqual(saved["group"], "原分组")
        self.assertEqual(saved["payload"]["template"], "")
        self.assertEqual(saved["payload"]["purpose"], "article")

    def test_flow_graph_and_schedule_round_trip_and_new_rows_are_created(self):
        result = import_table(self.store, "flows", "", csv_bytes([["名称", "分组"], ["新流程", "组"]]), "flow.csv")
        self.assertEqual(result["created"], 1)
        flow = self.store.list_flows()[0]
        raw, filename, content_type = export_table(self.store, {"kind": "flows", "ids": [flow["id"]]})
        self.store.delete_flow(flow["id"])
        import_table(self.store, "flows", "", raw, filename)
        self.assertEqual(self.store.list_flows()[0]["graph"], flow["graph"])

    def test_new_result_copies_image_and_status_round_trips(self):
        image = Path(self.folder.name) / "source.png"
        image.write_bytes(b"image")
        imported = import_table(self.store, "results", "", csv_bytes([["名称", "路径", "状态"], ["图片", str(image), "已出作品"]]), "results.csv")
        self.assertEqual(imported["created"], 1)
        result = self.store.list_results(0)[0]
        self.assertTrue(result["worked"])
        self.assertTrue((Path(self.folder.name) / result["path"]).is_file())
        raw, filename, content_type = export_table(self.store, {"kind": "results", "ids": [result["id"]]})
        imported = import_table(self.store, "results", "", raw, filename)
        self.assertEqual(imported["updated"], 1)

    def test_runs_import_edit_delete_and_unlimited_loading(self):
        import_table(self.store, "runs", "", csv_bytes([["流程", "状态", "方式"], ["flow", "succeeded", "once"]]), "runs.csv")
        run = self.store.list_runs(limit=0)[0]
        self.store.update_run(run["id"], {"status": "failed", "error": "错误"})
        raw, filename, content_type = export_table(self.store, {"kind": "runs", "ids": [run["id"]]})
        self.store.delete_runs([run["id"]])
        import_table(self.store, "runs", "", raw, filename)
        self.assertEqual(self.store.get_run(run["id"])["error"], "错误")
        for number in range(105):
            self.store.add_run({"id": str(number), "flowId": "flow", "mode": "once", "status": "succeeded", "startedAt": "2026-11-06"})
        self.assertEqual(len(self.store.list_runs(limit=0)), 106)

    def test_excel_literals_stay_text_and_formula_import_is_rejected(self):
        self.store.upsert_element({"id": "p", "type": "prompt", "name": "=1+1", "payload": blank_payload("prompt")})
        raw, filename, content_type = export_table(self.store, {"kind": "elements", "type": "prompt", "ids": ["p"]})
        workbook = load_workbook(io.BytesIO(raw))
        self.assertEqual(workbook.active["B2"].data_type, "s")
        workbook.active["B2"] = "=1+1"
        output = io.BytesIO()
        workbook.save(output)
        with self.assertRaisesRegex(ValueError, "公式"):
            read_table(output.getvalue(), "test.xlsx")

    def test_export_selection_is_exact_and_secrets_are_excluded(self):
        for record_id in ("a", "b"):
            self.store.upsert_element({"id": record_id, "type": "model_profile", "name": record_id, "payload": blank_payload("model_profile")})
        self.store.set_secret("a", "private-test-key")
        raw, filename, content_type = export_table(self.store, {"kind": "elements", "type": "model_profile", "ids": ["a"], "format": "csv"})
        self.assertNotIn(b"private-test-key", raw)
        self.assertEqual([record["id"] for record in read_table(raw, filename)], ["a"])


if __name__ == "__main__":
    unittest.main()
