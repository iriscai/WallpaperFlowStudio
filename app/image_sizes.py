"""One canvas per image node, shared by request settings and saved pixels."""

from __future__ import annotations

import io
import math
import re

from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

from app.carry import FlowError


IMAGE_SIZE_PRESETS = [
    {"id": "avatar", "label": "头像默认 · 1024 × 1024 · 1:1", "width": 1024, "height": 1024},
    {"id": "wallpaper", "label": "壁纸默认 · 1080 × 1920 · 9:16", "width": 1080, "height": 1920},
    {"id": "square-512", "label": "小头像 · 512 × 512 · 1:1", "width": 512, "height": 512},
    {"id": "square-768", "label": "头像 · 768 × 768 · 1:1", "width": 768, "height": 768},
    {"id": "square-1536", "label": "高清头像 · 1536 × 1536 · 1:1", "width": 1536, "height": 1536},
    {"id": "square-2048", "label": "高清方图 · 2048 × 2048 · 1:1", "width": 2048, "height": 2048},
    {"id": "portrait-720", "label": "手机壁纸 · 720 × 1280 · 9:16", "width": 720, "height": 1280},
    {"id": "portrait-1440", "label": "高清壁纸 · 1440 × 2560 · 9:16", "width": 1440, "height": 2560},
    {"id": "portrait-tall", "label": "全面屏壁纸 · 1080 × 2400 · 9:20", "width": 1080, "height": 2400},
    {"id": "portrait-2-3", "label": "竖图 · 1024 × 1536 · 2:3", "width": 1024, "height": 1536},
    {"id": "portrait-3-4", "label": "竖图 · 1080 × 1440 · 3:4", "width": 1080, "height": 1440},
    {"id": "landscape-16-9", "label": "横屏壁纸 · 1920 × 1080 · 16:9", "width": 1920, "height": 1080},
    {"id": "landscape-3-2", "label": "横图 · 1536 × 1024 · 3:2", "width": 1536, "height": 1024},
    {"id": "landscape-4-3", "label": "横图 · 1440 × 1080 · 4:3", "width": 1440, "height": 1080},
]


def default_size_preset(flow: dict) -> str:
    name = str(flow.get("name") or "")
    return "avatar" if "头像" in name else "wallpaper"


def image_size_config(node: dict, flow: dict) -> dict:
    params = node.get("params") or {}
    preset_id = params.get("sizePreset") or default_size_preset(flow)
    if preset_id == "custom":
        dimensions = []
        for key, label in (("imageWidth", "宽度"), ("imageHeight", "高度")):
            raw = params.get(key)
            if isinstance(raw, bool) or not re.fullmatch(r"\d+", str(raw)) or not 256 <= int(raw) <= 4096:
                raise FlowError(f"自定义图片{label}必须是 256 到 4096 之间的整数像素。")
            dimensions.append(int(raw))
        width, height = dimensions
    else:
        preset = next((item for item in IMAGE_SIZE_PRESETS if item["id"] == preset_id), None)
        if preset is None:
            raise FlowError("不认识的生图尺寸预设，请重新选择头像、壁纸或自定义尺寸。")
        width, height = preset["width"], preset["height"]
    divisor = math.gcd(width, height)
    return {"preset": preset_id, "width": width, "height": height, "ratio": f"{width // divisor}:{height // divisor}"}


def canvas_prompt(prompt: str, size: dict) -> str:
    return prompt.rstrip() + (
        f"\n\n画布硬性要求：{size['width']}x{size['height']} 像素，宽高比 {size['ratio']}。"
        "本批每张图片均使用此尺寸，本要求优先于上文的尺寸、比例及参考图原始画幅。"
        "请按此画幅安排构图，生成一张完整独立作品。"
    )


def normalize_canvas(blob: bytes, width: int, height: int) -> tuple[bytes, dict]:
    """Keep all foreground content; fit and pad when the API changes the canvas."""
    try:
        with Image.open(io.BytesIO(blob)) as opened:
            source_format = opened.format
            image = ImageOps.exif_transpose(opened)
            original_size = image.size
            if original_size == (width, height) and source_format == "PNG" and not opened.getexif():
                image.load()
                return blob, {"originalSize": list(original_size), "actualSize": [width, height], "sizeAdjusted": False}
            image = image.convert("RGBA")
            if original_size != (width, height):
                foreground = ImageOps.contain(image, (width, height), Image.Resampling.LANCZOS)
                if foreground.size == (width, height):
                    image = foreground
                else:
                    background = ImageOps.fit(image.convert("RGB"), (width, height), Image.Resampling.LANCZOS)
                    background = background.filter(ImageFilter.GaussianBlur(max(8, min(width, height) // 32))).convert("RGBA")
                    background.alpha_composite(foreground, ((width - foreground.width) // 2, (height - foreground.height) // 2))
                    image = background
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue(), {"originalSize": list(original_size), "actualSize": [width, height], "sizeAdjusted": original_size != (width, height)}
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise FlowError("生图接口返回的图片无法读取或校验尺寸，请检查图片接口响应。") from exc
