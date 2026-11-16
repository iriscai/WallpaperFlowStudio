"""Node types and blank elements. Nodes do the work; elements are the swappable parts."""

from __future__ import annotations

import copy

NODE_TYPES = [
    {
        "type": "start.theme",
        "group": "开始",
        "label": "开始·选取主题",
        "blurb": "确定这一轮的主题，并带上领域。",
        "product": ["theme"],
        "forward": [],
        "accepts": [],
        "hints": ["theme", "theme.text", "theme.domain.name", "theme.domain.tone", "theme.domain.audience"],
        "elementTypes": [
            {"type": "theme", "label": "主题"},
            {"type": "domain", "label": "领域"},
        ],
    },
    {
        "type": "prompt.compose",
        "group": "生成",
        "label": "生成提示词",
        "blurb": "用提示词元素和已经携带进来的字段，渲染出下一段要用的提示词。",
        "product": ["prompt"],
        "forward": ["theme", "titles", "articles", "primaryTitle", "articleExcerpt"],
        "accepts": ["theme", "titles", "articles", "primaryTitle", "articleExcerpt"],
        "hints": ["prompt", "prompt.text", "prompt.purpose"],
        "elementTypes": [{"type": "prompt", "label": "提示词"}],
    },
    {
        "type": "prompt.from_refs",
        "group": "生成",
        "label": "参考生成提示词",
        "blurb": "勾选参考图或参考文章，调用生文模型按条数生成 1 到 N 条提示词。",
        "product": ["prompts", "prompt"],
        "forward": ["theme"],
        "accepts": ["theme"],
        "hints": ["prompts", "prompt", "prompt.text", "referenceUsed.name", "referenceUsed.note", "title"],
        "elementTypes": [
            {"type": "reference_image", "label": "参考图"},
            {"type": "reference_article", "label": "参考文章"},
            {"type": "prompt", "label": "补充说明"},
            {"type": "model_profile", "kind": "text", "label": "生文模型"},
        ],
    },
    {
        "type": "prompt.from_multi_refs",
        "group": "生成",
        "label": "多参考图生成提示词",
        "blurb": "从当前筛选内已勾选的参考图中随机抽取 N 张，一起交给生文模型生成提示词。",
        "product": ["prompts", "prompt", "referenceUsed", "referencesUsed", "referenceNames", "referenceNotes"],
        "forward": ["theme"],
        "accepts": ["theme"],
        "hints": ["prompts", "prompt.text", "referencesUsed", "referencesUsed.0.name", "referencesUsed.0.note", "referenceNames", "referenceNotes", "referenceUsed.name", "referenceUsed.note"],
        "elementTypes": [
            {"type": "reference_image", "label": "参考图候选"},
            {"type": "reference_article", "label": "参考文章"},
            {"type": "prompt", "label": "补充说明"},
            {"type": "model_profile", "kind": "text", "label": "生文模型"},
        ],
        "defaults": {"referenceCount": 2, "count": 3, "purpose": "image"},
    },
    {
        "type": "text.title",
        "group": "生成",
        "label": "生成标题",
        "blurb": "用提示词，加上携带进来的主题、参考文章和参考文案，生成标题。",
        "product": ["titles"],
        "forward": ["theme"],
        "accepts": ["theme", "prompt"],
        "hints": ["titles", "titles.items", "titles.items.0.text"],
        "elementTypes": [
            {"type": "prompt", "purpose": "title", "label": "标题提示词"},
            {"type": "reference_article", "label": "参考文章"},
            {"type": "reference_copy", "label": "参考文案"},
            {"type": "domain", "label": "领域"},
            {"type": "model_profile", "kind": "text", "label": "文本模型"},
        ],
    },
    {
        "type": "text.article",
        "group": "生成",
        "label": "生成文章",
        "blurb": "用上游标题和本节点的文章提示词生成正文。",
        "product": ["articles"],
        "forward": ["theme", "titles", "primaryTitle"],
        "accepts": ["theme", "titles", "primaryTitle", "prompt"],
        "hints": ["articles", "articles.items", "articles.items.0.title", "articles.items.0.body"],
        "elementTypes": [
            {"type": "prompt", "purpose": "article", "label": "文章提示词"},
            {"type": "reference_article", "label": "参考文章"},
            {"type": "reference_copy", "label": "参考文案"},
            {"type": "domain", "label": "领域"},
            {"type": "model_profile", "kind": "text", "label": "文本模型"},
        ],
    },
    {
        "type": "image.generate",
        "group": "生成",
        "label": "生成图片",
        "blurb": "按提示词数组或生图配置组生成图片。整批统一使用节点指定尺寸，支持头像、壁纸预设及自定义宽高。",
        "product": ["images", "imageSize"],
        "forward": ["theme", "titles", "articles", "primaryTitle", "articleExcerpt", "prompts", "prompt"],
        "accepts": ["theme", "titles", "articles", "primaryTitle", "prompt", "prompts", "articleExcerpt", "referenceUsed", "referencesUsed"],
        "hints": ["images", "images.size", "images.items", "images.items.0.path", "images.items.0.prompt", "images.items.0.actualSize", "imageSize.width", "imageSize.height", "imageSize.ratio", "prompts"],
        "elementTypes": [
            {"type": "prompt", "purpose": "image", "label": "生图提示词"},
            {"type": "image_config_group", "label": "生图配置组"},
            {"type": "model_profile", "kind": "image", "label": "生图模型"},
        ],
    },
    {
        "type": "rule.apply",
        "group": "整理",
        "label": "规则变换",
        "blurb": "不调用模型。按规则改写字段，再交给下游。",
        "product": ["payload"],
        "forward": ["theme", "titles", "articles", "images", "primaryTitle", "prompt", "articleExcerpt"],
        "accepts": ["theme", "titles", "articles", "images", "prompt", "primaryTitle", "articleExcerpt", "payload"],
        "hints": ["payload", "theme", "titles", "primaryTitle", "articleExcerpt"],
        "elementTypes": [{"type": "rule", "label": "规则"}],
    },
    {
        "type": "file.output",
        "group": "输出",
        "label": "文件输出",
        "blurb": "把流程里的文章和图片写进文件夹，或打成 ZIP。",
        "product": ["output"],
        "forward": [],
        "accepts": ["theme", "titles", "articles", "images", "imageSize", "prompt", "prompts"],
        "hints": ["output", "output.folder", "output.zip", "output.files"],
        "elementTypes": [{"type": "output_target", "label": "输出目标"}],
    },
    {
        "type": "work.generate",
        "group": "输出",
        "label": "生成作品",
        "blurb": "按日期汇总作品，用携带参数命名作品文件夹和 ZIP。",
        "product": ["output"],
        "forward": [],
        "accepts": ["images", "imageSize", "title", "referenceUsed"],
        "hints": ["title", "images", "output.title", "output.folder", "output.zip", "output.files", "output.collection"],
        "elementTypes": [],
        "defaults": {"directory": "outputs/works", "nameTemplate": "ins${title}${ending}"},
    },
    {
        "type": "result.direct",
        "group": "输出",
        "label": "结果库直出",
        "blurb": "从结果库的一个分组里，随机抽出 N 张还没出过作品的图，直接打成 ZIP。",
        "product": ["output", "images"],
        "forward": [],
        "accepts": [],
        "hints": ["output", "output.zip", "images"],
        "elementTypes": [],
    },
]

ELEMENT_TYPES = [
    {"type": "domain", "label": "领域"},
    {"type": "theme", "label": "主题"},
    {"type": "reference_image", "label": "参考图"},
    {"type": "reference_article", "label": "参考文章"},
    {"type": "reference_copy", "label": "参考文案"},
    {"type": "prompt", "label": "提示词"},
    {"type": "image_config_group", "label": "生图配置组"},
    {"type": "model_profile", "label": "API配置"},
    {"type": "output_target", "label": "输出目标"},
    {"type": "rule", "label": "规则"},
]

ELEMENT_DEFAULTS = {
    "domain": {"summary": "", "audience": "", "tone": "", "tags": []},
    "theme": {"text": "", "notes": ""},
    "reference_image": {"path": "", "role": "风格", "note": ""},
    "reference_article": {"title": "", "body": "", "usage": "结构"},
    "reference_copy": {"text": "", "usage": "标题"},
    "prompt": {"purpose": "article", "template": "", "notes": ""},
    "image_config_group": {
        "variants": [
            {"name": "默认", "width": 1080, "height": 1920, "count": 1, "style": "", "negative": "文字，水印"}
        ]
    },
    "model_profile": {
        "kind": "text",
        "baseUrl": "https://api.openai.com/v1",
        "model": "",
        "temperature": 0.7,
        "maxTokens": None,
        "concurrency": None,
    },
    "output_target": {
        "directory": "outputs/default",
        "writeFolder": True,
        "writeZip": False,
        "zipName": "{{flowName}}-{{date}}",
    },
    "rule": {"steps": [{"op": "require", "field": "", "from": "", "template": "", "value": "", "message": ""}]},
}

BLANK_SCHEDULE = {
    "enabled": False,
    "frequency": "daily",
    "time": "09:00",
    "weekdays": [],
    "date": "",
    "nextRunAt": None,
}


def node_type_map() -> dict:
    return {item["type"]: item for item in NODE_TYPES}


def blank_payload(element_type: str) -> dict:
    if element_type not in ELEMENT_DEFAULTS:
        raise KeyError(element_type)
    return copy.deepcopy(ELEMENT_DEFAULTS[element_type])


def blank_node(node_type: str, node_id: str, x: int, y: int) -> dict:
    info = node_type_map()[node_type]
    keep = node_type not in ("start.theme", "file.output", "result.direct", "work.generate")
    return {
        "id": node_id,
        "type": node_type,
        "name": info["label"],
        "x": x,
        "y": y,
        "imports": [],
        "rulesBefore": [],
        "rulesAfter": [],
        "params": {"keepInlet": keep, **copy.deepcopy(info.get("defaults") or {})},
    }
