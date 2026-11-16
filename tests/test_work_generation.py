"""Named parameters and image-only work packages, with no external API calls."""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from image_fixtures import png_bytes

from app.carry import FlowError, apply_carry, render
from app.catalog import blank_node
from app.engine import Engine
from app.ids import new_id
from app.nodes import Ctx, WORK_TITLE_ENDINGS, run_work
from app.store import Store


class WorkGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mp-work-test-")
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, MP_CONTENT_HOME=self.tmp.name)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "app.sqlite3")
        self.addCleanup(self.store.close)
        self.engine = Engine(self.store)
        self.images = [self.root / "first.png", self.root / "second.jpg"]
        for index, image in enumerate(self.images):
            image.write_bytes(b"test-image-" + bytes([index]))

    def context(self, title="作品名称", **params):
        return Ctx(
            node={"type": "work.generate", "params": params},
            inlet={"title": title, "images": {"items": [{"path": str(p)} for p in self.images]}},
            elements=[], by_type={}, flow={"name": "测试流程"}, run_id=new_id("run"),
            key_for=None, keys_for=None, scratch=self.root,
            mark_used=None, save_result=None, list_results=None, mark_results_worked=None,
        )

    def element(self, kind, name, payload):
        return self.store.upsert_element({"type": kind, "name": name, "payload": payload})

    def pipeline(self, binding_source="referenceUsed.note", *, edge_alias=False):
        references = [
            self.element("reference_image", "未抽中的图", {"path": str(self.images[0]), "note": "错误标题"}),
            self.element("reference_image", "真正抽中的参考图", {"path": str(self.images[1]), "note": "参考图说明作为作品名"}),
        ]
        profiles = [self.element("model_profile", kind, {"kind": kind, "model": "mock"}) for kind in ("text", "image")]
        for profile in profiles:
            self.store.set_secret(profile["id"], "fake-local-key")
        refs = blank_node("prompt.from_refs", new_id("node"), 0, 0)
        refs["params"].update(count=2, keepInlet=False)
        if binding_source and not edge_alias:
            refs["params"]["carryParams"] = [{"source": binding_source, "target": "${title}"}]
        refs["imports"] = [{"elementId": el["id"]} for el in references + profiles[:1]]
        image = blank_node("image.generate", new_id("node"), 300, 0)
        image["params"]["keepInlet"] = False
        image["imports"] = [{"elementId": profiles[1]["id"]}]
        middle = blank_node("rule.apply", new_id("node"), 600, 0)
        middle["params"]["keepInlet"] = False
        work = blank_node("work.generate", new_id("node"), 900, 0)
        work["params"]["nameTemplate"] = "${title}"
        nodes = [refs, image, middle, work]
        edges = []
        for source, target, field in zip(nodes, nodes[1:], ("prompts", "images", "images")):
            edges.append({"id": new_id("edge"), "from": source["id"], "to": target["id"],
                          "carry": [{"source": field, "target": field, "op": "copy"}], "rules": []})
        if edge_alias:
            edges[0]["carry"].append({"source": binding_source, "target": "${title}", "op": "copy"})
        flow = self.store.upsert_flow({"name": "参考图到作品", "graph": {"nodes": nodes, "edges": edges}})
        return flow, nodes, references

    def run_mocked(self, flow, chosen):
        self.generated_blob = png_bytes()
        with patch("app.nodes.random.choice", return_value=chosen), patch("app.nodes.chat", return_value="模拟图片 9:16"), patch(
            "app.nodes.generate_image", return_value=(self.generated_blob, "1080x1920")
        ) as image_call:
            result = self.engine.run(flow["id"])
        return result, image_call.call_count

    def test_reference_name_or_note_survives_all_nodes_and_names_zip(self):
        for source, expected in [("referenceUsed.name", "真正抽中的参考图"), ("referenceUsed.note", "参考图说明作为作品名")]:
            with self.subTest(source=source):
                flow, nodes, references = self.pipeline(source)
                run, count = self.run_mocked(flow, references[1])
                self.assertEqual(run["status"], "succeeded", run["error"])
                self.assertEqual(count, 2)
                traces = run["trace"]["nodes"]
                self.assertEqual(traces[nodes[0]["id"]]["export"]["referenceUsed"]["note"], references[1]["payload"]["note"])
                for node in nodes[1:]:
                    self.assertEqual(traces[node["id"]]["inlet"]["title"], expected)
                self.assertNotIn("referenceUsed", traces[nodes[1]["id"]]["export"])
                self.assertNotIn("prompts", traces[nodes[1]["id"]]["export"])
                output = traces[nodes[-1]["id"]]["export"]["output"]
                self.assertEqual(run["trace"]["output"]["collection"], output["collection"])
                self.assertIn(output["collection"], self.store.output_dirs())
                self.assertEqual(Path(output["folder"]).name, expected)
                self.assertEqual(Path(output["zip"]).name, expected + ".zip")
                with zipfile.ZipFile(output["zip"]) as archive:
                    self.assertEqual(archive.namelist(), [f"{expected}/01.png", f"{expected}/02.png"])
                    for name in archive.namelist():
                        self.assertEqual(archive.read(name), self.generated_blob)

    def test_existing_edge_alias_also_becomes_a_carried_parameter(self):
        flow, nodes, references = self.pipeline(edge_alias=True)
        run, _ = self.run_mocked(flow, references[1])
        self.assertEqual(run["status"], "succeeded", run["error"])
        self.assertEqual(run["trace"]["nodes"][nodes[-1]["id"]]["inlet"]["title"], references[1]["payload"]["note"])

    def test_fixed_text_survives_all_nodes_and_names_repeated_work_packages(self):
        flow, nodes, references = self.pipeline(binding_source=None)
        flow["graph"]["nodes"][0]["params"]["carryParams"] = [
            {"sourceType": "literal", "source": "missing.field", "value": "暗黑系壁纸分享", "target": "${title}"},
            {"source": "title", "target": "作品名"},
        ]
        self.store.upsert_flow(flow)
        for suffix in ("", "-2"):
            run, count = self.run_mocked(flow, references[1])
            self.assertEqual(run["status"], "succeeded", run["error"])
            self.assertEqual(count, 2)
            traces = run["trace"]["nodes"]
            for node in nodes[1:]:
                self.assertEqual(traces[node["id"]]["inlet"]["title"], "暗黑系壁纸分享")
                self.assertEqual(traces[node["id"]]["inlet"]["作品名"], "暗黑系壁纸分享")
            output = traces[nodes[-1]["id"]]["export"]["output"]
            name = "暗黑系壁纸分享" + suffix
            self.assertEqual(Path(output["folder"]).name, name)
            self.assertEqual(Path(output["zip"]).name, name + ".zip")
            with zipfile.ZipFile(output["zip"]) as archive:
                self.assertEqual(archive.namelist(), [f"{name}/01.png", f"{name}/02.png"])

    def test_fixed_text_does_not_resolve_fields_or_nested_templates(self):
        flow, nodes, references = self.pipeline(binding_source=None)
        literal = '固定${theme.text} {{referenceUsed.name}} 与 "说明"'
        flow["graph"]["nodes"][0]["params"]["carryParams"] = [
            {"sourceType": "literal", "value": literal, "target": "title"},
        ]
        self.store.upsert_flow(flow)
        run, _ = self.run_mocked(flow, references[1])
        self.assertEqual(run["status"], "succeeded", run["error"])
        traces = run["trace"]["nodes"]
        self.assertEqual(traces[nodes[-1]["id"]]["inlet"]["title"], literal)
        self.assertEqual(Path(traces[nodes[-1]["id"]]["export"]["output"]["zip"]).stem,
                         '固定${theme.text} {{referenceUsed.name}} 与 说明')

    def test_fixed_text_requires_nonempty_text_and_valid_parameter_name(self):
        for literal, target, expected in [
            ("", "title", "固定文案不能为空"), ("  ", "title", "固定文案不能为空"),
            (None, "title", "固定文案不能为空"), (123, "title", "固定文案不能为空"),
            ("有效文案", "bad.name", "有效参数名"), ("有效文案", "", "有效参数名"),
        ]:
            with self.subTest(literal=literal, target=target):
                flow, _, references = self.pipeline(binding_source=None)
                flow["graph"]["nodes"][0]["params"]["carryParams"] = [
                    {"sourceType": "literal", "value": literal, "target": target},
                ]
                self.store.upsert_flow(flow)
                run, count = self.run_mocked(flow, references[1])
                self.assertEqual(run["status"], "failed")
                self.assertIn(expected, run["error"])
                self.assertEqual(count, 0)

    def test_edge_template_parameter_keeps_its_rendered_value(self):
        flow, nodes, references = self.pipeline(binding_source=None)
        flow["graph"]["edges"][0]["carry"].append({"op": "template", "target": "${title}", "template": "作品-${referenceUsed.name}"})
        flow = self.store.upsert_flow(flow)
        run, _ = self.run_mocked(flow, references[1])
        self.assertEqual(run["status"], "succeeded", run["error"])
        self.assertEqual(run["trace"]["nodes"][nodes[-1]["id"]]["inlet"]["title"], "作品-真正抽中的参考图")

    def test_disconnected_branch_does_not_supply_title(self):
        flow, _, references = self.pipeline(binding_source=None)
        unrelated = blank_node("start.theme", new_id("node"), -300, 200)
        unrelated["params"].update(themeText="不能泄漏", carryParams=[{"source": "theme.text", "target": "title"}])
        flow["graph"]["nodes"].append(unrelated)
        flow = self.store.upsert_flow(flow)
        run, _ = self.run_mocked(flow, references[1])
        self.assertEqual(run["status"], "failed")
        self.assertIn("参数 title 未携带", run["error"])
        self.assertFalse((self.root / "outputs" / "works").exists())

    def test_edge_rules_can_drop_parameters(self):
        flow, _, references = self.pipeline()
        rule = self.element("rule", "移除标题", {"steps": [{"op": "drop", "field": "title"}]})
        flow["graph"]["edges"][1]["rules"] = [rule["id"]]
        flow = self.store.upsert_flow(flow)
        run, _ = self.run_mocked(flow, references[1])
        self.assertEqual(run["status"], "failed")
        self.assertIn("参数 title 未携带", run["error"])

    def test_missing_binding_source_is_actionable(self):
        flow, _, references = self.pipeline("referenceUsed.missing")
        run, _ = self.run_mocked(flow, references[1])
        self.assertEqual(run["status"], "failed")
        self.assertIn("title 的来源字段 referenceUsed.missing 不存在", run["error"])

    def test_image_only_zip_preserves_bytes_extensions_and_same_named_root(self):
        output = run_work(self.context()).export["output"]
        self.assertEqual(output["files"], ["01.png", "02.jpg"])
        with zipfile.ZipFile(output["zip"]) as archive:
            self.assertEqual(archive.namelist(), ["作品名称/01.png", "作品名称/02.jpg"])
            for name, source in zip(archive.namelist(), self.images):
                self.assertEqual(archive.read(name), source.read_bytes())
                self.assertEqual((Path(output["folder"]) / Path(name).name).read_bytes(), source.read_bytes())

    def test_duplicates_skip_existing_zip_or_folder_and_never_overwrite(self):
        ctx = self.context()
        first = run_work(ctx).export["output"]
        original = Path(first["zip"]).read_bytes()
        root = Path(first["folder"]).parent
        (root / "作品名称-2.zip").write_bytes(b"existing zip")
        (root / "作品名称-3").mkdir()
        second = run_work(ctx).export["output"]
        self.assertEqual(second["title"], "作品名称-4")
        self.assertEqual(Path(second["zip"]).stem, Path(second["folder"]).name)
        self.assertEqual(Path(first["zip"]).read_bytes(), original)
        self.assertEqual((root / "作品名称-2.zip").read_bytes(), b"existing zip")

    def test_both_template_syntaxes_and_unicode_parameter(self):
        ctx = self.context(nameTemplate="${作品名}-{{title}}")
        ctx.inlet["作品名"] = "中文参数"
        self.assertEqual(run_work(ctx).export["output"]["title"], "中文参数-作品名称")
        self.assertEqual(render("${title}/{{title}}", {"title": "保留"}), "保留/保留")
        self.assertEqual(apply_carry({"title": "保留"}, [{"source": "${title}", "target": "${新标题}"}], []), {"新标题": "保留"})

    def test_empty_title_missing_images_or_missing_file_create_no_outputs(self):
        for problem in ("empty-title", "missing-title", "empty-images", "missing-file", "missing-path"):
            with self.subTest(problem=problem):
                ctx = self.context(directory="outputs/" + problem)
                if problem == "empty-title":
                    ctx.inlet["title"] = "  "
                elif problem == "missing-title":
                    ctx.inlet.pop("title")
                elif problem == "empty-images":
                    ctx.inlet["images"] = {"items": []}
                elif problem == "missing-file":
                    ctx.inlet["images"]["items"][1]["path"] = str(self.root / "missing.png")
                else:
                    ctx.inlet["images"]["items"][1] = {}
                with self.assertRaises(FlowError):
                    run_work(ctx)
                self.assertFalse((self.root / "outputs" / problem).exists())

    def test_windows_names_are_sanitized_and_cannot_escape_output_root(self):
        for title, expected in [("../目录\\测试:*?", "目录 测试"), ("CON.txt", "作品-CON.txt"), ("  标题. ", "标题")]:
            with self.subTest(title=title):
                output = run_work(self.context(title)).export["output"]
                self.assertEqual(output["title"], expected)
                self.assertEqual(Path(output["folder"]).resolve().parent, self.root / "outputs" / "works" / output["date"])
        with self.assertRaises(FlowError):
            run_work(self.context("..."))

    def test_copy_or_zip_failure_removes_partial_outputs(self):
        for operation in ("app.nodes.shutil.copy2", "app.nodes.zipfile.ZipFile.write"):
            with self.subTest(operation=operation):
                with patch(operation, side_effect=OSError("模拟写入失败")):
                    with self.assertRaisesRegex(OSError, "模拟写入失败"):
                        run_work(self.context())
                self.assertFalse(any(p.is_file() for p in (self.root / "outputs" / "works").rglob("*")))

    def test_categories_share_one_date_folder_and_preserve_work_zip_layout(self):
        with patch("app.nodes.datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 16, 22, 30)
            first = run_work(self.context(subdirectory="壁纸/风景")).export["output"]
            second = run_work(self.context(subdirectory="头像\\萌系")).export["output"]
            flat = run_work(self.context("未分类作品")).export["output"]
        collection = self.root / "outputs" / "works" / "2026-11-16"
        self.assertEqual(first["collection"], str(collection))
        self.assertEqual(second["collection"], first["collection"])
        self.assertEqual(Path(first["folder"]).parent, collection / "壁纸" / "风景")
        self.assertEqual(Path(second["folder"]).parent, collection / "头像" / "萌系")
        self.assertEqual(Path(flat["folder"]).parent, collection)
        with zipfile.ZipFile(second["zip"]) as archive:
            self.assertEqual(archive.namelist(), ["作品名称/01.png", "作品名称/02.jpg"])

    def test_next_day_uses_new_collection_and_keeps_compact_name_template_date(self):
        outputs = []
        with patch("app.nodes.datetime") as clock:
            for day in (16, 17):
                clock.now.return_value = datetime(2026, 11, day, 0, 1)
                outputs.append(run_work(self.context()).export["output"])
            dated = run_work(self.context(nameTemplate="${title}-${date}")).export["output"]
        self.assertNotEqual(outputs[0]["collection"], outputs[1]["collection"])
        self.assertEqual([o["title"] for o in outputs], ["作品名称", "作品名称"])
        self.assertEqual(dated["title"], "作品名称-20261117")
        self.assertTrue(Path(outputs[0]["zip"]).is_file())

    def test_invalid_category_cannot_escape_date_folder_or_create_outputs(self):
        for category in ("../outside", "壁纸/../../outside", "C:\\outside", "/outside", "壁纸/非法*分类"):
            with self.subTest(category=category), self.assertRaises(FlowError):
                run_work(self.context(subdirectory=category))
        self.assertFalse((self.root / "outputs").exists())

    def test_ins_template_uses_varied_safe_endings_in_folder_and_zip(self):
        templates = blank_node("work.generate", new_id("node"), 0, 0)["params"]
        self.assertEqual(templates["nameTemplate"], "ins${title}${ending}")
        endings = set()
        for index in range(24):
            ctx = self.context("简约头像 有自己的小情绪", **templates)
            ctx.run_id = f"run-ending-{index}"
            output = run_work(ctx).export["output"]
            self.assertTrue(output["title"].startswith("ins简约头像 有自己的小情绪"))
            ending = next((ending for ending in WORK_TITLE_ENDINGS if ending in output["title"]), None)
            self.assertIsNotNone(ending)
            endings.add(ending)
            self.assertEqual(Path(output["zip"]).stem, Path(output["folder"]).name)
            with zipfile.ZipFile(output["zip"]) as archive:
                self.assertEqual(archive.namelist(), [output["title"] + "/01.png", output["title"] + "/02.jpg"])
                self.assertEqual(archive.read(archive.namelist()[0]), self.images[0].read_bytes())
        self.assertEqual(endings, set(WORK_TITLE_ENDINGS))

    def test_custom_ins_caption_and_ending_do_not_change_for_same_run(self):
        params = {"nameTemplate": "ins简约头像 有自己的小情绪${ending}"}
        first = self.context(**params)
        second = self.context(directory="outputs/another", **params)
        second.run_id = first.run_id
        self.assertEqual(run_work(first).export["output"]["title"], run_work(second).export["output"]["title"])

    def test_reference_article_only_has_no_implicit_image_parameter(self):
        article = self.element("reference_article", "文章", {"body": "参考正文"})
        model = self.element("model_profile", "文本模型", {"kind": "text", "model": "mock"})
        self.store.set_secret(model["id"], "fake-key")
        node = blank_node("prompt.from_refs", new_id("node"), 0, 0)
        node["imports"] = [{"elementId": item["id"]} for item in (article, model)]
        flow = self.store.upsert_flow({"name": "只用参考文章", "graph": {"nodes": [node], "edges": []}})
        with patch("app.nodes.chat", return_value="文章转提示词"):
            run = self.engine.run(flow["id"])
        self.assertEqual(run["status"], "succeeded", run["error"])


if __name__ == "__main__":
    unittest.main()
