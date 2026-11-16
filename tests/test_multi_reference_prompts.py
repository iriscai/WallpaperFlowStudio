"""Multi-reference sampling, multimodal requests and downstream work packages.

All model calls are mocked or served on loopback; no paid requests.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from image_fixtures import png_bytes

from app.catalog import blank_node
from app.engine import Engine
from app.id_migration import rewrite_document
from app.ids import is_uuid_id, new_id
from app.store import Store


class MultiReferenceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="mp-multi-ref-")
        self.addCleanup(folder.cleanup)
        environment = patch.dict(os.environ, MP_CONTENT_HOME=folder.name)
        environment.start()
        self.addCleanup(environment.stop)
        self.root = Path(folder.name)
        self.store = Store(self.root / "app.sqlite3")
        self.addCleanup(self.store.close)
        self.engine = Engine(self.store)
        self.eligible = [self.reference(f"头像{index}") for index in range(4)]
        self.hidden_group = self.reference("壁纸候选", group="壁纸")
        self.hidden_tag = self.reference("森系候选", tags=["森系"])
        self.disabled = self.reference("停用候选", status="停用")
        self.unchecked = self.reference("没有勾选的候选")
        self.text = self.store.upsert_element({
            "type": "model_profile", "name": "本地生文", "payload": {
                "kind": "text", "model": "test-text", "concurrency": 3,
            },
        })
        self.store.set_secret(self.text["id"], "test-local-key")

    def reference(self, name, group="头像", tags=None, status="启用", note=None):
        path = self.root / (name + ".png")
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
        return self.store.upsert_element({
            "type": "reference_image", "name": name, "group": group,
            "tags": ["可爱"] if tags is None else tags, "status": status,
            "payload": {"path": str(path), "note": name + "说明" if note is None else note, "useCount": 4},
        })

    def flow(self, *, references=None, node_type="prompt.from_multi_refs", **params):
        node = blank_node(node_type, new_id("node"), 0, 0)
        node["params"].update(refGroup="头像", refTag="可爱", count=3)
        node["params"].update(params)
        if references is None:
            references = self.eligible + [self.hidden_group, self.hidden_tag, self.disabled, self.eligible[0]]
        node["imports"] = [{"elementId": item["id"], "elementType": item["type"]} for item in references + [self.text]]
        flow = self.store.upsert_flow({"name": "多图测试", "graph": {"nodes": [node], "edges": []}})
        return flow, node

    def assert_usage(self, selected, increment=1):
        for item in self.eligible + [self.hidden_group, self.hidden_tag, self.disabled, self.unchecked]:
            self.assertEqual(self.store.get_element(item["id"])["useCount"], 4 + (increment if item["id"] in selected else 0))

    def test_exact_unique_filtered_checked_images_shared_across_independent_prompt_count(self):
        flow, node = self.flow(referenceCount=2)
        with patch("app.nodes.chat", return_value="方形头像 1:1") as model:
            run = self.engine.run(flow["id"])
        self.assertEqual(run["status"], "succeeded", run["error"])
        export = run["trace"]["nodes"][node["id"]]["export"]
        references = export["referencesUsed"]
        selected = {item["id"] for item in references}
        self.assertEqual(len(references), 2)
        self.assertEqual(len(selected), 2)
        self.assertTrue(selected <= {item["id"] for item in self.eligible})
        self.assertEqual(len(export["prompts"]), 3)
        self.assertEqual(export["referenceUsed"], references[0])
        self.assertEqual(export["referenceNames"], "、".join(item["name"] for item in references))
        self.assertEqual(export["referenceNotes"], "；".join(item["note"] for item in references))
        self.assertEqual(model.call_count, 3)
        for call in model.call_args_list:
            self.assertEqual(call.kwargs["images"], [item["path"] for item in references])
            for index, ref in enumerate(references, 1):
                self.assertIn(f"参考图{index}《{ref['name']}》", call.args[2])
                self.assertIn(ref["note"], call.args[2])
            self.assertIn("共同参考 2 张图片", call.args[2])
        self.assert_usage(selected)
        self.assertEqual([item["useCount"] for item in references], [5, 5])

    def test_one_reference_and_maximum_reference_count(self):
        many = self.eligible + [self.reference(f"补充{index}") for index in range(8)]
        for number in (1, 12):
            with self.subTest(number=number):
                flow, node = self.flow(references=many, referenceCount=number, count=1)
                with patch("app.nodes.chat", return_value="测试提示词") as model:
                    run = self.engine.run(flow["id"])
                self.assertEqual(run["status"], "succeeded", run["error"])
                self.assertEqual(len(model.call_args.kwargs["images"]), number)
                self.assertEqual(len(run["trace"]["nodes"][node["id"]]["export"]["prompts"]), 1)

    def test_invalid_reference_counts_reject_before_api_and_usage(self):
        for number in (0, -1, 13, True, False, 1.5, "2.5", "", "abc", None):
            with self.subTest(number=number):
                flow, _ = self.flow(referenceCount=number)
                with patch("app.nodes.chat") as model:
                    run = self.engine.run(flow["id"])
                self.assertEqual(run["status"], "failed")
                self.assertIn("N 必须为 1 到 12 的整数", run["error"])
                model.assert_not_called()
        self.assert_usage(set())

    def test_insufficient_including_article_only_and_duplicate_imports_reject_before_api(self):
        article = self.store.upsert_element({"type": "reference_article", "name": "文章", "payload": {"body": "正文"}})
        for refs in ([], [article], [self.eligible[0]] * 3,
                     [self.eligible[0], self.disabled, self.hidden_group, self.hidden_tag]):
            with self.subTest(refs=[ref["name"] for ref in refs]):
                flow, _ = self.flow(references=refs, referenceCount=2)
                with patch("app.nodes.chat") as model:
                    run = self.engine.run(flow["id"])
                self.assertEqual(run["status"], "failed")
                self.assertIn("不够随机抽取 2 张", run["error"])
                model.assert_not_called()
        self.assert_usage(set())

    def test_missing_one_selected_file_rejects_whole_node_without_model_calls(self):
        refs = self.eligible[:2]
        Path(refs[1]["payload"]["path"]).unlink()
        flow, _ = self.flow(references=refs, referenceCount=2)
        with patch("app.nodes.chat") as model:
            run = self.engine.run(flow["id"])
        self.assertEqual(run["status"], "failed")
        self.assertIn("参考图文件找不到", run["error"])
        self.assertIn(refs[1]["name"], run["error"])
        model.assert_not_called()
        self.assert_usage(set())

    def test_empty_or_failed_generation_does_not_increment_any_reference(self):
        flow, _ = self.flow(referenceCount=2)
        for result in ("", RuntimeError("模拟接口失败")):
            with self.subTest(result=result):
                kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with patch("app.nodes.chat", **kwargs):
                    run = self.engine.run(flow["id"])
                self.assertEqual(run["status"], "failed")
                self.assert_usage(set())

    def test_blank_notes_are_optional_and_omitted_from_joined_text(self):
        blank = self.reference("说明可空", note="")
        flow, node = self.flow(references=[self.eligible[0], blank], referenceCount=2)
        with patch("app.nodes.chat", return_value="提示词"):
            run = self.engine.run(flow["id"])
        self.assertEqual(run["status"], "succeeded", run["error"])
        export = run["trace"]["nodes"][node["id"]]["export"]
        self.assertEqual(export["referenceNotes"], self.eligible[0]["payload"]["note"])
        self.assertEqual(len(export["referencesUsed"]), 2)

    def test_original_single_reference_node_retains_sampling_and_export_behavior(self):
        flow, node = self.flow(node_type="prompt.from_refs")
        with patch("app.nodes.random.choice", return_value=self.hidden_group), patch("app.nodes.chat", return_value="提示词") as model:
            run = self.engine.run(flow["id"])
        self.assertEqual(run["status"], "succeeded", run["error"])
        export = run["trace"]["nodes"][node["id"]]["export"]
        self.assertEqual(export["referenceUsed"]["id"], self.hidden_group["id"])
        self.assertNotIn("referencesUsed", export)
        self.assertTrue(all(len(call.kwargs["images"]) == 1 for call in model.call_args_list))
        self.assert_usage({self.hidden_group["id"]})

    def test_joined_description_reaches_image_library_and_named_zip(self):
        flow, refs = self.flow(references=self.eligible[:2], referenceCount=2,
                               carryParams=[{"source": "referenceNotes", "target": "${title}"}])
        image_profile = self.store.upsert_element({"type": "model_profile", "name": "生图", "payload": {"kind": "image", "model": "test-image"}})
        self.store.set_secret(image_profile["id"], "local-image-key")
        image = blank_node("image.generate", new_id("node"), 300, 0)
        image["imports"] = [{"elementId": image_profile["id"]}]
        library = blank_node("file.output", new_id("node"), 600, 0)
        library["params"].update(saveAs="library", keepInlet=True)
        work = blank_node("work.generate", new_id("node"), 900, 0)
        nodes = [refs, image, library, work]
        edges = []
        for source, target, fields in ((refs, image, ["prompts", "referenceUsed", "referencesUsed"]),
                                       (image, library, ["images"]), (library, work, ["images"])):
            edges.append({"id": new_id("edge"), "from": source["id"], "to": target["id"],
                          "carry": [{"source": field, "target": field, "op": "copy"} for field in fields]})
        flow["graph"] = {"nodes": nodes, "edges": edges}
        self.store.upsert_flow(flow)
        generated = png_bytes()
        with patch("app.nodes.chat", return_value="方形头像 1:1"), patch("app.nodes.generate_image", return_value=(generated, "1080x1920")):
            run = self.engine.run(flow["id"])
        self.assertEqual(run["status"], "succeeded", run["error"])
        trace = run["trace"]["nodes"]
        export = trace[refs["id"]]["export"]
        title = export["referenceNotes"]
        for node in nodes[1:]:
            self.assertEqual(trace[node["id"]]["inlet"]["title"], title)
        self.assertEqual(trace[image["id"]]["export"]["images"]["referencesUsed"], export["referencesUsed"])
        records = self.store.list_results(limit=0)
        self.assertEqual(len(records), 3)
        self.assertTrue(all(record["referenceId"] == export["referenceUsed"]["id"] for record in records))
        self.assertTrue(all(record["group"] == "头像" and record["tags"] == ["可爱"] for record in records))
        output = trace[work["id"]]["export"]["output"]
        self.assertEqual(Path(output["folder"]).name, title)
        self.assertEqual(Path(output["zip"]).stem, title)
        with zipfile.ZipFile(output["zip"]) as archive:
            self.assertEqual(archive.namelist(), [f"{title}/{index:02}.png" for index in range(1, 4)])
            self.assertTrue(all(archive.read(name) == generated for name in archive.namelist()))

    def test_real_multimodal_requests_run_concurrently_and_switch_quota_key(self):
        requests = []
        lock = threading.Lock()
        concurrent = threading.Barrier(3)

        class LocalModel(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                key = self.headers["Authorization"].removeprefix("Bearer ")
                with lock:
                    requests.append((key, body))
                if key == "empty-local-key":
                    status = 403
                    response = {"error": {"code": "insufficient_user_quota", "message": "余额不足"}}
                else:
                    concurrent.wait(timeout=8)
                    status = 200
                    response = {"choices": [{"message": {"content": "多参考图提示词 1:1"}}]}
                encoded = json.dumps(response).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalModel)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.text["payload"]["baseUrl"] = f"http://127.0.0.1:{server.server_port}/v1"
        self.store.upsert_element(self.text)
        self.store.set_secrets(self.text["id"], ["empty-local-key", "working-local-key"])
        flow, node = self.flow(referenceCount=2)
        run = self.engine.run(flow["id"])
        self.assertEqual(run["status"], "succeeded", run["error"])
        traced = run["trace"]["nodes"][node["id"]]
        selected = traced["export"]["referencesUsed"]
        expected = [base64.b64encode(Path(item["path"]).read_bytes()).decode() for item in selected]
        self.assertEqual(sum(key == "working-local-key" for key, _ in requests), 3)
        self.assertIn("自动跳过 1 条", traced["message"])
        for key, body in requests:
            self.assertEqual(body["model"], "test-text")
            contents = body["messages"][-1]["content"]
            self.assertEqual([part["type"] for part in contents], ["text", "image_url", "image_url"])
            self.assertEqual([part["image_url"]["url"].split(",", 1)[1] for part in contents[1:]], expected)
            if key == "empty-local-key":
                self.assertIn(("working-local-key", body), requests)
        self.assert_usage({item["id"] for item in selected})

    def test_plural_reference_ids_rewrite_with_singular_compatibility(self):
        original = {"referencesUsed": [{"id": "old-ref-1", "note": "保留说明"}, {"id": "old-ref-2"}],
                    "images": {"referencesUsed": [{"id": "old-ref-1"}]}, "referenceUsed": {"id": "old-ref-1"}}
        rewritten = rewrite_document(self.store, original, "")
        for item in rewritten["referencesUsed"]:
            self.assertTrue(is_uuid_id("reference_image", item["id"]))
        self.assertEqual(rewritten["referenceUsed"]["id"], rewritten["referencesUsed"][0]["id"])
        self.assertEqual(rewritten["images"]["referencesUsed"][0]["id"], rewritten["referenceUsed"]["id"])
        self.assertEqual(rewritten["referencesUsed"][0]["note"], "保留说明")


if __name__ == "__main__":
    unittest.main()
