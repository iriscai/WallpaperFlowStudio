"""Import reference images from a ZIP with shared library metadata."""

from __future__ import annotations

import stat
import uuid
import zipfile
from pathlib import PurePosixPath

from app.catalog import blank_payload
from app.ids import new_id
from app.paths import data_home

MAX_ZIP_BYTES = 200_000_000
MAX_IMAGE_BYTES = 15_000_000
MAX_EXPANDED_BYTES = 500_000_000
MAX_IMAGES = 2000
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def image_filename(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if not info.flag_bits & 0x800:
        try:
            name = name.encode("cp437").decode("gb18030")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name.replace("\\", "/")


def matches_image(data: bytes, suffix: str) -> bool:
    if suffix == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return data.startswith(b"\xff\xd8\xff")
    if suffix == ".gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    return data.startswith(b"RIFF") and data[8:12] == b"WEBP"


def import_reference_zip(store, source, group: str = "", tags: list[str] | None = None, note: str = "") -> dict:
    if not isinstance(tags, (list, type(None))) or any(not isinstance(tag, str) for tag in tags or []):
        raise ValueError("标签需要文字数组")
    tags = list(dict.fromkeys(tag.strip() for tag in tags or [] if tag.strip()))
    if not isinstance(group, str) or not isinstance(note, str):
        raise ValueError("分组和说明需要文字")
    try:
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("无法读取 ZIP，请选择有效的 ZIP 压缩包") from exc
    created_paths = []
    batch_dir = None
    skipped = 0
    try:
        with archive:
            entries = archive.infolist()
            if len(entries) > 10000:
                raise ValueError("ZIP 内文件过多，每次最多包含 10000 个条目")
            images = []
            expanded = 0
            for entry in entries:
                if entry.is_dir():
                    continue
                name = image_filename(entry)
                path = PurePosixPath(name)
                if "__MACOSX" in path.parts or path.name.startswith(".") or path.suffix.lower() not in IMAGE_SUFFIXES:
                    skipped += 1
                    continue
                if path.is_absolute() or ".." in path.parts or ":" in name or stat.S_ISLNK(entry.external_attr >> 16):
                    raise ValueError("ZIP 中的图片路径不正确：" + name)
                if entry.flag_bits & 1:
                    raise ValueError("请先解密 ZIP 中的图片：" + name)
                if entry.file_size <= 0 or entry.file_size > MAX_IMAGE_BYTES:
                    raise ValueError("图片为空或超过 15MB：" + name)
                expanded += entry.file_size
                if expanded > MAX_EXPANDED_BYTES:
                    raise ValueError("ZIP 内图片解压后合计不能超过 500MB")
                images.append((entry, path))
            if not images:
                raise ValueError("ZIP 中没有可导入的图片，支持 PNG、JPG、JPEG、WebP、GIF")
            if len(images) > MAX_IMAGES:
                raise ValueError("每次最多导入 2000 张参考图")
            relative_dir = "uploads/reference-images/" + uuid.uuid4().hex
            target_dir = data_home() / relative_dir
            target_dir.mkdir(parents=True, exist_ok=False)
            batch_dir = target_dir
            with store.transaction():
                for entry, path in images:
                    try:
                        with archive.open(entry) as member:
                            data = member.read(MAX_IMAGE_BYTES + 1)
                    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
                        raise ValueError("无法读取 ZIP 中的图片：" + str(path)) from exc
                    suffix = path.suffix.lower()
                    if len(data) > MAX_IMAGE_BYTES or not matches_image(data, suffix):
                        raise ValueError("图片格式不正确或文件损坏：" + str(path))
                    record_id = new_id("reference_image")
                    filename = uuid.uuid4().hex + suffix
                    target = batch_dir / filename
                    created_paths.append(target)
                    target.write_bytes(data)
                    payload = blank_payload("reference_image")
                    payload.update({"path": relative_dir + "/" + filename, "note": note})
                    store.upsert_element({"id": record_id, "type": "reference_image", "name": path.stem, "group": group.strip(), "tags": tags, "status": "启用", "fields": [], "payload": payload})
        return {"ok": True, "created": len(images), "skipped": skipped}
    except Exception:
        for target in created_paths:
            target.unlink(missing_ok=True)
        if batch_dir is not None:
            batch_dir.rmdir()
        raise
