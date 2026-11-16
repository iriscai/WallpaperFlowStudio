"""Field paths, templates, and rules. These are the pass-through operations on a flow."""

from __future__ import annotations

import copy
import json
import re

_FIELD = r"[\w.]+"
_TOKEN = re.compile(r"\{\{\s*(" + _FIELD + r")\s*\}\}|\$\{\s*(" + _FIELD + r")\s*\}")


class FlowError(Exception):
    pass


def get_path(doc, path: str):
    if path is None or path == "":
        return None
    current = doc
    for part in str(path).split("."):
        if current is None:
            return None
        if isinstance(current, list):
            if not part.isdigit():
                return None
            index = int(part)
            if index < 0 or index >= len(current):
                return None
            current = current[index]
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def set_path(doc: dict, path: str, value) -> None:
    parts = [part for part in str(path).split(".") if part]
    if not parts:
        raise FlowError("字段路径是空的")
    current = doc
    for part in parts[:-1]:
        if isinstance(current, dict):
            nxt = current.get(part)
            if not isinstance(nxt, (dict, list)):
                nxt = {}
                current[part] = nxt
            current = nxt
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if index < 0 or index >= len(current):
                raise FlowError(f"字段不存在：{path}")
            current = current[index]
            continue
        raise FlowError(f"写不进字段：{path}")
    last = parts[-1]
    if isinstance(current, dict):
        current[last] = value
        return
    if isinstance(current, list) and last.isdigit():
        index = int(last)
        if 0 <= index < len(current):
            current[index] = value
            return
    raise FlowError(f"写不进字段：{path}")


def drop_path(doc, path: str) -> None:
    parts = [part for part in str(path).split(".") if part]
    if not parts:
        return
    parent = doc
    for part in parts[:-1]:
        parent = _step(parent, part)
        if parent is None:
            return
    last = parts[-1]
    if isinstance(parent, dict):
        parent.pop(last, None)
    elif isinstance(parent, list) and last.isdigit():
        index = int(last)
        if 0 <= index < len(parent):
            parent.pop(index)


def _step(current, part: str):
    if isinstance(current, dict):
        return current.get(part)
    if isinstance(current, list) and part.isdigit():
        index = int(part)
        if 0 <= index < len(current):
            return current[index]
    return None


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def field_path(value: str) -> str:
    value = str(value or "").strip()
    match = _TOKEN.fullmatch(value)
    return (match.group(1) or match.group(2)) if match else value


def render(template: str, context, *, strict: bool = False) -> str:
    def replace(match):
        path = match.group(1) or match.group(2)
        value = get_path(context, path)
        if strict and value is None:
            raise FlowError(f"参数 {path} 未携带。请在上游节点配置携带参数，再连接到生成作品节点。")
        return _text(value)

    return _TOKEN.sub(replace, template or "")


def truthy(value) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def apply_rule(doc: dict, payload: dict) -> tuple[dict, str | None]:
    steps = (payload or {}).get("steps") or []
    for step in steps:
        if not isinstance(step, dict):
            continue
        op = step.get("op") or ""
        field = (step.get("field") or "").strip()
        if op == "require":
            if not field or not truthy(get_path(doc, field)):
                return doc, step.get("message") or f"{field or '字段'} 是空的，规则中止了流程"
        elif op == "block":
            return doc, step.get("message") or "规则中止了流程"
        elif op == "set":
            if not field:
                raise FlowError("设置字段时要填写字段名")
            if (step.get("from") or "").strip():
                set_path(doc, field, copy.deepcopy(get_path(doc, step["from"].strip())))
            else:
                set_path(doc, field, step.get("value"))
        elif op == "template":
            if not field:
                raise FlowError("模板规则要填写写入的字段名")
            set_path(doc, field, render(step.get("template") or "", doc))
        elif op == "drop":
            if field:
                drop_path(doc, field)
        elif op == "append_note":
            notes = get_path(doc, "notes")
            if not isinstance(notes, list):
                notes = []
            note = step.get("value")
            if not note:
                note = render(step.get("template") or "", doc)
            notes.append(note or "")
            set_path(doc, "notes", notes)
        else:
            raise FlowError(f"不认识的规则动作：{op}")
    return doc, None


def apply_carry(upstream: dict, mappings: list, rules: list[dict], parameters: dict | None = None) -> dict:
    """Carry named parameters, apply field mappings, then run the edge's rules."""

    context = copy.deepcopy(upstream or {})
    fragment: dict = copy.deepcopy(parameters or {})
    for mapping in mappings or []:
        if not isinstance(mapping, dict):
            continue
        op = mapping.get("op") or "copy"
        target = field_path(mapping.get("target"))
        if not target:
            continue
        if op == "template":
            rendered = render(mapping.get("template") or "", context)
            set_path(fragment, target, rendered)
            if "." not in target:
                context[target] = rendered
            continue
        source = field_path(mapping.get("source"))
        if not source:
            continue
        value = get_path(context, source)
        if value is None:
            continue
        set_path(fragment, target, copy.deepcopy(value))
        if "." not in target:
            context[target] = get_path(fragment, target)
    for rule in rules:
        fragment, blocked = apply_rule(fragment, rule or {})
        if blocked:
            raise FlowError(blocked)
    return fragment


def merge_docs(base: dict, incoming: dict) -> dict:
    """Later edges replace scalars and lists. Dicts merge one level deeper."""

    for key, value in (incoming or {}).items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            merge_docs(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base
