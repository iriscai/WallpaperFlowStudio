"""Credential failover against an isolated local model API; no paid requests."""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

from image_fixtures import png_bytes

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine import Engine
from app.providers import KeyPool, ProviderError, _http_error, chat, generate_image
from app.seed import seed_if_empty
from app.server import make_handler
from app.store import Store


class KeyPoolTests(unittest.TestCase):
    def test_key_errors_and_quota_codes_are_recognized(self):
        for status, body in [
            (403, {"error": {"message": "用户额度不足, 剩余额度: ＄-0.524818", "code": "insufficient_user_quota"}}),
            (429, {"error": {"message": "billing exhausted", "code": "insufficient_quota"}}),
            (400, {"error": {"message": "令牌额度不足"}}),
            (401, {"error": {"message": "invalid key"}}),
            (402, {"error": {"message": "payment required"}}),
            (403, {"error": {"message": "permission denied"}}),
        ]:
            with self.subTest(status=status, body=body):
                error = _http_error(status, json.dumps(body, ensure_ascii=False))
                self.assertTrue(error.key_unavailable)
                self.assertEqual(error.status, status)
        for status in (400, 404, 429, 500, 502):
            self.assertFalse(_http_error(status, '{"error":{"message":"other error"}}').key_unavailable)

    def test_concurrent_failover_and_later_jobs_skip_rejected_key(self):
        pool = KeyPool(["bad", "good"])
        barrier = threading.Barrier(2)
        calls = []
        lock = threading.Lock()

        def operation(key):
            with lock:
                calls.append(key)
            if key == "bad":
                barrier.wait(timeout=5)
                raise ProviderError("用户额度不足", status=403)
            return "done"

        with ThreadPoolExecutor(max_workers=2) as workers:
            jobs = [workers.submit(pool.call, operation) for _ in range(2)]
            self.assertEqual([job.result(timeout=10) for job in jobs], ["done", "done"])
            later = list(workers.map(lambda _: pool.call(operation), range(8)))
        self.assertEqual(later, ["done"] * 8)
        self.assertEqual(calls.count("bad"), 2)  # Both were already in flight.
        self.assertEqual(calls.count("good"), 10)

    def test_exhaustion_is_bounded_and_redacts_keys_from_error(self):
        keys = ["sk-private-one", "sk-private-two", "sk-private-three"]
        pool = KeyPool(keys + [keys[0]])
        calls = []

        def operation(key):
            calls.append(key)
            raise ProviderError("余额不足 " + key, status=403)

        for _ in range(2):
            with self.assertRaises(ProviderError) as caught:
                pool.call(operation, start_index=1)
            self.assertIn("全部 3 条密钥", str(caught.exception))
            self.assertTrue(all(key not in str(caught.exception) for key in keys))
        self.assertEqual(calls, [keys[1], keys[2], keys[0]])

    def test_bad_request_is_not_replayed_with_another_key(self):
        calls = []
        pool = KeyPool(["one", "two"])

        def operation(key):
            calls.append(key)
            raise ProviderError("invalid image size", status=400)

        with self.assertRaisesRegex(ProviderError, "invalid image size"):
            pool.call(operation)
        self.assertEqual(calls, ["one"])

    def test_round_robin_start_and_wrap_to_remaining_key(self):
        pool = KeyPool(["good", "bad"])
        calls = []

        def operation(key):
            calls.append(key)
            if key == "bad":
                raise ProviderError("invalid key", status=401)
            return key

        self.assertEqual(pool.call(operation, start_index=1), "good")
        self.assertEqual(calls, ["bad", "good"])


class ModelFailoverTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        environment = patch.dict(os.environ, {"MP_CONTENT_HOME": folder.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = Store(Path(folder.name) / "app.sqlite3")
        self.addCleanup(self.store.close)
        seed_if_empty(self.store)
        self.engine = Engine(self.store)
        self.requests = []
        self.request_lock = threading.Lock()
        self.failures = {
            "sk-text-empty": (403, "insufficient_user_quota"),
            "sk-image-empty": (403, "insufficient_user_quota"),
        }
        self.transient_failures = 0
        self.generated_blob = png_bytes()
        test = self

        class ModelAPI(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                key = self.headers["Authorization"].removeprefix("Bearer ")
                with test.request_lock:
                    test.requests.append({"key": key, "body": body, "path": self.path})
                    failure = test.failures.get(key)
                    if self.path.endswith("images/generations") and test.transient_failures:
                        test.transient_failures -= 1
                        failure = (503, "service_unavailable")
                status = 200
                if failure:
                    status, code = failure
                    response = {"error": {"message": "用户额度不足" if "quota" in code else code, "code": code}}
                elif self.path.endswith("images/generations"):
                    response = {"data": [{"b64_json": base64.b64encode(test.generated_blob).decode()}]}
                else:
                    response = {"choices": [{"message": {"content": "测试模型完成内容"}}]}
                encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), ModelAPI)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base_url = f"http://127.0.0.1:{server.server_port}/v1"
        for kind in ("text", "image"):
            profile = self.store.get_element(f"model-{kind}-default")
            profile["payload"].update(baseUrl=self.base_url, model=f"{kind}-test", concurrency=1)
            self.store.upsert_element(profile)
            self.store.set_secrets(profile["id"], [f"sk-{kind}-empty", f"sk-{kind}-working"])

    def test_reference_prompts_and_images_retry_same_requests_and_finish_flow(self):
        with patch("app.providers.time.sleep") as sleep:
            run = self.engine.run("flow-cute-wallpaper")
        self.assertEqual(run["status"], "succeeded", run["error"])
        sleep.assert_not_called()
        for kind in ("text", "image"):
            requests = [item for item in self.requests if item["body"]["model"] == f"{kind}-test"]
            self.assertEqual([item["key"] for item in requests], [f"sk-{kind}-empty"] + [f"sk-{kind}-working"] * 5)
            self.assertEqual(requests[0]["body"], requests[1]["body"])
        traced = run["trace"]["nodes"]
        for node_id in ("n-prompts", "n-image"):
            self.assertIn("自动跳过 1 条", traced[node_id]["message"])
        self.assertEqual(len(traced["n-image"]["export"]["images"]["items"]), 5)
        self.assertTrue(Path(traced["n-output"]["export"]["output"]["zip"]).is_file())

    def test_compose_titles_and_articles_share_failed_keys_across_nodes(self):
        flow = self.store.get_flow("flow-wallpaper-article")
        flow["graph"]["nodes"] = [node for node in flow["graph"]["nodes"] if node["type"] in {"prompt.compose", "text.title", "text.article"}]
        flow["graph"]["edges"] = []
        compose = flow["graph"]["nodes"][0]
        compose["params"]["useModel"] = True
        compose["imports"].append({"elementId": "model-text-default", "elementType": "model_profile"})
        self.store.upsert_flow(flow)
        run = self.engine.run(flow["id"])
        self.assertEqual(run["status"], "succeeded", run["error"])
        self.assertEqual([item["key"] for item in self.requests], ["sk-text-empty"] + ["sk-text-working"] * 3)
        self.assertTrue(all(node["provider"] == "api" for node in run["trace"]["nodes"].values()))

    def test_all_keys_empty_stops_and_next_run_rechecks_after_recharge(self):
        self.failures["sk-text-working"] = (403, "insufficient_user_quota")
        run = self.engine.run("flow-cute-wallpaper")
        self.assertEqual(run["status"], "failed")
        self.assertIn("全部 2 条密钥", run["error"])
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn("sk-text-", json.dumps(run))
        self.failures.pop("sk-text-empty")
        self.requests.clear()
        run = self.engine.run("flow-cute-wallpaper")
        self.assertEqual(run["status"], "succeeded", run["error"])
        self.assertEqual(self.requests[0]["key"], "sk-text-empty")

    def test_quota_reported_as_429_switches_images_without_sleeping(self):
        self.failures["sk-image-empty"] = (429, "insufficient_quota")
        profile = self.store.get_element("model-image-default")
        with patch("app.providers.time.sleep") as sleep:
            result = KeyPool(self.store.get_secrets(profile["id"])).call(
                lambda key: generate_image(profile, key, "相同提示词", 1080, 1920),
            )
        self.assertEqual(result[1], "1080x1920")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0]["body"], self.requests[1]["body"])
        sleep.assert_not_called()

    def test_transient_image_error_keeps_existing_retry_on_same_key(self):
        self.transient_failures = 1
        profile = self.store.get_element("model-image-default")
        with patch("app.providers.time.sleep") as sleep:
            generate_image(profile, "sk-image-working", "提示词", 1080, 1920)
        self.assertEqual([item["key"] for item in self.requests], ["sk-image-working"] * 2)
        sleep.assert_called_once_with(8)

    def test_text_interface_test_also_uses_fallback_key(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store, self.engine))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = Request(f"http://127.0.0.1:{server.server_port}/api/elements/model-text-default/test", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=10) as response:
            result = json.load(response)
        self.assertTrue(result["ok"])
        self.assertEqual([item["key"] for item in self.requests], ["sk-text-empty", "sk-text-working"])


if __name__ == "__main__":
    unittest.main()
