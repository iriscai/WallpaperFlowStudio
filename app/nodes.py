"""Node handlers. Each one reads inlet fields plus imported elements and returns an export."""

from __future__ import annotations

import copy
import random
import re
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from app.carry import FlowError, apply_rule, get_path, render
from app.ids import new_id
from app.image_sizes import canvas_prompt, image_size_config, normalize_canvas
from app.paths import resolve_path
from app.pngutil import fit_size, write_gradient
from app.providers import KeyPool, chat, generate_image

MAX_IMAGES = 12
WORK_TITLE_ENDINGS = ("｜可直存", "｜自取", "｜喜欢就存", "·换上试试", "｜拿图随意", "·存一组")


@dataclass
class NodeResult:
    export: dict
    provider: str
    message: str


@dataclass
class Ctx:
    node: dict
    inlet: dict
    elements: list
    by_type: dict
    flow: dict
    run_id: str
    key_for: callable
    keys_for: callable
    scratch: Path
    mark_used: callable
    save_result: callable
    list_results: callable
    mark_results_worked: callable
    key_pools: dict[str, KeyPool] = field(default_factory=dict)
    execution_config: dict | None = None


def _profile_keys(ctx: Ctx, profile: dict) -> list[str]:
    getter = getattr(ctx, "keys_for", None)
    found = getter(profile["id"]) if getter else []
    keys = [str(item).strip() for item in (found or []) if str(item).strip()]
    if keys:
        return keys
    single = str(ctx.key_for(profile["id"]) or "").strip() if ctx.key_for else ""
    return [single] if single else []


def _worker_count(profile: dict, jobs: int, keys: list[str], override: int | None = None) -> int:
    raw = override if override is not None else (profile.get("payload") or {}).get("concurrency")
    try:
        chosen = int(raw) if raw not in (None, "") else len(keys)
    except (TypeError, ValueError):
        chosen = len(keys)
    return max(1, min(chosen or 1, jobs or 1, 12))


def _key_pool(ctx: Ctx, profile: dict, keys: list[str]) -> KeyPool:
    if profile["id"] not in ctx.key_pools:
        ctx.key_pools[profile["id"]] = KeyPool(keys)
    return ctx.key_pools[profile["id"]]


def _parallel(count: int, workers: int, job):
    if count <= 1 or workers <= 1:
        return [job(index) for index in range(count)]
    results = [None] * count
    errors = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(job, index): index for index in range(count)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                errors.append(exc)
    if errors:
        raise errors[0]
    return results


def run_node(ctx: Ctx) -> NodeResult:
    kind = ctx.node.get("type")
    handlers = {
        "start.theme": run_theme,
        "prompt.compose": run_prompt,
        "prompt.from_refs": run_prompts_from_references,
        "prompt.from_multi_refs": run_prompts_from_references,
        "text.title": run_title,
        "text.article": run_article,
        "image.generate": run_image,
        "rule.apply": run_rules,
        "file.output": run_output,
        "work.generate": run_work,
        "result.direct": run_result_direct,
    }
    handler = handlers.get(kind)
    if handler is None:
        raise FlowError(f"不认识的节点类型：{kind}")
    return handler(ctx)


def run_theme(ctx: Ctx) -> NodeResult:
    params = ctx.node.get("params") or {}
    theme_el = _first(ctx.by_type.get("theme"))
    domain_el = _first(ctx.by_type.get("domain"))
    text = str(params.get("themeText") or "").strip()
    if not text and theme_el:
        text = str(theme_el["payload"].get("text") or "").strip()
    if not text:
        raise FlowError("还没有主题。在节点里填写，或导入一个主题元素。")
    domain = {}
    if domain_el:
        payload = domain_el["payload"]
        domain = {
            "id": domain_el["id"],
            "name": domain_el["name"],
            "summary": payload.get("summary") or "",
            "audience": payload.get("audience") or "",
            "tone": payload.get("tone") or "",
            "tags": payload.get("tags") or [],
        }
    export = {
        "theme": {
            "text": text,
            "domain": domain,
            "themeId": theme_el["id"] if theme_el else "",
            "notes": (theme_el or {}).get("payload", {}).get("notes") or "",
        }
    }
    return NodeResult(export, "local", f"已选定主题「{text}」")


def run_prompt(ctx: Ctx) -> NodeResult:
    prompt_el = _choose_prompt(ctx.by_type.get("prompt") or [], "")
    if prompt_el is None:
        raise FlowError("生成提示词节点还没有导入提示词元素。")
    rendered = render(prompt_el["payload"].get("template") or "", _enrich(ctx.inlet)).strip()
    if not rendered:
        raise FlowError(f"提示词「{prompt_el['name']}」渲染后是空的。")
    provider = "template"
    message = f"已按模板渲染「{prompt_el['name']}」"
    text = rendered
    if (ctx.node.get("params") or {}).get("useModel"):
        profile = _model(ctx, "text")
        keys = _profile_keys(ctx, profile) if profile else []
        if profile and keys and (profile["payload"].get("model") or "").strip():
            pool = _key_pool(ctx, profile, keys)
            text = pool.call(lambda key: chat(profile, key, rendered))
            provider = "api"
            message = f"已调用 {profile['payload'].get('model')} 生成提示词" + pool.switch_message()
        else:
            message = "已勾选调用模型，但文本模型缺少密钥或模型名，这次仍按模板渲染"
    export = {
        "prompt": {
            "text": text,
            "purpose": prompt_el["payload"].get("purpose") or "other",
            "templateId": prompt_el["id"],
            "name": prompt_el["name"],
            "provider": provider,
        }
    }
    return NodeResult(export, provider, message)


def run_prompts_from_references(ctx: Ctx) -> NodeResult:
    params = ctx.node.get("params") or {}
    multi = ctx.node.get("type") == "prompt.from_multi_refs"
    try:
        count = int(params.get("count") or 3)
    except (TypeError, ValueError):
        count = 3
    count = max(1, min(12, count))
    purpose = params.get("purpose") or "image"
    images = [item for item in (ctx.by_type.get("reference_image") or []) if (item.get("status") or "启用") != "停用"]
    if multi:
        images = list({item["id"]: item for item in images}.values())
        if params.get("refGroup"):
            images = [item for item in images if item.get("group") == params["refGroup"]]
        if params.get("refTag"):
            images = [item for item in images if params["refTag"] in (item.get("tags") or [])]
        raw = params.get("referenceCount", 2)
        if isinstance(raw, bool) or not re.fullmatch(r"\d+", str(raw)) or not 1 <= int(raw) <= 12:
            raise FlowError("随机参考图数量 N 必须为 1 到 12 的整数。")
        reference_count = int(raw)
        if len(images) < reference_count:
            raise FlowError(
                f"当前筛选内已勾选且启用的参考图只有 {len(images)} 张，不够随机抽取 {reference_count} 张。"
                "请调整分组、标签或勾选更多参考图。"
            )
    articles = list(ctx.by_type.get("reference_article") or [])
    if not images and not articles:
        raise FlowError("请至少勾选一张参考图或一篇参考文章，可以多选。")
    instruction = ""
    guide = _choose_prompt(ctx.by_type.get("prompt") or [], purpose) or _choose_prompt(ctx.by_type.get("prompt") or [], "")
    if guide:
        instruction = render(guide["payload"].get("template") or "", _enrich(ctx.inlet)).strip()
    if multi:
        carried_images = random.sample(images, reference_count)
    else:
        chosen = random.choice(images) if images else None
        carried_images = [chosen] if chosen else []
    material = _reference_material(carried_images, articles)
    image_files = _reference_image_files(carried_images)
    theme = get_path(ctx.inlet, "theme.text") or ""
    profile = _model(ctx, "text")
    if profile is None:
        raise FlowError("参考生成提示词要调用生文模型。请在这个节点导入一个文本模型。")
    model_name = (profile.get("payload") or {}).get("model") or ""
    if not str(model_name).strip():
        raise FlowError("生文模型还没有填写模型名。")
    keys = _profile_keys(ctx, profile)
    if not keys:
        raise FlowError("生文模型还没有密钥。")
    workers = _worker_count(profile, count, keys, (ctx.execution_config or {}).get('promptConcurrency'))
    pool = _key_pool(ctx, profile, keys)

    def one_prompt(index: int) -> str:
        ask = _reference_prompt_request(index + 1, count, purpose, theme, material, instruction)
        if multi:
            ask += (
                f"\n\n本轮共同参考 {len(carried_images)} 张图片，附图顺序与参考图编号一致。"
                "请结合整组图片生成这一条提示词，具体参考方式遵循补充要求。"
            )
        try:
            content = pool.call(lambda key: chat(profile, key, ask, system=None, images=image_files), start_index=index)
        except FlowError as exc:
            raise FlowError(f"第 {index + 1}/{count} 条提示词生成失败：{exc}") from exc
        lines = _parse_prompt_lines(content, 1)
        if not lines:
            raise FlowError(f"第 {index + 1} 条生文没有返回提示词")
        return lines[0]

    texts = _parallel(count, workers, one_prompt)
    provider = "api"
    references_used = []
    for chosen in carried_images:
        use_count = int((chosen.get("payload") or {}).get("useCount") or chosen.get("useCount") or 0) + 1
        if ctx.mark_used:
            recorded = ctx.mark_used(chosen["id"])
            if isinstance(recorded, int):
                use_count = recorded
        used = {
            "id": chosen.get("id") or "",
            "name": chosen.get("name") or "",
            "note": (chosen.get("payload") or {}).get("note") or "",
            "group": chosen.get("group") or "",
            "tags": list(chosen.get("tags") or []),
            "path": str((chosen.get("payload") or {}).get("path") or ""),
            "useCount": use_count,
        }
        references_used.append(used)
    used = references_used[0] if references_used else None
    picked = used["name"] if used else "无"
    message = (
        f"已并发调用生文模型 {str(model_name).strip()}，"
        f"从 {len(images)} 张勾选参考图中随机带上《{picked}》。"
        f"生成 {len(texts)} 条提示词。并发 {workers}，密钥 {len(keys)} 条。"
    )
    if multi:
        picked = "、".join(f"《{item['name']}》" for item in references_used)
        message = (
            f"已并发调用生文模型 {str(model_name).strip()}，从当前筛选内 {len(images)} 张已勾选参考图中"
            f"随机抽取 {len(references_used)} 张，共同参考 {picked}。"
            f"生成 {len(texts)} 条提示词。并发 {workers}，密钥 {len(keys)} 条。"
        )
    if len(texts) < count:
        message += f" 希望 {count} 条，实际得到 {len(texts)} 条。"
    message += pool.switch_message()
    export = {
        "prompts": texts,
        "referenceUsed": used,
        "prompt": {
            "text": texts[0],
            "purpose": purpose,
            "name": f"参考提示词 1/{len(texts)}",
            "provider": provider,
        },
    }
    if multi:
        export.update(
            referencesUsed=references_used,
            referenceNames="、".join(item["name"] for item in references_used),
            referenceNotes="；".join(item["note"] for item in references_used if item["note"]),
        )
    return NodeResult(export, provider, message)


def _prompt_texts(value) -> list[str]:
    """Accept a prompt array, or the older object that kept the array in items."""
    if isinstance(value, list):
        raw = value
    elif isinstance(value, dict):
        raw = value.get("items") or []
    else:
        return []
    texts = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            texts.append(item.strip())
        elif isinstance(item, dict) and str(item.get("text") or "").strip():
            texts.append(str(item["text"]).strip())
    return texts


def _reference_image_files(images: list) -> list[str]:
    paths = []
    missing = []
    for element in images:
        raw = str((element.get("payload") or {}).get("path") or "").strip()
        name = element.get("name") or raw or "未命名参考图"
        if not raw:
            missing.append(f"{name}（没有路径）")
            continue
        try:
            resolved = resolve_path(raw)
        except ValueError as exc:
            missing.append(f"{name}（{exc}）")
            continue
        if not resolved.is_file():
            missing.append(f"{name}（{resolved}）")
            continue
        paths.append(str(resolved))
    if missing:
        raise FlowError("参考图文件找不到，无法发给模型：" + "；".join(missing))
    return paths


def _reference_material(images: list, articles: list) -> str:
    lines = []
    for index, element in enumerate(images, 1):
        payload = element.get("payload") or {}
        lines.append(
            f"参考图{index}《{element.get('name') or ''}》角色：{payload.get('role') or '未标'}。说明：{payload.get('note') or '无'}。路径：{payload.get('path') or ''}"
        )
    for index, element in enumerate(articles, 1):
        payload = element.get("payload") or {}
        body = str(payload.get("body") or "").strip()
        if len(body) > 280:
            body = body[:280] + "…"
        lines.append(f"参考文章{index}《{payload.get('title') or element.get('name') or ''}》用途：{payload.get('usage') or ''}。正文：{body}")
    return "\n".join(lines)


def _reference_prompt_request(index: int, count: int, purpose: str, theme: str, material: str, instruction: str) -> str:
    purpose_label = {"image": "生图", "title": "标题", "article": "写文章"}.get(purpose, "写作")
    parts = [
        f"请根据下面的参考图和参考文章，只写 1 条{purpose_label}提示词。这是第 {index} 条，共 {count} 条，必须和其他条不同。",
        "只输出这一条提示词，不要编号，不要解释。",
    ]
    if theme:
        parts.append(f"主题：{theme}")
    if instruction:
        parts.append("补充要求：\n" + instruction)
    parts.append("参考材料：\n" + material)
    return "\n\n".join(parts)


def _parse_prompt_lines(content: str, count: int) -> list[str]:
    lines = []
    for line in (content or "").splitlines():
        cleaned = re.sub(r"^\s*(?:[-*•]+|\d+\s*[.、)]\s*)", "", line).strip()
        if cleaned:
            lines.append(cleaned)
        if len(lines) >= count:
            break
    return lines


def run_title(ctx: Ctx) -> NodeResult:
    return _run_text(ctx, "title")


def run_article(ctx: Ctx) -> NodeResult:
    return _run_text(ctx, "article")


def run_image(ctx: Ctx) -> NodeResult:
    size = image_size_config(ctx.node, ctx.flow)
    width, height = size["width"], size["height"]
    view = _enrich(ctx.inlet)
    view["imageSize"] = size
    excerpt = str(view.get("articleExcerpt") or "").replace("\n", " ").strip()
    if len(excerpt) > 180:
        excerpt = excerpt[:180] + "…"
    view["articleExcerpt"] = excerpt
    group = _first(ctx.by_type.get("image_config_group"))
    carried_prompts = _prompt_texts(ctx.inlet.get("prompts"))[:MAX_IMAGES]
    variants = _variants(group["payload"]) if group else []
    negative = "；".join(item.get("negative") or "" for item in variants if item.get("negative"))
    jobs = []
    if carried_prompts:
        profile = _model(ctx, "image")
        if profile is None:
            raise FlowError("生图节点收到了上游提示词，但还没有导入生图模型。")
        model_name = str((profile.get("payload") or {}).get("model") or "").strip()
        if not model_name:
            raise FlowError("生图模型还没有填写模型名。")
        keys = _profile_keys(ctx, profile)
        if not keys:
            raise FlowError("生图模型还没有密钥。")
        source = "上游提示词数组"
        for text in carried_prompts:
            jobs.append((text, {"name": "", "width": width, "height": height}))
        prompt_text = carried_prompts[0]
    else:
        instruction, source = _instruction(ctx, "image", view)
        if not instruction:
            instruction = (
                f"手机竖版壁纸。主题：{get_path(view, 'theme.text') or ''}。"
                f"标题：{view.get('primaryTitle') or ''}。画面：{excerpt}。"
            )
            source = "节点内置说明"
        if group is None:
            raise FlowError("没有上游提示词数组时，生图节点需要导入生图配置组。")
        view["style"] = (variants[0].get("style") or "") if variants else ""
        prompt_text = render(instruction, view).strip()
        if negative:
            prompt_text += f"\n不要出现：{negative}"
        for variant in variants:
            count = max(0, int(variant.get("count") or 0))
            for _ in range(count):
                if len(jobs) >= MAX_IMAGES:
                    break
                jobs.append((prompt_text, variant))
            if len(jobs) >= MAX_IMAGES:
                break
    if not jobs:
        raise FlowError("生图配置组的张数是 0。")
    if not carried_prompts:
        profile = _model(ctx, "image")
        keys = _profile_keys(ctx, profile) if profile else []
    use_api = bool(carried_prompts) or bool(profile and keys and str((profile.get("payload") or {}).get("model") or "").strip())
    image_dir = ctx.scratch / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    workers = _worker_count(profile, len(jobs), keys, (ctx.execution_config or {}).get('imageConcurrency')) if use_api else 1
    pool = _key_pool(ctx, profile, keys) if use_api else None

    def one_image(offset: int) -> dict:
        base_text, variant = jobs[offset]
        index = offset + 1
        variant_prompt = base_text
        if not carried_prompts and variant.get("style"):
            variant_prompt += f"\n这一张的风格：{variant['style']}"
        variant_prompt = canvas_prompt(variant_prompt, size)
        dest = image_dir / f"{index:02d}.png"
        if use_api:
            try:
                blob, sent = pool.call(
                    lambda key: generate_image(profile, key, variant_prompt, width, height), start_index=offset,
                )
                blob, dimensions = normalize_canvas(blob, width, height)
            except FlowError as exc:
                raise FlowError(f"第 {index}/{len(jobs)} 张图片生成失败：{exc}") from exc
            dest.write_bytes(blob)
            provider_name = "api"
            sent_size = sent
        else:
            fit_w, fit_h = fit_size(width, height)
            seed = f"{base_text}{view.get('primaryTitle') or ''}{get_path(view, 'theme.text') or ''}{index}"
            write_gradient(dest, fit_w, fit_h, seed)
            blob, dimensions = normalize_canvas(dest.read_bytes(), width, height)
            dest.write_bytes(blob)
            provider_name = "local-draft"
            sent_size = "local"
        return {
            "path": str(dest),
            "prompt": variant_prompt,
            "provider": provider_name,
            "configGroupId": group["id"] if group else "",
            "variant": variant.get("name") or "",
            "intendedSize": [width, height],
            "sentSize": sent_size,
            **dimensions,
        }

    items = _parallel(len(jobs), workers, one_image)
    provider = items[0]["provider"]
    if carried_prompts:
        how = f"按提示词数组 {len(items)} 条各调用一次生图模型，并发 {workers}，密钥 {len(keys)} 条"
    else:
        how = f"生成 {len(items)} 张图片"
    if provider == "api":
        message = f"已调用生图模型 {profile['payload'].get('model')}，{how}。" + pool.switch_message()
    else:
        message = f"本地草稿图 {len(items)} 张，未调用生图模型。{how}，提示词来自{source}。"
    adjusted = sum(item["sizeAdjusted"] for item in items) if use_api else 0
    message += f" 统一尺寸 {width}×{height}（{size['ratio']}）。"
    if adjusted:
        message += f" 已将 {adjusted} 张接口返回尺寸不符的图片等比缩放并补边。"
    images_export = {"items": items, "provider": provider, "promptUsed": items[0]["prompt"], "size": size}
    used = ctx.inlet.get("referenceUsed")
    if isinstance(used, dict):
        images_export["referenceUsed"] = used
    if isinstance(ctx.inlet.get("referencesUsed"), list):
        images_export["referencesUsed"] = copy.deepcopy(ctx.inlet["referencesUsed"])
    return NodeResult({"images": images_export, "imageSize": size}, provider, message)


def run_rules(ctx: Ctx) -> NodeResult:
    document = copy.deepcopy(ctx.inlet)
    applied = 0
    for element in ctx.by_type.get("rule") or []:
        document, blocked = apply_rule(document, element.get("payload") or {})
        applied += 1
        if blocked:
            raise FlowError(blocked)
    if applied == 0:
        return NodeResult(document, "rules", "没有绑定规则，字段原样通过")
    return NodeResult(document, "rules", f"已执行 {applied} 条规则")


def _unique_zip(root: Path, base: str, run_id: str) -> Path:
    plain = root / f"{base}.zip"
    if not plain.exists():
        return plain
    candidate = root / f"{base}-{run_id}.zip"
    if not candidate.exists():
        return candidate
    index = 2
    while (root / f"{base}-{run_id}-{index}.zip").exists():
        index += 1
    return root / f"{base}-{run_id}-{index}.zip"


def run_work(ctx: Ctx) -> NodeResult:
    params = ctx.node.get("params") or {}
    images = ctx.inlet.get("images") or {}
    items = images.get("items") if isinstance(images, dict) else images
    if not isinstance(items, list) or not items:
        raise FlowError("生成作品没有收到图片集合。请将上游的 images 携带到这个节点。")
    sources = []
    for index, image in enumerate(items, 1):
        raw = image.get("path") if isinstance(image, dict) else image
        if not isinstance(raw, str) or not raw.strip():
            raise FlowError(f"第 {index} 张图片缺少文件路径，无法生成作品。")
        source = resolve_path(raw)
        if not source.is_file():
            raise FlowError(f"第 {index} 张图片文件不存在：{raw}")
        sources.append(source)
    now = datetime.now()
    context = copy.deepcopy(ctx.inlet)
    ending_index = int(sha256(ctx.run_id.encode("utf-8")).hexdigest()[:8], 16) % len(WORK_TITLE_ENDINGS)
    context.update(date=now.strftime("%Y%m%d"), flowName=ctx.flow.get("name") or "flow",
                   ending=WORK_TITLE_ENDINGS[ending_index])
    raw_title = render(str(params.get("nameTemplate") or "${title}"), context, strict=True).strip()
    if not raw_title:
        raise FlowError("作品名称为空。请检查携带参数，或改用参考图名称作为 title。")
    title = _work_name(raw_title)
    work_date = now.strftime("%Y-%m-%d")
    collection = resolve_path(str(params.get("directory") or "outputs/works")) / work_date
    category = str(params.get("subdirectory") or "").strip().replace("\\", "/")
    subdirectory = Path(category)
    if subdirectory.is_absolute() or subdirectory.drive or ".." in subdirectory.parts or re.search(r'[<>:"|?*\x00-\x1f]', category):
        raise FlowError("分类子目录必须是相对路径，不能包含 .. 或文件名非法字符。")
    root = collection / subdirectory
    if not root.resolve().is_relative_to(collection.resolve()):
        raise FlowError("分类子目录不能越出日期文件夹。")
    root.mkdir(parents=True, exist_ok=True)
    index = 1
    while True:
        folder_name = title if index == 1 else f"{title}-{index}"
        folder = root / folder_name
        zip_path = root / (folder_name + ".zip")
        if zip_path.exists():
            index += 1
            continue
        try:
            folder.mkdir()
            break
        except FileExistsError:
            index += 1
    created_zip = False
    files = []
    try:
        for index, source in enumerate(sources, 1):
            name = f"{index:02d}{source.suffix.lower() or '.png'}"
            shutil.copy2(source, folder / name)
            files.append(name)
        with zipfile.ZipFile(zip_path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            created_zip = True
            for name in files:
                archive.write(folder / name, f"{folder_name}/{name}")
    except Exception:
        shutil.rmtree(folder)
        if created_zip:
            zip_path.unlink(missing_ok=True)
        raise
    return NodeResult(
        {"output": {"title": folder_name, "folder": str(folder), "zip": str(zip_path), "files": files,
                    "collection": str(collection), "date": work_date}},
        "files", f"已生成作品「{folder_name}」，共 {len(files)} 张图片，已归档至 {work_date} 日期文件夹并打包 ZIP。",
    )


def _work_name(value: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        raise FlowError("作品名称中没有可用于文件夹命名的文字。")
    name = name[:100].rstrip(" .")
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name):
        name = "作品-" + name
    return name


def run_result_direct(ctx: Ctx) -> NodeResult:
    params = ctx.node.get("params") or {}
    group = str(params.get("group") or "").strip()
    if not group:
        raise FlowError("请先选择结果库分组。")
    try:
        count = int(params.get("count") or 1)
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(50, count))
    if not ctx.list_results:
        raise FlowError("结果库不可用。")
    available = ctx.list_results(1000, group, True)
    if len(available) < count:
        raise FlowError(f"分组「{group}」里未出作品的图片只有 {len(available)} 张，不够 {count} 张。")
    picked = random.sample(available, count)
    directory = str(params.get("directory") or "outputs/result-direct")
    root = resolve_path(directory)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    zip_name = str(params.get("zipName") or "结果直出-{{date}}")
    zip_name = zip_name.replace("{{date}}", stamp).replace("{{flowName}}", str(ctx.flow.get("name") or "flow"))
    zip_path = _unique_zip(root, _safe(zip_name, "结果直出"), ctx.run_id)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, item in enumerate(picked, 1):
            source = resolve_path(str(item.get("path") or ""))
            if not source.is_file():
                raise FlowError(f"结果图文件不存在：{item.get('path') or ''}")
            suffix = source.suffix.lower() or ".png"
            archive.write(source, f"images/{index:02d}{suffix}")
    if ctx.mark_results_worked:
        ctx.mark_results_worked([item["id"] for item in picked])
    message = f"已从分组「{group}」随机抽出 {count} 张未出作品的图片，并打成 ZIP。"
    return NodeResult(
        {
            "images": {
                "items": [
                    {"path": str(resolve_path(item["path"])), "prompt": item.get("prompt") or "", "provider": "result-library"}
                    for item in picked
                ],
                "provider": "result-library",
            },
            "output": {"folder": str(root), "zip": str(zip_path), "files": [zip_path.name], "results": picked},
        },
        "files",
        message,
    )


def run_output(ctx: Ctx) -> NodeResult:
    target = _first(ctx.by_type.get("output_target"))
    payload = dict((target or {}).get("payload") or {})
    params = ctx.node.get("params") or {}
    directory = str(params.get("directory") or payload.get("directory") or "outputs/default")
    save_as = str(params.get("saveAs") or "")
    write_folder = payload.get("writeFolder", True) if params.get("writeFolder") is None else bool(params.get("writeFolder"))
    write_zip = payload.get("writeZip", False) if params.get("writeZip") is None else bool(params.get("writeZip"))
    write_library = False
    if save_as == "zip":
        write_zip = True
        write_folder = True
    elif save_as == "library":
        write_zip = False
        write_library = True
        write_folder = True
    elif save_as == "both":
        write_zip = True
        write_library = True
        write_folder = True
    if not write_folder and not write_zip and not write_library:
        raise FlowError("输出目标至少要打开文件夹或 ZIP。")
    root = resolve_path(directory)
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / _safe(ctx.run_id, "run")
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    files: list[str] = []
    inlet = ctx.inlet or {}
    titles = ((inlet.get("titles") or {}).get("items")) or []
    articles = ((inlet.get("articles") or {}).get("items")) or []
    images = ((inlet.get("images") or {}).get("items")) or []
    if titles:
        lines = []
        if (inlet.get("titles") or {}).get("provider") == "local-draft":
            lines.append("# 本地草稿：未调用模型")
        for index, item in enumerate(titles, 1):
            lines.append(f"{index}. {item.get('text') or ''}")
        _write(run_dir / "titles.txt", "\n".join(lines) + "\n")
        files.append("titles.txt")
    prompt_bits = []
    if (inlet.get("titles") or {}).get("promptUsed"):
        prompt_bits.append(("title.txt", inlet["titles"]["promptUsed"]))
    if (inlet.get("articles") or {}).get("promptUsed"):
        prompt_bits.append(("article.txt", inlet["articles"]["promptUsed"]))
    if (inlet.get("images") or {}).get("promptUsed"):
        prompt_bits.append(("image.txt", inlet["images"]["promptUsed"]))
    for index, text in enumerate(_prompt_texts(inlet.get("prompts")), 1):
        prompt_bits.append((f"from-ref-{index:02d}.txt", text))
    if prompt_bits:
        (run_dir / "prompts").mkdir(exist_ok=True)
        for name, text in prompt_bits:
            _write(run_dir / "prompts" / name, text + "\n")
            files.append(f"prompts/{name}")
    for index, article in enumerate(articles, 1):
        title = article.get("title") or f"文章{index}"
        name = f"{index:02d}-{_safe(title, 'article')}.md"
        body = article.get("body") or ""
        _write(run_dir / name, f"# {title}\n\n{body}\n")
        files.append(name)
    copied_refs = set()
    for index, image in enumerate(images, 1):
        source = Path(str(image.get("path") or ""))
        if not source.is_file():
            continue
        suffix = source.suffix.lower() if source.suffix else ".png"
        relative = f"images/{index:02d}{suffix}"
        dest = run_dir / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        files.append(relative)
        for ref in image.get("referenceImages") or []:
            ref_path = str(ref.get("path") or "")
            if not ref_path or ref_path in copied_refs:
                continue
            try:
                src = resolve_path(ref_path)
            except ValueError:
                continue
            if not src.is_file():
                continue
            copied_refs.add(ref_path)
            ref_name = f"references/{_safe(ref.get('name') or src.stem, 'ref')}{src.suffix.lower()}"
            target = run_dir / ref_name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            files.append(ref_name)
    warnings = []
    if not articles:
        warnings.append("这次没有文章进入输出节点")
    if not images:
        warnings.append("这次没有图片进入输出节点")
    manifest = {
        "flowId": ctx.flow.get("id"),
        "flowName": ctx.flow.get("name"),
        "runId": ctx.run_id,
        "theme": get_path(inlet, "theme.text") or "",
        "files": files,
        "warnings": warnings,
        "titles": [item.get("text") for item in titles],
        "imageProviders": sorted({item.get("provider") for item in images}),
    }
    _write(run_dir / "manifest.json", json_text(manifest))
    files.append("manifest.json")
    library_items = _save_result_library(ctx, inlet, images, run_dir) if write_library else []
    zip_path = ""
    if write_zip:
        stamp = datetime.now().strftime("%Y%m%d")
        ctx_for_name = copy.deepcopy(inlet)
        ctx_for_name["date"] = stamp
        ctx_for_name["flowName"] = ctx.flow.get("name") or "flow"
        raw_name = render((payload.get("zipName") or "{{flowName}}-{{date}}"), ctx_for_name)
        zip_file = root / f"{_safe(raw_name, 'output')}.zip"
        if zip_file.exists():
            zip_file = root / f"{_safe(raw_name, 'output')}-{ctx.run_id[:6]}.zip"
        with zipfile.ZipFile(zip_file, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file_path in run_dir.rglob("*"):
                if file_path.is_file():
                    archive.write(file_path, file_path.relative_to(run_dir).as_posix())
        zip_path = str(zip_file)
    if not write_folder:
        shutil.rmtree(run_dir, ignore_errors=True)
        folder = ""
    else:
        folder = str(run_dir)
    bits = []
    if folder:
        bits.append("已写入文件夹")
    if zip_path:
        bits.append("已打包 ZIP")
    if library_items:
        bits.append(f"已保存 {len(library_items)} 张到结果库")
    if warnings:
        bits.append("；".join(warnings))
    export = {"output": {"folder": folder, "zip": zip_path, "files": files, "warnings": warnings, "results": library_items}}
    return NodeResult(export, "files", "。".join(bits) or "已输出")


def _save_result_library(ctx: Ctx, inlet: dict, images: list, run_dir: Path) -> list[dict]:
    if not ctx.save_result:
        return []
    image_block = inlet.get("images") if isinstance(inlet.get("images"), dict) else {}
    used = image_block.get("referenceUsed") if isinstance(image_block.get("referenceUsed"), dict) else {}
    group = used.get("group") or ""
    tags = used.get("tags") if isinstance(used.get("tags"), list) else []
    saved = []
    for index, image in enumerate(images, 1):
        source = Path(str(image.get("path") or ""))
        suffix = source.suffix.lower() if source.suffix else ".png"
        copied = run_dir / f"images/{index:02d}{suffix}"
        if not copied.is_file():
            continue
        result_id = new_id("result")
        relative = f"results/{result_id}{suffix}"
        dest = resolve_path(relative)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(copied, dest)
        record = ctx.save_result(
            {
                "id": result_id,
                "flowId": ctx.flow.get("id") or "",
                "flowName": ctx.flow.get("name") or "",
                "runId": ctx.run_id,
                "name": f"{used.get('name') or '结果图'}-{index:02d}",
                "group": group,
                "tags": tags,
                "referenceId": used.get("id") or "",
                "referenceName": used.get("name") or "",
                "referenceGroup": group,
                "referenceTags": tags,
                "path": relative,
                "prompt": image.get("prompt") or "",
            }
        )
        saved.append(record)
    return saved


def _run_text(ctx: Ctx, role: str) -> NodeResult:
    view = _enrich(ctx.inlet)
    purpose = "title" if role == "title" else "article"
    instruction, source = _instruction(ctx, purpose, view)
    if not instruction:
        if role == "title":
            instruction = f"请为主题「{get_path(view, 'theme.text') or ''}」写 3 个公众号标题，每行一个，不要编号。"
        else:
            instruction = (
                f"请写一篇 400 到 700 字的公众号文章，分 3 到 4 段。"
                f"主题：{get_path(view, 'theme.text') or ''}。标题：{view.get('primaryTitle') or ''}。"
            )
        source = "节点内置说明"
    context_mode = (ctx.node.get("params") or {}).get("contextMode") or "auto"
    user_text = instruction
    if context_mode != "template-only":
        user_text = instruction.rstrip() + "\n\n" + _material_block(ctx, view)
    profile = _model(ctx, "text")
    keys = _profile_keys(ctx, profile) if profile else []
    if profile and keys and (profile["payload"].get("model") or "").strip():
        pool = _key_pool(ctx, profile, keys)
        content = pool.call(lambda key: chat(profile, key, user_text))
        provider = "api"
        message = f"已调用 {profile['payload'].get('model')}。提示词来自{source}。" + pool.switch_message()
    else:
        content = _local_title_text(view, ctx) if role == "title" else _local_article_text(view, ctx)
        provider = "local-draft"
        message = f"本地草稿，未调用模型。提示词来自{source}。配置文本模型的密钥和模型名后，同一节点会改走接口。"
    if role == "title":
        titles = _parse_titles(content)
        if not titles:
            raise FlowError("没有得到标题")
        export = {"titles": {"items": [{"text": item} for item in titles], "provider": provider, "promptUsed": user_text}}
        return NodeResult(export, provider, message)
    title = str(view.get("primaryTitle") or "").strip()
    if not title:
        items = ((view.get("titles") or {}).get("items")) or []
        title = (items[0].get("text") if items else "") or "未命名"
    export = {
        "articles": {
            "items": [{"title": title, "body": content.strip(), "provider": provider}],
            "provider": provider,
            "promptUsed": user_text,
        }
    }
    return NodeResult(export, provider, message)


def _instruction(ctx: Ctx, purpose: str, view: dict) -> tuple[str, str]:
    carried = ctx.inlet.get("prompt") if isinstance(ctx.inlet.get("prompt"), dict) else None
    if carried and str(carried.get("text") or "").strip():
        return render(str(carried["text"]), view).strip(), "入口携带"
    element = _choose_prompt(ctx.by_type.get("prompt") or [], purpose)
    if element is None:
        return "", ""
    return render(element["payload"].get("template") or "", view).strip(), f"元素「{element['name']}」"


def _material_block(ctx: Ctx, view: dict) -> str:
    lines = ["【携带与导入的材料】"]
    theme = view.get("theme") or {}
    if theme.get("text"):
        lines.append(f"主题：{theme.get('text')}")
    domain = theme.get("domain") or {}
    if domain.get("name"):
        lines.append(f"领域：{domain.get('name')}；语气：{domain.get('tone') or ''}；读者：{domain.get('audience') or ''}")
    if view.get("primaryTitle"):
        lines.append(f"主标题：{view.get('primaryTitle')}")
    titles = ((view.get("titles") or {}).get("items")) or []
    if titles:
        lines.append("标题：")
        for index, item in enumerate(titles, 1):
            lines.append(f"{index}. {item.get('text') or ''}")
    for element in ctx.by_type.get("reference_article") or []:
        payload = element["payload"]
        lines.append(f"参考文章（{payload.get('usage') or '综合'}）{payload.get('title') or element['name']}")
        lines.append(payload.get("body") or "")
    for element in ctx.by_type.get("reference_copy") or []:
        payload = element["payload"]
        lines.append(f"参考文案（{payload.get('usage') or ''}）：{payload.get('text') or ''}")
    for element in ctx.by_type.get("domain") or []:
        payload = element["payload"]
        lines.append(f"导入领域：{element['name']}；语气：{payload.get('tone') or ''}")
    return "\n".join(lines)


def _local_title_text(view: dict, ctx: Ctx) -> str:
    theme = get_path(view, "theme.text") or "今天的主题"
    tone = (get_path(view, "theme.domain.tone") or "清楚").split("、")[0]
    copies = ctx.by_type.get("reference_copy") or []
    hint = ""
    if copies:
        hint = str(copies[0]["payload"].get("text") or "").strip()
    third = f"{theme}｜{tone}的这一张"
    if hint:
        third = f"{theme}｜沿用参考文案的短句口气"
    return "\n".join(
        [
            f"{theme}：适合留在锁屏上的画面",
            f"把{theme}写成一组可以照着找的壁纸",
            third,
        ]
    )


def _local_article_text(view: dict, ctx: Ctx) -> str:
    theme = get_path(view, "theme.text") or "这个主题"
    title = view.get("primaryTitle") or theme
    tone = get_path(view, "theme.domain.tone") or "清楚、具体"
    audience = get_path(view, "theme.domain.audience") or "公众号读者"
    return (
        "【本地草稿】还没有为文本模型填写密钥。下面按主题、标题和领域语气排了结构，用来先把流程跑通。"
        "填好模型后，同一条流程会改走接口，这段不会再出现。\n\n"
        f"{title}\n\n"
        f"{theme}适合放在手机锁屏的上半屏。画面不要塞满，留一条能放下时间的空。\n\n"
        "第一眼先看见光。窗边或灯下只留一条暖边，其余地方收暗，壁纸缩得很小的时候层次还在。\n\n"
        "中间放一个静止的物件，例如杯沿、书页或窗帘边。物件只占一小角，用来给定视线。\n\n"
        f"句子保持短。领域语气是{tone}，读者是{audience}。不写下载地址，也不把参考文章整段搬进来。\n\n"
        f"如果这张图配的是「{title}」，正文就停在画面本身。"
    )


def _parse_titles(content: str) -> list[str]:
    titles = []
    for line in (content or "").splitlines():
        cleaned = re.sub(r"^\s*(?:[-*•]+|\d+\s*[.、)] )\s*", "", line).strip()
        cleaned = cleaned.strip("「」\"'")
        if cleaned and not cleaned.startswith("#") and not cleaned.startswith("【本地草稿】"):
            titles.append(cleaned)
    if not titles and content.strip():
        titles.append(content.strip())
    return titles[:8]


def _enrich(inlet: dict) -> dict:
    view = copy.deepcopy(inlet or {})
    if not view.get("primaryTitle"):
        items = ((view.get("titles") or {}).get("items")) or []
        if items:
            view["primaryTitle"] = items[0].get("text") or ""
    if not view.get("articleExcerpt"):
        articles = ((view.get("articles") or {}).get("items")) or []
        if articles:
            view["articleExcerpt"] = (articles[0].get("body") or "")[:180]
    return view


def _choose_prompt(elements: list, purpose: str):
    if purpose:
        for element in elements:
            if (element.get("payload") or {}).get("purpose") == purpose:
                return element
    return elements[0] if elements else None


def _model(ctx: Ctx, kind: str):
    matches = [
        element
        for element in ctx.by_type.get("model_profile") or []
        if (element.get("payload") or {}).get("kind") == kind
    ]
    return matches[0] if matches else None


def _variants(payload: dict) -> list[dict]:
    variants = payload.get("variants")
    if isinstance(variants, list) and variants:
        return [item for item in variants if isinstance(item, dict)]
    return [payload]


def _first(items):
    return items[0] if items else None


def _safe(name: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\r\n\t]', " ", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    return (cleaned or fallback)[:40]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def json_text(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
