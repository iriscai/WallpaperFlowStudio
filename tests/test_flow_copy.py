"""Independent flow copies with collision-free appended names and IDs."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine import Engine
from app.ids import is_uuid_id
from app.server import make_handler
from app.store import Store


class FlowCopyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(Path(self.folder.name) / "app.sqlite3")
        self.addCleanup(self.store.close)
        self.source = self.store.upsert_flow({
            "id": "flow-source",
            "name": "壁纸流程",
            "description": "说明",
            "group": "壁纸",
            "tags": ["标签"],
            "status": "停用",
            "fields": [{"key": "账号", "value": "账号1"}],
            "graph": {"nodes": [{"id": "node1", "type": "start.theme", "params": {"themeText": "海边"}, "imports": [{"elementId": "theme-shared"}]}, {"id": "node2", "type": "text.article", "params": {"keepInlet": True}, "imports": []}], "edges": [{"id": "edge1", "from": "node1", "to": "node2", "carry": [{"source": "theme", "target": "theme", "op": "copy"}], "rules": ["shared-rule"]}]},
            "schedule": {"enabled": True, "frequency": "daily", "time": "09:00", "weekdays": [], "date": "", "nextRunAt": "2000-01-01T00:00:00+00:00"},
        })

    def assert_graph_copy(self, graph):
        source = self.source["graph"]
        mapping = {old["id"]: new["id"] for old, new in zip(source["nodes"], graph["nodes"])}
        self.assertEqual(len(mapping), len(source["nodes"]))
        for old, new in zip(source["nodes"], graph["nodes"]):
            self.assertTrue(is_uuid_id("node", new["id"]))
            self.assertEqual({k: v for k, v in old.items() if k != "id"}, {k: v for k, v in new.items() if k != "id"})
        for old, new in zip(source["edges"], graph["edges"]):
            self.assertTrue(is_uuid_id("edge", new["id"]))
            self.assertEqual(new["from"], mapping[old["from"]])
            self.assertEqual(new["to"], mapping[old["to"]])
            self.assertEqual(new["carry"], old["carry"])
            self.assertEqual(new["rules"], old["rules"])

    def test_copy_n_appends_names_and_generates_independent_uuid_ids(self):
        copies = self.store.copy_flows([self.source["id"]], 3)
        self.assertEqual([flow["name"] for flow in copies], ["壁纸流程-副本1", "壁纸流程-副本2", "壁纸流程-副本3"])
        self.assertEqual(len({flow["id"] for flow in copies}), 3)
        for flow in copies:
            self.assertTrue(is_uuid_id("flow", flow["id"]))
            self.assert_graph_copy(flow["graph"])
            for key in ("description", "group", "tags", "status", "fields"):
                self.assertEqual(flow[key], self.source[key])
            self.assertFalse(flow["schedule"]["enabled"])
            self.assertIsNone(flow["schedule"]["nextRunAt"])
            self.assertEqual(flow["schedule"]["time"], "09:00")
        self.assertEqual(self.store.get_flow(self.source["id"]), self.source)
        copies[0]["graph"]["nodes"][0]["params"]["themeText"] = "副本主题"
        self.store.upsert_flow(copies[0])
        self.assert_graph_copy(self.store.get_flow(copies[1]["id"])["graph"])
        self.assertEqual(self.store.get_flow(self.source["id"]), self.source)

    def test_repeated_copy_continues_and_name_or_id_collision_skips_suffix(self):
        self.store.copy_flows([self.source["id"]], 2)
        collision = copy.deepcopy(self.source)
        collision.update({"id": "another-id", "name": "壁纸流程-副本3"})
        self.store.upsert_flow(collision)
        collision.update({"id": "flow-source-copy-4", "name": "其他流程"})
        self.store.upsert_flow(collision)
        created = self.store.copy_flows([self.source["id"]], 2)
        self.assertEqual([flow["name"] for flow in created], ["壁纸流程-副本4", "壁纸流程-副本5"])
        self.assertTrue(all(is_uuid_id("flow", flow["id"]) for flow in created))
        self.assertEqual(self.store.get_flow("flow-source-copy-4")["name"], "其他流程")

    def test_multiple_sources_deduplicate_ids_and_copy_each_n_times(self):
        other = copy.deepcopy(self.source)
        other.update({"id": "second", "name": "第二条"})
        self.store.upsert_flow(other)
        copies = self.store.copy_flows([self.source["id"], "second", self.source["id"]], 2)
        self.assertEqual(len(copies), 4)
        self.assertEqual([flow["name"] for flow in copies], ["壁纸流程-副本1", "壁纸流程-副本2", "第二条-副本1", "第二条-副本2"])
        self.assertEqual(len({flow["id"] for flow in copies}), 4)
        self.assertTrue(all(is_uuid_id("flow", flow["id"]) for flow in copies))

    def test_keep_schedule_recalculates_next_run(self):
        created = self.store.copy_flows([self.source["id"]], 1, True)[0]
        self.assertTrue(created["schedule"]["enabled"])
        self.assertGreater(datetime.fromisoformat(created["schedule"]["nextRunAt"]), datetime.now(timezone.utc))
        self.assertEqual(self.store.get_flow(self.source["id"]), self.source)

    def test_bad_counts_and_missing_source_do_not_create_records(self):
        for count in (0, -1, 1.5, True, "2", None, 1001):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    self.store.copy_flows([self.source["id"]], count)
        for ids in ([], None, "flow-source", [None], [""], [self.source["id"], "missing"]):
            with self.subTest(ids=ids):
                with self.assertRaises(ValueError):
                    self.store.copy_flows(ids, 1)
        self.assertEqual(len(self.store.list_flows()), 1)

    def test_write_error_rolls_back_every_copy(self):
        original = self.store.upsert_flow
        def fail_second(flow):
            if flow["name"].endswith("-副本2"):
                raise RuntimeError("write failed")
            return original(flow)
        with patch.object(self.store, "upsert_flow", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.store.copy_flows([self.source["id"]], 3)
        self.assertEqual(self.store.list_flows(), [self.source])

    def test_concurrent_copy_requests_do_not_overwrite_each_other(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(self.store.copy_flows, [self.source["id"]], 3) for request_index in range(3)]
            copies = [flow for future in futures for flow in future.result()]
        self.assertEqual(len({flow["id"] for flow in copies}), 9)
        self.assertEqual(len({flow["name"] for flow in copies}), 9)
        self.assertEqual(len(self.store.list_flows()), 10)

    def test_copy_endpoint_returns_new_records_and_reports_bad_input(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store, Engine(self.store)))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = "http://127.0.0.1:" + str(server.server_port) + "/api/flows/copy"
        request = Request(url, data=json.dumps({"ids": [self.source["id"]], "count": 2}).encode(), headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            result = json.load(response)
        self.assertEqual(result["count"], 2)
        self.assertTrue(is_uuid_id("flow", result["flows"][0]["id"]))
        with self.assertRaises(HTTPError) as failure:
            urlopen(Request(url, data=b'{"ids":["flow-source"],"count":0}', headers={"Content-Type": "application/json"}))
        self.assertEqual(failure.exception.code, 400)
        failure.exception.close()


if __name__ == "__main__":
    unittest.main()
