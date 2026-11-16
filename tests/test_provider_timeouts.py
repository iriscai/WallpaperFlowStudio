"""Bounded transport retries, including a real stalled response on loopback."""

from __future__ import annotations

import base64
import http.client
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from app.engine import Engine
from app.providers import KeyPool, ProviderError, chat, generate_image
from app.seed import seed_if_empty
from app.store import Store


class ProviderTimeoutTests(unittest.TestCase):
    def profile(self, kind):
        return {"id": kind, "payload": {"kind": kind, "model": "local-test", "baseUrl": "http://localhost.test/v1"}}

    def response(self, payload):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return response

    def completion(self):
        return self.response({"choices": [{"message": {"content": "恢复成功"}}]})

    def test_connection_timeouts_exhaust_with_context_without_disabling_key(self):
        pool = KeyPool(["local-key-one", "local-key-two"])
        with patch("app.providers.urllib.request.urlopen", side_effect=[TimeoutError("The read operation timed out")] * 3 + [self.completion()]) as request, patch("app.providers.time.sleep") as sleep:
            with self.assertRaises(ProviderError) as caught:
                pool.call(lambda key: chat(self.profile("text"), key, "原始请求"))
            self.assertIn("生文接口读取超时", str(caught.exception))
            self.assertIn("300 秒", str(caught.exception))
            self.assertIn("已尝试 3 次", str(caught.exception))
            self.assertFalse(caught.exception.key_unavailable)
            self.assertEqual(pool.switch_message(), "")
            # A transient transport failure must not permanently reject this key.
            self.assertEqual(pool.call(lambda key: chat(self.profile("text"), key, "下一条")), "恢复成功")
        self.assertEqual(request.call_count, 4)
        self.assertTrue(all(item.kwargs["timeout"] == 300 for item in request.call_args_list))
        self.assertTrue(all(item.args[0].get_header("Authorization") == "Bearer local-key-one" for item in request.call_args_list))
        self.assertEqual(sleep.call_args_list, [call(5), call(10)])

    def test_response_read_failures_retry_same_image_request_with_longer_default(self):
        failed = self.response(b"")
        failed.read.side_effect = TimeoutError("The read operation timed out")
        blob = b"mock-image-bytes"
        completed = self.response({"data": [{"b64_json": base64.b64encode(blob).decode()}]})
        with patch("app.providers.urllib.request.urlopen", side_effect=[failed, completed]) as request, patch("app.providers.time.sleep") as sleep:
            self.assertEqual(generate_image(self.profile("image"), "local-key", "原始图片提示词", 1080, 1920), (blob, "1080x1920"))
        self.assertEqual(request.call_count, 2)
        requests = [item.args[0] for item in request.call_args_list]
        self.assertEqual(requests[0].data, requests[1].data)
        self.assertEqual(json.loads(requests[1].data)["prompt"], "原始图片提示词")
        self.assertTrue(all(item.kwargs["timeout"] == 600 for item in request.call_args_list))
        failed.__exit__.assert_called_once()
        sleep.assert_called_once_with(5)

    def test_wrapped_timeouts_disconnects_and_truncated_responses_recover(self):
        for problem in (urllib.error.URLError(TimeoutError("timed out")),
                        ConnectionResetError("connection reset"),
                        http.client.RemoteDisconnected("remote closed"),
                        http.client.IncompleteRead(b"partial", 20)):
            with self.subTest(problem=problem):
                with patch("app.providers.urllib.request.urlopen", side_effect=[problem, self.completion()]) as request, patch("app.providers.time.sleep"):
                    self.assertEqual(chat(self.profile("text"), "local-key", "相同请求"), "恢复成功")
                self.assertEqual(request.call_count, 2)

    def test_image_download_retries_get_without_repeating_generation(self):
        image_url = "http://localhost.test/ready.png"
        generated = self.response({"data": [{"url": image_url}]})
        stalled = self.response(b"")
        stalled.read.side_effect = TimeoutError("read timed out")
        image = self.response(b"complete-generated-image")
        with patch("app.providers.urllib.request.urlopen", side_effect=[generated, stalled, image]) as request, patch("app.providers.time.sleep"):
            blob, _ = generate_image(self.profile("image"), "local-key", "提示词", 1024, 1024)
        self.assertEqual(blob, b"complete-generated-image")
        self.assertEqual([item.args[0].get_method() for item in request.call_args_list], ["POST", "GET", "GET"])
        self.assertEqual([item.args[0].full_url for item in request.call_args_list[1:]], [image_url, image_url])

    def test_download_and_generation_timeout_errors_are_distinguishable(self):
        for download in (False, True):
            with self.subTest(download=download):
                generated = [self.response({"data": [{"url": "http://localhost.test/out.png"}]})] if download else []
                with patch("app.providers.urllib.request.urlopen", side_effect=generated + [TimeoutError("read timed out")] * 3) as request, patch("app.providers.time.sleep"):
                    with self.assertRaises(ProviderError) as caught:
                        generate_image(self.profile("image"), "local-key", "提示词", 1024, 1024)
                self.assertIn("生成图片下载读取超时" if download else "生图接口读取超时", str(caught.exception))
                self.assertIn("300 秒" if download else "600 秒", str(caught.exception))
                self.assertEqual(request.call_count, 4 if download else 3)

    def test_real_stalled_response_body_retries_and_keeps_multimodal_payload(self):
        requests = []
        release_first = threading.Event()

        class LocalAPI(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                requests.append(body)
                encoded = json.dumps({"choices": [{"message": {"content": "本地模拟完成"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                if len(requests) == 1:
                    self.wfile.flush()
                    release_first.wait(timeout=2)
                try:
                    self.wfile.write(encoded)
                except OSError:
                    pass

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalAPI)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(release_first.set)
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "reference.png"
            image.write_bytes(b"reference-image-bytes")
            profile = self.profile("text")
            profile["payload"]["baseUrl"] = f"http://127.0.0.1:{server.server_port}/v1"
            with patch("app.providers.time.sleep") as sleep:
                result = chat(profile, "local-only-key", "看图生成提示词", timeout=0.1, images=[str(image)])
            self.assertEqual(result, "本地模拟完成")
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0], requests[1])
            parts = json.loads(requests[1])["messages"][-1]["content"]
            self.assertEqual(parts[0]["text"], "看图生成提示词")
            self.assertTrue(parts[1]["image_url"]["url"].endswith(base64.b64encode(image.read_bytes()).decode()))
            sleep.assert_called_once_with(5)

    def test_failed_run_identifies_prompt_or_image_number_and_stage(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, MP_CONTENT_HOME=folder):
            store = Store(Path(folder) / "app.sqlite3")
            try:
                seed_if_empty(store)
                flow = store.get_flow("flow-cute-wallpaper")
                for node in flow["graph"]["nodes"]:
                    if node["type"] == "prompt.from_refs":
                        node["params"]["count"] = 1
                store.upsert_flow(flow)
                for kind in ("text", "image"):
                    profile = store.get_element("model-" + kind + "-default")
                    profile["payload"].update(baseUrl="http://localhost.test/v1", model="local-test", concurrency=1)
                    store.upsert_element(profile)
                    store.set_secret(profile["id"], "fake-local-key")
                engine = Engine(store)
                for kind in ("text", "image"):
                    with self.subTest(kind=kind):
                        responses = [self.completion()] if kind == "image" else []
                        with patch("app.providers.urllib.request.urlopen", side_effect=responses + [TimeoutError("read timed out")] * 3), patch("app.providers.time.sleep"):
                            run = engine.run(flow["id"])
                        self.assertEqual(run["status"], "failed")
                        self.assertIn("第 1/1 条提示词生成失败" if kind == "text" else "第 1/1 张图片生成失败", run["error"])
                        self.assertIn("生文接口读取超时" if kind == "text" else "生图接口读取超时", run["error"])
                        self.assertIn("已尝试 3 次", run["error"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
