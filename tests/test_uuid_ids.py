"""UUID creation, atomic migration, historical links, and legacy imports."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.catalog import ELEMENT_DEFAULTS
from app.engine import Engine
from app.id_migration import migrate_uuid_ids
from app.ids import is_uuid_id
from app.packages import build_flow_package, import_package
from app.seed import seed_if_empty
from app.server import make_handler
from app.store import Store
from app.tables import import_table


class UUIDTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        environment = patch.dict(os.environ, {"MP_CONTENT_HOME": folder.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = Store(Path(folder.name) / "app.sqlite3")
        self.addCleanup(self.store.close)
        seed_if_empty(self.store)
        self.old_flows = self.store.list_flows()
        self.old_elements = self.store.list_elements()
        self.old_package = build_flow_package(self.store, self.store.get_flow("flow-cute-wallpaper"))

    def assert_graph(self, graph):
        node_ids = {node["id"] for node in graph["nodes"]}
        for node in graph["nodes"]:
            self.assertTrue(is_uuid_id("node", node["id"]))
            for imported in node.get("imports") or []:
                self.assertIsNotNone(self.store.get_element(imported["elementId"]))
            for bucket in ("rulesBefore", "rulesAfter"):
                for value in node.get(bucket) or []:
                    self.assertIsNotNone(self.store.get_element(value))
        for edge in graph["edges"]:
            self.assertTrue(is_uuid_id("edge", edge["id"]))
            self.assertIn(edge["from"], node_ids)
            self.assertIn(edge["to"], node_ids)
            for value in edge.get("rules") or []:
                self.assertIsNotNone(self.store.get_element(value))

    def test_migration_preserves_data_graphs_children_and_credentials(self):
        self.store.set_secrets("model-text-default", ["private-test-one", "private-test-two"])
        ref = self.store.get_element("ref-image-cute")
        ref["payload"]["useCount"] = 7
        self.store.upsert_element(ref)
        report = migrate_uuid_ids(self.store)
        self.assertTrue(report["migrated"])
        self.assertEqual(report["counts"]["flow"], len(self.old_flows))
        for old in self.old_elements:
            actual = self.store.get_element(old["id"])
            self.assertTrue(is_uuid_id(actual["type"], actual["id"]))
            for field in ("name", "group", "tags", "status", "fields"):
                self.assertEqual(actual[field], old[field])
            if old["id"] != "ref-image-cute":
                self.assertEqual(actual["payload"], old["payload"])
        self.assertEqual(self.store.get_element("ref-image-cute")["useCount"], 7)
        for old in self.old_flows:
            actual = self.store.get_flow(old["id"])
            self.assertTrue(is_uuid_id("flow", actual["id"]))
            self.assert_graph(actual["graph"])
            for field in ("name", "group", "tags", "fields", "status", "schedule", "updatedAt"):
                self.assertEqual(actual[field], old[field])
        self.assertEqual(self.store.get_secrets("model-text-default"), ["private-test-one", "private-test-two"])
        self.assertTrue(self.store.secret_status()[self.store.get_element("model-text-default")["id"]]["hasKey"])
        with closing(sqlite3.connect(report["backup"])) as backup:
            self.assertIsNotNone(backup.execute("SELECT id FROM flows WHERE id = 'flow-cute-wallpaper'").fetchone())
        before = (self.store.list_flows(), self.store.list_elements())
        seed_if_empty(self.store)
        self.assertFalse(migrate_uuid_ids(self.store)["migrated"])
        self.assertEqual(before, (self.store.list_flows(), self.store.list_elements()))

    def test_history_results_and_deleted_references_are_remapped_without_changing_text_or_paths(self):
        result = self.store.save_result({"id": "old-result", "flowId": "flow-cute-wallpaper", "runId": "old-run", "referenceId": "ref-image-cute", "path": "results/old-result.png", "prompt": "保留 old-result ref-image-cute 原文"})
        trace = {"order": ["n-prompts", "deleted-node"], "nodes": {"n-prompts": {"export": {"theme": {"domain": {"id": "domain-wallpaper"}, "themeId": "theme-cute-wallpaper"}, "prompt": {"templateId": "prompt-cute-wallpaper", "text": "n-prompts 原文"}, "referenceUsed": {"id": "ref-image-cute"}, "images": {"items": [{"configGroupId": "image-group-cute", "path": "work/old-run/image.png"}]}}}, "deleted-node": {"export": {"output": {"results": [result]}}}}, "output": {"zip": "outputs/old-run.zip"}}
        self.store.add_run({"id": "old-run", "flowId": "flow-cute-wallpaper", "mode": "once", "status": "succeeded", "startedAt": "2026-11-06", "trace": trace})
        self.store.add_run({"id": "orphan-run", "flowId": "deleted-flow", "mode": "once", "status": "failed", "startedAt": "2026-11-06", "trace": {"order": ["gone"], "nodes": {"gone": {}}}})
        migrate_uuid_ids(self.store)
        run = self.store.get_run("old-run")
        result = self.store.get_result("old-result")
        flow = self.store.get_flow("flow-cute-wallpaper")
        self.assertTrue(is_uuid_id("run", run["id"]))
        self.assertTrue(is_uuid_id("result", result["id"]))
        self.assertEqual(run["flowId"], flow["id"])
        self.assertEqual(result["runId"], run["id"])
        self.assertEqual(result["flowId"], flow["id"])
        self.assertEqual(result["referenceId"], self.store.get_element("ref-image-cute")["id"])
        self.assertEqual(result["path"], "results/old-result.png")
        self.assertEqual(result["prompt"], "保留 old-result ref-image-cute 原文")
        node = next(node for node in flow["graph"]["nodes"] if node["type"] == "prompt.from_refs")
        export = run["trace"]["nodes"][node["id"]]["export"]
        self.assertEqual(run["trace"]["order"][0], node["id"])
        self.assertEqual(export["referenceUsed"]["id"], result["referenceId"])
        self.assertEqual(export["theme"]["domain"]["id"], self.store.get_element("domain-wallpaper")["id"])
        self.assertEqual(export["prompt"]["text"], "n-prompts 原文")
        self.assertEqual(export["images"]["items"][0]["path"], "work/old-run/image.png")
        deleted = run["trace"]["order"][1]
        self.assertTrue(is_uuid_id("node", deleted))
        self.assertEqual(run["trace"]["nodes"][deleted]["export"]["output"]["results"][0]["id"], result["id"])
        self.assertTrue(is_uuid_id("flow", self.store.get_run("orphan-run")["flowId"]))

    def test_migration_error_rolls_back_ids_and_aliases(self):
        original = self.store.normalize_graph
        calls = 0
        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected migration failure")
            return original(*args)
        with patch.object(self.store, "normalize_graph", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                migrate_uuid_ids(self.store)
        self.assertEqual(self.store.list_flows(), self.old_flows)
        self.assertEqual(self.store.list_elements(), self.old_elements)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM id_aliases").fetchone()[0], 0)
        self.assertIsNone(self.store.get_setting("uuid_ids"))
        self.assertTrue(migrate_uuid_ids(self.store)["migrated"])

    def test_legacy_packages_and_tables_update_migrated_records_without_duplicates(self):
        migrate_uuid_ids(self.store)
        flow = self.store.get_flow("flow-cute-wallpaper")
        count = len(self.store.list_elements())
        imported = import_package(self.store, self.old_package)
        self.assertEqual(imported["flowId"], flow["id"])
        self.assertEqual(self.store.get_flow(flow["id"])["graph"], flow["graph"])
        self.assertEqual(len(self.store.list_elements()), count)
        result = import_table(self.store, "elements", "prompt", b'id,\xe5\x90\x8d\xe7\xa7\xb0\nprompt-cute-wallpaper,Updated\n', "old.csv")
        self.assertEqual(result["updated"], 1)
        self.assertEqual(self.store.get_element("prompt-cute-wallpaper")["name"], "Updated")
        result = import_table(self.store, "flows", "", 'id,名称\nexternal-old-flow,导入新流程\n'.encode(), "new.csv")
        self.assertEqual(result["created"], 1)
        self.assertTrue(is_uuid_id("flow", self.store.get_flow("external-old-flow")["id"]))
        self.assert_graph(self.store.get_flow("external-old-flow")["graph"])

    def test_new_records_and_copies_use_uuid_and_migrated_flow_runs_locally(self):
        migrate_uuid_ids(self.store)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store, Engine(self.store)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def post(path, body):
            request = Request(f"http://127.0.0.1:{server.server_port}" + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=10) as response:
                return json.load(response)
        flow = post("/api/flows", {"name": "新流程"})
        self.assertTrue(is_uuid_id("flow", flow["id"]))
        self.assert_graph(flow["graph"])
        for kind in ELEMENT_DEFAULTS:
            element = post("/api/elements", {"type": kind, "name": "新元素"})
            self.assertTrue(is_uuid_id(kind, element["id"]))
        run = post("/api/runs", {"flowId": flow["id"]})
        self.assertTrue(is_uuid_id("run", run["id"]))
        copies = post("/api/flows/copy", {"ids": ["flow-cute-wallpaper"], "count": 3})["flows"]
        all_nodes = set()
        for flow in copies:
            self.assertTrue(is_uuid_id("flow", flow["id"]))
            self.assert_graph(flow["graph"])
            ids = {node["id"] for node in flow["graph"]["nodes"]}
            self.assertFalse(all_nodes & ids)
            all_nodes.update(ids)
        run = Engine(self.store).run("flow-wallpaper-article")
        self.assertEqual(run["status"], "succeeded", run["error"])
        self.assertTrue(is_uuid_id("run", run["id"]))
        self.assertEqual(run["flowId"], self.store.get_flow("flow-wallpaper-article")["id"])

    def test_legacy_package_import_into_an_empty_store_generates_uuid_and_keeps_links(self):
        store = Store(self.store.path.parent / "import.sqlite3")
        self.addCleanup(store.close)
        imported = import_package(store, self.old_package)
        flow = store.get_flow(imported["flowId"])
        self.assertTrue(is_uuid_id("flow", flow["id"]))
        for element in store.list_elements():
            self.assertTrue(is_uuid_id(element["type"], element["id"]))
        for node in flow["graph"]["nodes"]:
            for item in node.get("imports") or []:
                self.assertIsNotNone(store.get_element(item["elementId"]))


if __name__ == "__main__":
    unittest.main()
