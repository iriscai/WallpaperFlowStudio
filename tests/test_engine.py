"""Isolated checks for carry, schedule, the wallpaper flow, and packages."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

from image_fixtures import png_bytes

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.carry import FlowError, apply_carry, apply_rule, get_path  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.ids import is_uuid_id
from app.image_sizes import image_size_config
import app.nodes as nodes  # noqa: E402
from app.packages import build_flow_package, import_package  # noqa: E402
from app.paths import resolve_path  # noqa: E402
from app.providers import extract_image_payload, image_request_body, text_message_content  # noqa: E402
from app.schedule import compute_next, mark_after_run, normalize_schedule  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.store import Store  # noqa: E402


class StyleImageRequestTests(unittest.TestCase):
    def test_text_request_embeds_reference_image(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        image = Path(folder.name) / "ref.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        content = text_message_content("写一条提示词", [str(image)])
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_old_nodes_receive_avatar_or_wallpaper_defaults(self):
        self.assertEqual(image_size_config({}, {"name": "女生头像-系列2"})["preset"], "avatar")
        self.assertEqual(image_size_config({}, {"name": "女生壁纸-系列2-混搭"})["preset"], "wallpaper")

    def test_body_is_model_prompt_and_configured_size(self):
        body = image_request_body("gpt-image-2", "可爱壁纸", 1080, 1920)
        self.assertEqual(set(body), {"model", "prompt", "size"})
        self.assertEqual(body["size"], "1080x1920")
        self.assertNotIn("image", body)
        self.assertEqual(extract_image_payload({"data": [{"b64_json": "abc"}]}), ("b64_json", "abc"))
        self.assertEqual(extract_image_payload({"data": [{"url": "http://example/a.png"}]}), ("url", "http://example/a.png"))


class ScheduleTests(unittest.TestCase):
    def test_weekday_numbers(self):
        # 2026-11-06 is a Friday. Local wall time is what the schedule uses.
        now = datetime(2026, 11, 6, 10, 0).astimezone()
        self.assertEqual((now.date().weekday() + 1) % 7, 5)

        daily = {"enabled": True, "frequency": "daily", "time": "09:00", "weekdays": [], "date": ""}
        self.assertEqual(compute_next(daily, now).date().isoformat(), "2026-11-07")

        weekdays = {"enabled": True, "frequency": "weekdays", "time": "09:00", "weekdays": [], "date": ""}
        self.assertEqual(compute_next(weekdays, now).date().isoformat(), "2026-11-09")

        weekly = {"enabled": True, "frequency": "weekly", "time": "09:00", "weekdays": [0], "date": ""}
        sunday = compute_next(weekly, now)
        self.assertEqual(sunday.date().isoformat(), "2026-11-08")
        self.assertEqual((sunday.date().weekday() + 1) % 7, 0)

        later_today = compute_next(
            {"frequency": "once", "time": "11:00", "date": "2026-11-06", "weekdays": []},
            now,
        )
        self.assertEqual(later_today.hour, 11)
        self.assertIsNone(
            compute_next({"frequency": "once", "time": "09:00", "date": "2026-11-06", "weekdays": []}, now)
        )

    def test_once_advances_to_disabled(self):
        now = datetime(2026, 11, 6, 11, 30).astimezone()
        schedule = normalize_schedule(
            {"enabled": True, "frequency": "once", "time": "18:00", "date": "2026-11-06", "weekdays": []},
            datetime(2026, 11, 6, 9, 0).astimezone(),
        )
        self.assertTrue(schedule["nextRunAt"])
        after = mark_after_run(schedule, now)
        self.assertFalse(after["enabled"])
        self.assertIsNone(after["nextRunAt"])

    def test_weekly_requires_a_day_when_enabled(self):
        with self.assertRaises(FlowError):
            normalize_schedule({"enabled": True, "frequency": "weekly", "time": "09:00", "weekdays": []})


class CarryTests(unittest.TestCase):
    def test_copy_skips_missing_and_rule_sets_title(self):
        upstream = {"theme": {"text": "海边"}, "titles": {"items": [{"text": "海边的灯"}]}}
        fragment = apply_carry(
            upstream,
            [
                {"source": "theme", "target": "theme", "op": "copy"},
                {"source": "titles", "target": "titles", "op": "copy"},
                {"source": "missing", "target": "gone", "op": "copy"},
            ],
            [{"steps": [{"op": "set", "field": "primaryTitle", "from": "titles.items.0.text"}]}],
        )
        self.assertEqual(fragment["primaryTitle"], "海边的灯")
        self.assertNotIn("gone", fragment)
        self.assertEqual(get_path(fragment, "theme.text"), "海边")

    def test_require_blocks(self):
        with self.assertRaises(FlowError):
            apply_carry({}, [], [{"steps": [{"op": "require", "field": "titles.items", "message": "没有标题"}]}])

    def test_relative_path_cannot_escape(self):
        with self.assertRaises(ValueError):
            resolve_path("outputs/../../outside")


class WallpaperFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["MP_CONTENT_HOME"] = self.tmp.name
        self.store = Store(Path(self.tmp.name) / "app.sqlite3")
        seed_if_empty(self.store)
        self.engine = Engine(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        os.environ.pop("MP_CONTENT_HOME", None)

    def test_wallpaper_flow_writes_article_image_and_zip(self):
        run = self.engine.run("flow-wallpaper-article", mode="once")
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        nodes = run["trace"]["nodes"]
        self.assertEqual(
            [item["type"] for item in (self.store.get_flow("flow-wallpaper-article")["graph"]["nodes"])],
            ["start.theme", "prompt.compose", "text.title", "text.article", "image.generate", "file.output"],
        )
        title_export = nodes["n-title"]["export"]
        first_title = title_export["titles"]["items"][0]["text"]
        self.assertIn("秋日窗边治愈壁纸", first_title)
        self.assertIn("把黄昏折进锁屏", title_export["titles"]["promptUsed"])
        self.assertEqual(nodes["n-title"]["provider"], "local-draft")

        article = nodes["n-article"]["export"]["articles"]["items"][0]
        self.assertEqual(article["title"], first_title)
        self.assertIn("本地草稿", article["body"])
        self.assertIn("不编造下载链接", nodes["n-article"]["export"]["articles"]["promptUsed"])
        self.assertEqual(nodes["n-article"]["inlet"]["primaryTitle"], first_title)
        self.assertNotIn("prompt", nodes["n-article"]["inlet"])

        image = nodes["n-image"]["export"]["images"]["items"][0]
        self.assertTrue(image["path"].endswith(".png"))
        self.assertTrue(Path(image["path"]).read_bytes().startswith(b"\x89PNG"))
        self.assertIn("柔光胶片", image["prompt"])
        self.assertNotIn("窗边柔光", image["prompt"])
        self.assertNotIn("referenceImages", image)

        folder = Path(run["trace"]["output"]["folder"])
        archive = Path(run["trace"]["output"]["zip"])
        self.assertTrue((folder / "manifest.json").is_file())
        self.assertTrue(any(folder.glob("*.md")))
        self.assertTrue(any((folder / "images").glob("*.png")))
        self.assertFalse((folder / "references").exists())
        with zipfile.ZipFile(archive) as zipped:
            names = zipped.namelist()
        self.assertTrue(any(name.endswith(".md") for name in names))
        self.assertTrue(any(name.startswith("images/") for name in names))
        self.assertFalse(any(name.startswith("references/") for name in names))

    def test_theme_override_and_missing_carry(self):
        run = self.engine.run("flow-wallpaper-article", mode="once", overrides={"themeText": "海边夜灯"})
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        self.assertIn("海边夜灯", run["trace"]["nodes"]["n-title"]["export"]["titles"]["items"][0]["text"])

        flow = self.store.get_flow("flow-wallpaper-article")
        for edge in flow["graph"]["edges"]:
            if edge["id"] == "e-title-article":
                edge["carry"] = [item for item in edge["carry"] if item["source"] != "titles"]
        self.store.upsert_flow(flow)
        blocked = self.engine.run("flow-wallpaper-article", mode="once")
        self.assertEqual(blocked["status"], "failed")
        self.assertIn("没有标题", blocked["error"])

    def test_cycle_is_rejected(self):
        self.store.upsert_flow(
            {
                "id": "flow-cycle",
                "name": "循环",
                "description": "",
                "graph": {
                    "nodes": [
                        {"id": "a", "type": "rule.apply", "name": "甲", "x": 0, "y": 0, "imports": [], "params": {}},
                        {"id": "b", "type": "rule.apply", "name": "乙", "x": 10, "y": 0, "imports": [], "params": {}},
                    ],
                    "edges": [
                        {"id": "ab", "from": "a", "to": "b", "carry": [], "rules": []},
                        {"id": "ba", "from": "b", "to": "a", "carry": [], "rules": []},
                    ],
                },
                "schedule": {"enabled": False, "frequency": "daily", "time": "09:00", "weekdays": [], "date": ""},
            }
        )
        run = self.engine.run("flow-cycle")
        self.assertEqual(run["status"], "failed")
        self.assertIn("循环", run["error"])

    def test_reference_and_prompts_are_table_rows(self):
        prompt = self.store.conn.execute(
            "SELECT template FROM prompts WHERE id = ?",
            ("prompt-article-wallpaper",),
        ).fetchone()
        self.assertIn("不编造下载链接", prompt["template"])
        article = self.store.conn.execute(
            "SELECT title, body FROM reference_articles WHERE id = ?",
            ("ref-article-dusk",),
        ).fetchone()
        self.assertEqual(article["title"], "把黄昏折进锁屏")
        self.assertIn("窗帘", article["body"])
        image = self.store.conn.execute(
            "SELECT path, role FROM reference_images WHERE id = ?",
            ("ref-image-soft",),
        ).fetchone()
        self.assertEqual(image["role"], "色彩")
        copy = self.store.conn.execute(
            "SELECT copy_text FROM reference_copies WHERE id = ?",
            ("ref-copy-wind",),
        ).fetchone()
        self.assertIn("锁屏", copy["copy_text"])
        step = self.store.conn.execute(
            "SELECT field, source_path FROM rule_steps WHERE rule_id = ?",
            ("rule-primary-title",),
        ).fetchone()
        self.assertEqual(step["field"], "primaryTitle")
        variant = self.store.conn.execute(
            "SELECT width, height, style FROM image_variants WHERE group_id = ?",
            ("image-group-portrait",),
        ).fetchone()
        self.assertEqual((variant["width"], variant["height"]), (1080, 1920))
        self.assertIn("柔光", variant["style"])
        self.store.upsert_element(
            {
                "id": "prompt-meta",
                "type": "prompt",
                "name": "带分组的提示词",
                "group": "测试分组",
                "tags": ["夜读", "标题"],
                "fields": [{"key": "账号", "value": "壁纸号"}],
                "payload": {"purpose": "title", "template": "测试", "notes": ""},
            }
        )
        stored = self.store.get_element("prompt-meta")
        self.assertEqual(stored["group"], "测试分组")
        self.assertEqual(stored["tags"], ["夜读", "标题"])
        self.assertEqual(stored["fields"], [{"key": "账号", "value": "壁纸号"}])

    def _use_fake_text_model(self, lines):
        element = self.store.get_element("model-text-default")
        element["payload"]["model"] = "gpt-test"
        self.store.upsert_element(element)
        self.store.set_secret("model-text-default", "sk-test")
        calls = []

        def fake(profile, key, user_text, timeout=120, system="你是微信公众号编辑。只输出要求的成品，不要解释工作过程。", images=None):
            calls.append({"key": key, "text": user_text, "system": system, "model": profile["payload"]["model"], "images": images or []})
            for index, line in enumerate(lines, 1):
                if f"第 {index} 条" in user_text:
                    return line
            return lines[0]

        original = nodes.chat
        nodes.chat = fake
        self.addCleanup(setattr, nodes, "chat", original)
        return calls

    def test_cute_wallpaper_requires_text_model(self):
        run = self.engine.run("flow-cute-wallpaper", mode="once")
        self.assertEqual(run["status"], "failed")
        self.assertIn("生文模型", run["error"])

    def test_cute_wallpaper_makes_five_images_and_a_zip(self):
        lines = [f"可爱壁纸提示词{index}" for index in range(1, 6)]
        calls = self._use_fake_text_model(lines)
        image_model = self.store.get_element("model-image-default")
        image_model["payload"]["model"] = "gpt-image-test"
        image_model["payload"]["concurrency"] = 2
        self.store.upsert_element(image_model)
        self.store.set_secrets("model-image-default", ["sk-image-aaaa", "sk-image-bbbb"])
        sent = []

        def fake_image(profile, key, prompt, width, height, timeout=180):
            sent.append((prompt, key))
            return png_bytes(width, height), f"{width}x{height}"

        original = nodes.generate_image
        nodes.generate_image = fake_image
        self.addCleanup(setattr, nodes, "generate_image", original)
        run = self.engine.run("flow-cute-wallpaper", mode="once")
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(item["system"] is None for item in calls))
        self.assertTrue(all(item["images"] and item["images"][0].endswith("cute-wallpaper-ref.png") for item in calls))
        traced = run["trace"]["nodes"]
        self.assertEqual(traced["n-prompts"]["export"]["prompts"], lines)
        self.assertEqual(traced["n-image"]["inlet"]["prompts"], lines)
        by_prompt = {prompt.split("\n\n画布硬性要求", 1)[0]: key for prompt, key in sent}
        self.assertEqual(by_prompt["可爱壁纸提示词1"], "sk-image-aaaa")
        self.assertEqual(by_prompt["可爱壁纸提示词2"], "sk-image-bbbb")
        self.assertEqual(by_prompt["可爱壁纸提示词3"], "sk-image-aaaa")
        self.assertEqual(traced["n-image"]["provider"], "api")
        self.assertEqual(len(traced["n-output"]["inlet"]["images"]["items"]), 5)
        self.assertNotIn("prompts", traced["n-output"]["inlet"])
        output = traced["n-output"]["export"]["output"]
        with zipfile.ZipFile(output["zip"]) as archive:
            names = archive.namelist()
        image_names = [name for name in names if name.startswith("images/") and name.endswith(".png")]
        self.assertEqual(image_names, ["images/01.png", "images/02.png", "images/03.png", "images/04.png", "images/05.png"])

    def test_checked_references_send_one_image_and_library_keeps_its_group(self):
        other = self.store.get_element("ref-image-cute")
        other["id"] = "ref-image-other"
        other["name"] = "另一张参考图"
        other["group"] = "其他组"
        other["tags"] = ["别的"]
        self.store.upsert_element(other)
        flow = self.store.get_flow("flow-cute-wallpaper")
        prompts = next(node for node in flow["graph"]["nodes"] if node["id"] == "n-prompts")
        prompts["imports"].append({"alias": "reference_image", "elementType": "reference_image", "elementId": "ref-image-other"})
        output = next(node for node in flow["graph"]["nodes"] if node["id"] == "n-output")
        output["params"]["saveAs"] = "library"
        self.store.upsert_flow(flow)
        calls = self._use_fake_text_model([f"可爱壁纸提示词{index}" for index in range(1, 6)])
        image_model = self.store.get_element("model-image-default")
        image_model["payload"]["model"] = "gpt-image-test"
        self.store.upsert_element(image_model)
        self.store.set_secret("model-image-default", "sk-image")

        def fake_image(profile, key, prompt, width, height, timeout=180):
            return png_bytes(width, height), f"{width}x{height}"

        original = nodes.generate_image
        nodes.generate_image = fake_image
        self.addCleanup(setattr, nodes, "generate_image", original)
        run = self.engine.run("flow-cute-wallpaper", mode="once")
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        self.assertTrue(all(len(item["images"]) == 1 for item in calls))
        self.assertEqual(len({item["images"][0] for item in calls}), 1)
        cute = self.store.get_element("ref-image-cute")
        another = self.store.get_element("ref-image-other")
        self.assertEqual(cute["useCount"] + another["useCount"], 1)
        chosen = cute if cute["useCount"] == 1 else another
        results = self.store.list_results()
        self.assertEqual(len(results), 5)
        self.assertTrue(all(is_uuid_id("result", item["id"]) for item in results))
        self.assertTrue(all(item["group"] == chosen["group"] and item["referenceGroup"] == chosen["group"] for item in results))
        self.assertTrue(all(item["tags"] == chosen["tags"] and item["referenceTags"] == chosen["tags"] for item in results))

    def test_result_direct_zips_unused_images_from_one_group(self):
        def add_result(name, group, worked=False):
            image = Path(self.tmp.name) / "results" / f"{name}.png"
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            return self.store.save_result(
                {
                    "id": str(__import__("uuid").uuid4()),
                    "name": name,
                    "group": group,
                    "tags": ["可爱"],
                    "referenceGroup": group,
                    "referenceTags": ["可爱"],
                    "path": f"results/{name}.png",
                    "prompt": name,
                }
            )

        first = add_result("甲", "可爱壁纸")
        second = add_result("乙", "可爱壁纸")
        add_result("丁", "可爱壁纸")
        add_result("丙", "其他组")
        self.store.mark_results_worked([second["id"]])
        self.store.upsert_flow(
            {
                "id": "flow-direct",
                "name": "直出",
                "description": "",
                "group": "",
                "tags": [],
                "fields": [],
                "schedule": {"enabled": False, "frequency": "daily", "time": "09:00", "weekdays": [], "date": "", "nextRunAt": None},
                "graph": {
                    "nodes": [
                        {
                            "id": "n1",
                            "type": "result.direct",
                            "name": "结果库直出",
                            "x": 40,
                            "y": 40,
                            "imports": [],
                            "rulesBefore": [],
                            "rulesAfter": [],
                            "params": {"group": "可爱壁纸", "count": 1, "directory": "outputs/direct", "zipName": "直出包"},
                        }
                    ],
                    "edges": [],
                },
            }
        )
        run = self.engine.run("flow-direct")
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        zip_path = run["trace"]["nodes"]["n1"]["export"]["output"]["zip"]
        self.assertTrue(Path(zip_path).is_file())
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
        self.assertEqual(names, ["images/01.png"])
        again = self.engine.run("flow-direct")
        self.assertEqual(again["status"], "succeeded", again.get("error"))
        second_zip = again["trace"]["nodes"]["n1"]["export"]["output"]["zip"]
        self.assertNotEqual(second_zip, zip_path)
        self.assertTrue(Path(zip_path).is_file())
        self.assertTrue(Path(second_zip).is_file())
        fresh = self.store.get_element("ref-image-cute")
        self.assertIsNotNone(fresh)
        remaining = self.store.list_results(100, "可爱壁纸", True)
        self.assertEqual(remaining, [])
        worked = self.store.list_results(100, "可爱壁纸", False)
        self.assertTrue(all(item["worked"] for item in worked))
        self.store.set_results_status([first["id"]], False)
        self.assertFalse(self.store.get_result(first["id"])["worked"])
        renamed = self.store.update_result(first["id"], {"name": "甲改", "group": "新分组", "tags": ["夜"], "worked": False})
        self.assertEqual(renamed["name"], "甲改")
        self.assertEqual(renamed["group"], "新分组")
        self.assertEqual(renamed["tags"], ["夜"])
        self.store.delete_results([first["id"]])
        self.assertIsNone(self.store.get_result(first["id"]))
        self.assertEqual(self.store.list_results(100, "其他组", True)[0]["name"], "丙")
        self.assertEqual(first["id"] in {item["id"] for item in worked}, True)

    def test_reference_node_makes_several_prompts(self):
        self.store.upsert_flow(
            {
                "id": "flow-ref-prompts",
                "name": "参考生成提示词",
                "description": "",
                "group": "",
                "tags": [],
                "fields": [],
                "schedule": {"enabled": False, "frequency": "daily", "time": "09:00", "weekdays": [], "date": ""},
                "graph": {
                    "nodes": [
                        {
                            "id": "n1",
                            "type": "prompt.from_refs",
                            "name": "从参考生成提示词",
                            "x": 40,
                            "y": 40,
                            "imports": [
                                {"elementType": "reference_image", "elementId": "ref-image-soft"},
                                {"elementType": "reference_article", "elementId": "ref-article-dusk"},
                                {"elementType": "model_profile", "elementId": "model-text-default"},
                            ],
                            "rulesBefore": [],
                            "rulesAfter": [],
                            "params": {"count": 4, "purpose": "image"},
                        }
                    ],
                    "edges": [],
                },
            }
        )
        calls = self._use_fake_text_model([f"生图提示词{index}" for index in range(1, 5)])
        run = self.engine.run("flow-ref-prompts")
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        self.assertEqual(len(calls), 4)
        self.assertTrue(any("窗边柔光" in item["text"] for item in calls))
        self.assertTrue(all(item["images"] and item["images"][0].endswith("wallpaper-ref.png") for item in calls))
        export = run["trace"]["nodes"]["n1"]["export"]
        self.assertEqual(export["prompts"], ["生图提示词1", "生图提示词2", "生图提示词3", "生图提示词4"])
        self.assertEqual(export["prompt"]["provider"], "api")
        self.assertTrue(export["prompt"]["text"])

    def test_package_roundtrip_leaves_secrets_out(self):
        self.store.set_secrets("model-text-default", ["sk-test-secret-value", "sk-second-secret"])
        self.assertEqual(self.store.get_secrets("model-text-default"), ["sk-test-secret-value", "sk-second-secret"])
        self.assertEqual(self.store.secret_status()["model-text-default"]["count"], 2)
        package = build_flow_package(self.store, self.store.get_flow("flow-wallpaper-article"))
        encoded = json.dumps(package, ensure_ascii=False)
        self.assertNotIn("sk-test-secret-value", encoded)
        self.assertNotIn("sk-second-secret", encoded)
        package["flow"]["id"] = "flow-copy"
        package["flow"]["name"] = "复制的壁纸流程"
        package["elements"][0]["name"] = "改过的元素名"
        imported = import_package(self.store, package)
        self.assertTrue(is_uuid_id("flow", imported["flowId"]))
        self.assertEqual(imported["flowId"], self.store.get_flow("flow-copy")["id"])
        self.assertEqual(self.store.get_flow("flow-copy")["name"], "复制的壁纸流程")


class LegacyTableTests(unittest.TestCase):
    def test_json_element_rows_move_into_tables(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "legacy.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE elements (id TEXT PRIMARY KEY, type TEXT, name TEXT, payload TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO elements VALUES (?, ?, ?, ?, ?)",
            (
                "prompt-old",
                "prompt",
                "旧提示词",
                json.dumps({"purpose": "title", "template": "旧模板", "notes": ""}, ensure_ascii=False),
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
        conn.close()
        store = Store(path)
        self.addCleanup(store.close)
        loaded = store.get_element("prompt-old")
        self.assertEqual(loaded["payload"]["template"], "旧模板")
        row = store.conn.execute("SELECT template FROM prompts WHERE id = 'prompt-old'").fetchone()
        self.assertEqual(row["template"], "旧模板")
        names = {item[0] for item in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertNotIn("elements", names)
        self.assertIn("reference_articles", names)
        self.assertIn("elements_json_backup", names)


if __name__ == "__main__":
    unittest.main()
