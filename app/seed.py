"""First-run library and the wallpaper-article flow."""

from __future__ import annotations

from app.catalog import BLANK_SCHEDULE
from app.pngutil import write_gradient
from app.paths import PROJECT_ROOT


def ensure_sample_image() -> None:
    path = PROJECT_ROOT / "resources" / "samples" / "wallpaper-ref.png"
    if path.exists():
        return
    write_gradient(path, 96, 144, "窗边柔光")


def seed_if_empty(store) -> None:
    ensure_sample_image()
    if store.get_setting("seeded") != "1":
        for element in ELEMENTS:
            store.upsert_element(element)
        store.upsert_flow(FLOW)
        store.set_setting("seeded", "1")
    ensure_cute_wallpaper(store)
    ensure_reference_carry(store)


def _element(element_id: str, element_type: str, name: str, payload: dict) -> dict:
    return {
        "id": element_id,
        "type": element_type,
        "name": name,
        "group": "壁纸案例",
        "tags": ["案例"],
        "fields": [],
        "payload": payload,
    }


def _import(element_id: str, element_type: str) -> dict:
    return {"alias": element_type, "elementType": element_type, "elementId": element_id}


def _node(node_id, node_type, name, x, y, imports, params=None) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "name": name,
        "x": x,
        "y": y,
        "imports": imports,
        "rulesBefore": [],
        "rulesAfter": [],
        "params": params or {},
    }


def ensure_reference_carry(store) -> None:
    flow = store.get_flow("flow-cute-wallpaper")
    if not flow:
        return
    changed = False
    nodes = (flow.get("graph") or {}).get("nodes") or []
    prompt_ids = {node["id"] for node in nodes if node.get("type") == "prompt.from_refs"}
    image_ids = {node["id"] for node in nodes if node.get("type") == "image.generate"}
    for edge in (flow.get("graph") or {}).get("edges") or []:
        if edge.get("from") not in prompt_ids or edge.get("to") not in image_ids:
            continue
        carry = edge.setdefault("carry", [])
        if any(item.get("source") == "referenceUsed" for item in carry):
            continue
        carry.append({"source": "referenceUsed", "target": "referenceUsed", "op": "copy"})
        changed = True
    if changed:
        store.upsert_flow(flow)


def ensure_cute_wallpaper(store) -> None:
    path = PROJECT_ROOT / "resources" / "samples" / "cute-wallpaper-ref.png"
    if not path.exists():
        write_gradient(path, 96, 160, "可爱壁纸")
    for element in CUTE_ELEMENTS:
        if store.get_element(element["id"]) is None:
            store.upsert_element(element)
    if store.get_flow(CUTE_FLOW["id"]) is None:
        store.upsert_flow(CUTE_FLOW)


def _edge(edge_id, source, target, fields, rules=None) -> dict:
    return {
        "id": edge_id,
        "from": source,
        "to": target,
        "carry": [{"source": field, "target": field, "op": "copy"} for field in fields],
        "rules": rules or [],
    }


ELEMENTS = [
    _element(
        "domain-wallpaper",
        "domain",
        "手机壁纸",
        {
            "summary": "手机锁屏和桌面壁纸的公众号内容",
            "audience": "喜欢收集壁纸、愿意为画面停留的读者",
            "tone": "治愈、清透、句子短",
            "tags": ["壁纸", "锁屏", "氛围"],
        },
    ),
    _element(
        "theme-autumn-window",
        "theme",
        "秋日窗边治愈壁纸",
        {"text": "秋日窗边治愈壁纸", "notes": "案例默认主题。单次执行时可以临时换成别的。"},
    ),
    _element(
        "ref-article-dusk",
        "reference_article",
        "把黄昏折进锁屏",
        {
            "title": "把黄昏折进锁屏",
            "usage": "结构",
            "body": "傍晚的光只在窗帘边上停一会儿。写壁纸文时，先告诉读者光从哪来，再给一个静止的物件，最后留一句短的，让人愿意把画面放进锁屏。不要堆形容词，也不要承诺一套图。",
        },
    ),
    _element(
        "ref-copy-wind",
        "reference_copy",
        "锁屏上的风",
        {"text": "锁屏上的风，刚好停在窗帘边上", "usage": "标题"},
    ),
    _element(
        "ref-copy-lamp",
        "reference_copy",
        "把房间的灯调暗",
        {"text": "今天的壁纸，想把房间的灯调暗一点", "usage": "开头"},
    ),
    _element(
        "ref-image-soft",
        "reference_image",
        "窗边柔光",
        {"path": "resources/samples/wallpaper-ref.png", "role": "色彩", "note": "上半屏留白，暖边收在窗帘附近"},
    ),
    _element(
        "prompt-title-wallpaper",
        "prompt",
        "壁纸标题",
        {
            "purpose": "title",
            "notes": "供生成标题节点使用，也可先经生成提示词节点渲染后再携带。",
            "template": "请为主题「{{theme.text}}」写 3 个微信公众号标题。\n领域：{{theme.domain.name}}\n语气：{{theme.domain.tone}}\n读者：{{theme.domain.audience}}\n\n要求：\n- 每行一个标题\n- 不要编号，不要解释\n- 标题具体，能看出画面，适合壁纸类账号\n- 可以借鉴参考文案的口气，不要照抄",
        },
    ),
    _element(
        "prompt-article-wallpaper",
        "prompt",
        "壁纸文章",
        {
            "purpose": "article",
            "notes": "写文章节点导入这份。标题提示词不要携带进这个节点，否则会盖过它。",
            "template": "请写一篇微信公众号文章。\n\n主题：{{theme.text}}\n标题：{{primaryTitle}}\n领域语气：{{theme.domain.tone}}\n\n要求：\n- 正文 400 到 700 字\n- 分成 3 到 4 个短段\n- 写可感知的画面，适合配手机壁纸\n- 不编造下载链接、价格和活动\n- 参考文章只作结构参考，不要照抄句子",
        },
    ),
    _element(
        "prompt-image-wallpaper",
        "prompt",
        "壁纸生图",
        {
            "purpose": "image",
            "notes": "生图节点导入。文章画面由连线上的规则写入 articleExcerpt。",
            "template": "手机竖版壁纸，干净，适合锁屏。\n主题：{{theme.text}}\n标题：{{primaryTitle}}\n画面：{{articleExcerpt}}\n风格：{{style}}\n不要文字，不要水印，不要标识。",
        },
    ),
    _element(
        "image-group-portrait",
        "image_config_group",
        "竖版壁纸·清透",
        {
            "variants": [
                {
                    "name": "清透竖版",
                    "width": 1080,
                    "height": 1920,
                    "count": 1,
                    "style": "柔光胶片，浅景深，留白",
                    "negative": "文字，水印，低清晰度",
                }
            ]
        },
    ),
    _element(
        "model-text-default",
        "model_profile",
        "默认文本模型",
        {
            "kind": "text",
            "baseUrl": "https://api.openai.com/v1",
            "model": "",
            "temperature": 0.7,
            "maxTokens": None,
        },
    ),
    _element(
        "model-image-default",
        "model_profile",
        "默认生图模型",
        {
            "kind": "image",
            "baseUrl": "https://api.openai.com/v1",
            "model": "",
            "temperature": 0.7,
            "maxTokens": None,
        },
    ),
    _element(
        "output-wallpaper",
        "output_target",
        "壁纸文章产出",
        {
            "directory": "outputs/wallpaper-article",
            "writeFolder": True,
            "writeZip": True,
            "zipName": "壁纸文章-{{theme.text}}-{{date}}",
        },
    ),
    _element(
        "rule-require-titles",
        "rule",
        "必须有标题",
        {"steps": [{"op": "require", "field": "titles.items", "from": "", "template": "", "value": "", "message": "没有标题，文章节点不会执行"}]},
    ),
    _element(
        "rule-primary-title",
        "rule",
        "抽出主标题",
        {"steps": [{"op": "set", "field": "primaryTitle", "from": "titles.items.0.text", "template": "", "value": "", "message": ""}]},
    ),
    _element(
        "rule-require-articles",
        "rule",
        "必须有文章",
        {"steps": [{"op": "require", "field": "articles.items", "from": "", "template": "", "value": "", "message": "没有文章，生图节点不会执行"}]},
    ),
    _element(
        "rule-article-excerpt",
        "rule",
        "抽出文章画面",
        {"steps": [{"op": "template", "field": "articleExcerpt", "from": "", "template": "{{articles.items.0.body}}", "value": "", "message": ""}]},
    ),
]

FLOW = {
    "id": "flow-wallpaper-article",
    "name": "公众号壁纸文章任务流程",
    "description": "选取主题后生成标题提示词，再生成标题、文章和图片，最后把文章与图片写入文件夹并打包 ZIP。连线上只传递点名的字段。提示词、参考图、生图配置和输出位置都是可替换元素。",
    "group": "壁纸案例",
    "tags": ["案例", "壁纸"],
    "fields": [{"key": "账号", "value": "壁纸号"}],
    "schedule": dict(BLANK_SCHEDULE),
    "graph": {
        "nodes": [
            _node(
                "n-theme",
                "start.theme",
                "选取主题",
                48,
                180,
                [_import("theme-autumn-window", "theme"), _import("domain-wallpaper", "domain")],
            ),
            _node(
                "n-title-prompt",
                "prompt.compose",
                "生成标题提示词",
                340,
                180,
                [_import("prompt-title-wallpaper", "prompt")],
                {"keepInlet": True, "useModel": False},
            ),
            _node(
                "n-title",
                "text.title",
                "生成标题",
                632,
                180,
                [
                    _import("prompt-title-wallpaper", "prompt"),
                    _import("ref-article-dusk", "reference_article"),
                    _import("ref-copy-wind", "reference_copy"),
                    _import("ref-copy-lamp", "reference_copy"),
                    _import("model-text-default", "model_profile"),
                ],
                {"keepInlet": True, "contextMode": "auto"},
            ),
            _node(
                "n-article",
                "text.article",
                "生成文章",
                924,
                180,
                [
                    _import("prompt-article-wallpaper", "prompt"),
                    _import("ref-article-dusk", "reference_article"),
                    _import("domain-wallpaper", "domain"),
                    _import("model-text-default", "model_profile"),
                ],
                {"keepInlet": True, "contextMode": "auto"},
            ),
            _node(
                "n-image",
                "image.generate",
                "生成图片",
                1216,
                180,
                [
                    _import("prompt-image-wallpaper", "prompt"),
                    _import("image-group-portrait", "image_config_group"),
                    _import("model-image-default", "model_profile"),
                ],
                {"keepInlet": True},
            ),
            _node(
                "n-output",
                "file.output",
                "输出文章与图片",
                1508,
                180,
                [_import("output-wallpaper", "output_target")],
            ),
        ],
        "edges": [
            _edge("e-theme-prompt", "n-theme", "n-title-prompt", ["theme"]),
            _edge("e-prompt-title", "n-title-prompt", "n-title", ["theme", "prompt"]),
            _edge("e-title-article", "n-title", "n-article", ["theme", "titles"], ["rule-require-titles", "rule-primary-title"]),
            _edge(
                "e-article-image",
                "n-article",
                "n-image",
                ["theme", "titles", "articles", "primaryTitle"],
                ["rule-require-articles", "rule-article-excerpt"],
            ),
            _edge("e-image-output", "n-image", "n-output", ["theme", "titles", "articles", "images"]),
        ],
    },
}


CUTE_ELEMENTS = [
    _element(
        "theme-cute-wallpaper",
        "theme",
        "可爱壁纸",
        {"text": "可爱壁纸", "notes": "可爱壁纸生成案例的主题。"},
    ),
    _element(
        "ref-image-cute",
        "reference_image",
        "可爱壁纸参考",
        {
            "path": "resources/samples/cute-wallpaper-ref.png",
            "role": "风格",
            "note": "圆润的小动物或甜点，粉彩，柔光，主体清楚，上半屏留白，适合手机锁屏",
        },
    ),
    _element(
        "prompt-cute-wallpaper",
        "prompt",
        "可爱壁纸生图要求",
        {
            "purpose": "image",
            "notes": "参考生成提示词时的补充要求。真正送给生图模型的是生成出来的 5 条。",
            "template": "每条都要能直接送给生图模型。画面是可爱手机竖版壁纸：Q版、圆润、粉彩、柔光、主体清楚、上半屏留白。五条彼此不同。不要文字，不要水印。",
        },
    ),
    _element(
        "image-group-cute",
        "image_config_group",
        "可爱竖版壁纸",
        {
            "variants": [
                {
                    "name": "可爱竖版",
                    "width": 1080,
                    "height": 1920,
                    "count": 1,
                    "style": "可爱、粉彩、柔光、圆润",
                    "negative": "文字，水印，写实恐怖，杂乱背景",
                }
            ]
        },
    ),
    _element(
        "output-cute-wallpaper",
        "output_target",
        "可爱壁纸打包",
        {
            "directory": "outputs/cute-wallpaper",
            "writeFolder": True,
            "writeZip": True,
            "zipName": "可爱壁纸-{{date}}",
        },
    ),
]

for _cute in CUTE_ELEMENTS:
    _cute["group"] = "可爱壁纸"
    _cute["tags"] = ["案例", "可爱"]


CUTE_FLOW = {
    "id": "flow-cute-wallpaper",
    "name": "可爱壁纸生成",
    "description": "参考图生成 5 份提示词后，把这 5 份提示词传入生图节点，各调用一次生图模型，再把 5 张图片传入打包节点打成 ZIP。",
    "group": "可爱壁纸",
    "tags": ["案例", "壁纸", "可爱"],
    "fields": [{"key": "张数", "value": "5"}],
    "schedule": dict(BLANK_SCHEDULE),
    "graph": {
        "nodes": [
            _node(
                "n-theme",
                "start.theme",
                "选取可爱主题",
                48,
                180,
                [_import("theme-cute-wallpaper", "theme"), _import("domain-wallpaper", "domain")],
            ),
            _node(
                "n-prompts",
                "prompt.from_refs",
                "参考图生成5份提示词",
                360,
                180,
                [
                    _import("ref-image-cute", "reference_image"),
                    _import("prompt-cute-wallpaper", "prompt"),
                    _import("model-text-default", "model_profile"),
                ],
                {"count": 5, "purpose": "image", "keepInlet": True},
            ),
            _node(
                "n-image",
                "image.generate",
                "生成5张图片",
                760,
                180,
                [
                    _import("model-image-default", "model_profile"),
                ],
                {"keepInlet": True},
            ),
            _node(
                "n-output",
                "file.output",
                "打包成ZIP",
                1120,
                180,
                [_import("output-cute-wallpaper", "output_target")],
            ),
        ],
        "edges": [
            _edge("e-theme-prompts", "n-theme", "n-prompts", ["theme"]),
            _edge("e-prompts-image", "n-prompts", "n-image", ["prompts", "referenceUsed"]),
            _edge("e-image-output", "n-image", "n-output", ["images"]),
        ],
    },
}
