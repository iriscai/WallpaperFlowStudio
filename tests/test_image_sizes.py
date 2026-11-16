"""Enforced canvas requests and actual file dimensions, without paid model calls."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.catalog import blank_node
from app.engine import Engine
from app.ids import new_id
from app.image_sizes import IMAGE_SIZE_PRESETS, image_size_config, normalize_canvas
from app.nodes import Ctx, run_image
from app.store import Store
from image_fixtures import png_bytes


class ImageSizeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="mp-canvas-")
        self.addCleanup(folder.cleanup)
        self.environment = patch.dict(os.environ, MP_CONTENT_HOME=folder.name)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.root = Path(folder.name)

    def context(self, params=None, prompts=None, variants=None, flow_name="尺寸验证", api=True):
        profile = {"id": "local-profile", "payload": {"kind": "image", "model": "mock", "concurrency": 3}}
        types = {"model_profile": [profile]} if api else {}
        if variants is not None:
            types["image_config_group"] = [{"id": "local-group", "payload": {"variants": variants}}]
        return Ctx(node={"type": "image.generate", "params": params or {}},
                   inlet={"prompts": prompts} if prompts else {}, elements=[], by_type=types,
                   flow={"name": flow_name}, run_id=new_id("run"),
                   key_for=lambda _: "local-key", keys_for=lambda _: ["local-key"],
                   scratch=self.root, mark_used=None, save_result=None, list_results=None,
                   mark_results_worked=None)

    def test_conflicting_prompts_all_use_selected_request_and_saved_dimensions(self):
        source = png_bytes(640, 360)
        ctx = self.context({"sizePreset": "avatar"}, ["横版16:9", "沿用参考图9:16", "画布2048x4096"])
        with patch("app.nodes.generate_image", return_value=(source, "1024x1024")) as model:
            result = run_image(ctx)
        self.assertEqual(model.call_count, 3)
        for call in model.call_args_list:
            self.assertEqual(call.args[3:5], (1024, 1024))
            self.assertIn("画布硬性要求：1024x1024", call.args[2])
        self.assertEqual(result.export["imageSize"]["ratio"], "1:1")
        self.assertEqual(result.export["images"]["size"]["preset"], "avatar")
        for item in result.export["images"]["items"]:
            self.assertEqual(item["originalSize"], [640, 360])
            self.assertTrue(item["sizeAdjusted"])
            with Image.open(item["path"]) as image:
                self.assertEqual(image.size, (1024, 1024))
                self.assertEqual(image.format, "PNG")
        self.assertIn("3 张接口返回尺寸不符", result.message)

    def test_legacy_avatar_and_wallpaper_flows_receive_correct_defaults(self):
        for name, expected in (("女生头像-系列2", (1024, 1024)), ("女生壁纸-系列2-混搭", (1080, 1920)), ("未命名流程", (1080, 1920))):
            with self.subTest(name=name):
                ctx = self.context(prompts=["沿用参考图1:1"], flow_name=name)
                with patch("app.nodes.generate_image", return_value=(png_bytes(*expected), f"{expected[0]}x{expected[1]}")) as model:
                    result = run_image(ctx)
                self.assertEqual(model.call_args.args[3:5], expected)
                self.assertFalse(result.export["images"]["items"][0]["sizeAdjusted"])

    def test_config_group_keeps_counts_styles_but_all_variants_use_one_canvas(self):
        ctx = self.context({"sizePreset": "portrait-2-3"}, variants=[
            {"name": "方图", "width": 512, "height": 512, "count": 1, "style": "水彩"},
            {"name": "横图", "width": 1920, "height": 1080, "count": 2, "style": "油画"},
        ])
        with patch("app.nodes.generate_image", return_value=(png_bytes(1024, 1536), "1024x1536")) as model:
            result = run_image(ctx)
        self.assertEqual(model.call_count, 3)
        self.assertTrue(all(call.args[3:5] == (1024, 1536) for call in model.call_args_list))
        prompts = [item["prompt"] for item in result.export["images"]["items"]]
        self.assertEqual(sum("水彩" in prompt for prompt in prompts), 1)
        self.assertEqual(sum("油画" in prompt for prompt in prompts), 2)

    def test_custom_and_every_preset_provide_valid_reduced_ratios(self):
        for preset in IMAGE_SIZE_PRESETS:
            config = image_size_config({"params": {"sizePreset": preset["id"]}}, {})
            self.assertEqual((config["width"], config["height"]), (preset["width"], preset["height"]))
        config = image_size_config({"params": {"sizePreset": "custom", "imageWidth": 1170, "imageHeight": 2532}}, {})
        self.assertEqual((config["width"], config["height"]), (1170, 2532))
        self.assertEqual(config["ratio"], "195:422")

    def test_invalid_custom_sizes_and_unknown_presets_fail_before_any_api_calls(self):
        for value in (None, "", 0, -1, True, 1.5, 255, 4097, "bad"):
            for field in ("imageWidth", "imageHeight"):
                with self.subTest(value=value, field=field):
                    params = {"sizePreset": "custom", "imageWidth": 1024, "imageHeight": 1024, field: value}
                    with patch("app.nodes.generate_image") as model:
                        with self.assertRaisesRegex(Exception, "256 到 4096"):
                            run_image(self.context(params, ["提示词"]))
                    model.assert_not_called()
        with patch("app.nodes.generate_image") as model:
            with self.assertRaisesRegex(Exception, "不认识的生图尺寸预设"):
                run_image(self.context({"sizePreset": "unknown"}, ["提示词"]))
            model.assert_not_called()

    def test_normalization_preserves_complete_subject_and_png_bytes_when_already_correct(self):
        # Both end markers must survive when a wide image is fitted into a square.
        original = Image.new("RGB", (400, 200), "white")
        original.paste("red", (0, 0, 50, 200))
        original.paste("blue", (350, 0, 400, 200))
        encoded = io.BytesIO()
        original.save(encoded, format="PNG")
        blob, _ = normalize_canvas(encoded.getvalue(), 512, 512)
        with Image.open(io.BytesIO(blob)) as image:
            self.assertEqual(image.getpixel((20, 256))[:3], (255, 0, 0))
            self.assertEqual(image.getpixel((490, 256))[:3], (0, 0, 255))
        exact = png_bytes(512, 512)
        kept, info = normalize_canvas(exact, 512, 512)
        self.assertEqual(kept, exact)
        self.assertFalse(info["sizeAdjusted"])

    def test_jpeg_orientation_is_applied_before_resize_and_output_is_png(self):
        image = Image.new("RGB", (300, 600), "orange")
        exif = Image.Exif()
        exif[274] = 6
        output = io.BytesIO()
        image.save(output, format="JPEG", exif=exif)
        normalized, metadata = normalize_canvas(output.getvalue(), 600, 300)
        self.assertEqual(metadata["originalSize"], [600, 300])
        with Image.open(io.BytesIO(normalized)) as saved:
            self.assertEqual(saved.size, (600, 300))
            self.assertEqual(saved.format, "PNG")

    def test_unreadable_response_is_reported_with_image_number(self):
        with patch("app.nodes.generate_image", return_value=(b"invalid-image", "1024x1024")):
            with self.assertRaisesRegex(Exception, "第 1/1 张图片生成失败.*无法读取"):
                run_image(self.context({"sizePreset": "avatar"}, ["提示词"]))

    def test_local_drafts_also_save_requested_pixels(self):
        ctx = self.context({"sizePreset": "custom", "imageWidth": 640, "imageHeight": 960},
                           variants=[{"count": 1, "width": 512, "height": 512}], api=False)
        with patch("app.nodes.generate_image") as model:
            result = run_image(ctx)
        model.assert_not_called()
        with Image.open(result.export["images"]["items"][0]["path"]) as image:
            self.assertEqual(image.size, (640, 960))

    def test_normalized_dimensions_survive_library_and_final_zip(self):
        store = Store(self.root / "app.sqlite3")
        self.addCleanup(store.close)
        profile = store.upsert_element({"type": "model_profile", "name": "模拟接口", "payload": {"kind": "image", "model": "mock"}})
        store.set_secret(profile["id"], "fake-local-key")
        reference_path = self.root / "reference.png"
        reference_path.write_bytes(png_bytes(64, 64))
        reference = store.upsert_element({"type": "reference_image", "name": "参考图", "payload": {"path": str(reference_path)}})
        text_profile = store.upsert_element({"type": "model_profile", "name": "模拟生文", "payload": {"kind": "text", "model": "mock", "concurrency": 1}})
        store.set_secret(text_profile["id"], "fake-local-key")
        start = blank_node("prompt.from_refs", new_id("node"), 0, 0)
        start["params"]["count"] = 2
        start["imports"] = [{"elementId": item["id"]} for item in (reference, text_profile)]
        start["params"]["carryParams"] = [{"sourceType": "literal", "value": "统一尺寸作品", "target": "title"}]
        image = blank_node("image.generate", new_id("node"), 300, 0)
        image["params"].update(sizePreset="custom", imageWidth=640, imageHeight=960, keepInlet=False)
        image["imports"] = [{"elementId": profile["id"]}]
        library = blank_node("file.output", new_id("node"), 600, 0)
        library["params"].update(saveAs="library", keepInlet=True)
        work = blank_node("work.generate", new_id("node"), 900, 0)
        nodes = [start, image, library, work]
        edges = [{"id": new_id("edge"), "from": a["id"], "to": b["id"], "carry": [{"source": field, "target": field, "op": "copy"}]}
                 for a, b, field in zip(nodes, nodes[1:], ("prompts", "images", "images"))]
        flow = store.upsert_flow({"name": "成品尺寸验证", "graph": {"nodes": nodes, "edges": edges}})
        with patch("app.nodes.chat", side_effect=["方形1:1", "横版16:9"]), patch("app.nodes.generate_image", side_effect=[(png_bytes(512, 512), "640x960"), (png_bytes(800, 450), "640x960")]):
            run = Engine(store).run(flow["id"])
        self.assertEqual(run["status"], "succeeded", run["error"])
        records = store.list_results(limit=0)
        self.assertEqual(len(records), 2)
        for record in records:
            with Image.open(self.root / record["path"]) as saved:
                self.assertEqual(saved.size, (640, 960))
        with zipfile.ZipFile(run["trace"]["output"]["zip"]) as archive:
            self.assertEqual(len(archive.namelist()), 2)
            for name in archive.namelist():
                with Image.open(io.BytesIO(archive.read(name))) as saved:
                    self.assertEqual(saved.size, (640, 960))


if __name__ == "__main__":
    unittest.main()
