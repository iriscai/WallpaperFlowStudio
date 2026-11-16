"""Editable spreadsheet transfers for every managed library."""

from __future__ import annotations

import copy
import csv
import io
import json
import shutil
import uuid
from pathlib import Path

from app.catalog import BLANK_SCHEDULE, ELEMENT_DEFAULTS, ELEMENT_TYPES, blank_node, blank_payload
from app.ids import new_id
from app.id_migration import rewrite_document, rewrite_trace
from app.paths import resolve_path
from app.schedule import normalize_schedule
from app.store import now_iso


LABELS = {"summary": "简介", "audience": "读者", "tone": "语气", "tags": "内容标签", "text": "文本", "notes": "备注", "path": "路径", "role": "角色", "note": "说明", "title": "标题", "body": "正文", "usage": "用途", "purpose": "用途", "template": "模板", "variants": "生图配置", "kind": "种类", "baseUrl": "接口地址", "model": "模型名", "temperature": "温度", "maxTokens": "最大令牌数", "concurrency": "并发数", "directory": "目录", "writeFolder": "写入文件夹", "writeZip": "写入ZIP", "zipName": "ZIP名称", "steps": "规则步骤"}
COMMON = [("id", "id"), ("name", "名称"), ("group", "分组"), ("tags", "标签"), ("status", "状态"), ("fields", "字段")]
RESULT_COLUMNS = [("id", "id"), ("name", "名称"), ("group", "分组"), ("tags", "标签"), ("worked", "状态"), ("prompt", "提示词"), ("path", "路径"), ("flowId", "流程编号"), ("flowName", "流程名称"), ("runId", "运行编号"), ("referenceId", "参考图编号"), ("referenceName", "参考图"), ("referenceGroup", "参考图分组"), ("referenceTags", "参考图标签"), ("createdAt", "创建时间")]
RUN_COLUMNS = [("id", "id"), ("flowId", "流程"), ("startedAt", "时间"), ("finishedAt", "结束时间"), ("mode", "方式"), ("status", "状态"), ("error", "错误"), ("trace", "执行详情")]
NAMES = {"elements": "元素库", "flows": "流程", "results": "结果库", "runs": "运行记录"}


def columns_for(kind: str, element_type: str = "") -> list[tuple[str, str]]:
    if kind == "elements":
        if element_type not in ELEMENT_DEFAULTS:
            raise ValueError("请选择元素类型")
        columns = COMMON + [("type", "类型")] + [("payload." + key, LABELS.get(key, key)) for key in ELEMENT_DEFAULTS[element_type]]
        if element_type == "reference_image":
            columns.append(("payload.useCount", "使用次数"))
        return columns
    if kind == "flows":
        return COMMON + [("description", "说明"), ("graph", "节点与连线"), ("schedule", "定时配置")]
    if kind == "results":
        return RESULT_COLUMNS
    if kind == "runs":
        return RUN_COLUMNS
    raise ValueError("不认识的表格类型")


def records_for(store, kind: str) -> list[dict]:
    if kind == "elements":
        return store.list_elements()
    if kind == "flows":
        return store.list_flows()
    if kind == "results":
        return store.list_results(0)
    if kind == "runs":
        return store.list_runs(limit=0)
    raise ValueError("不认识的表格类型")


def export_table(store, body: dict) -> tuple[bytes, str, str]:
    kind = body.get("kind") or ""
    element_type = body.get("type") or ""
    columns = columns_for(kind, element_type)
    records = records_for(store, kind)
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise ValueError("请指定要导出的记录")
    ids = [store.resolve_id("element" if kind == "elements" else kind.rstrip("s"), value) for value in ids]
    by_id = {record["id"]: record for record in records}
    records = [by_id[record_id] for record_id in dict.fromkeys(ids) if record_id in by_id]
    if kind == "elements":
        records = [record for record in records if record["type"] == element_type]
    rows = [[label for key, label in columns]]
    for record in records:
        row = []
        for key, label in columns:
            value = record.get("payload", {}).get(key[8:]) if key.startswith("payload.") else record.get(key)
            if key == "worked":
                value = "已出作品" if value else "未出作品"
            elif isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            row.append("" if value is None else value)
        rows.append(row)
    name = next((item["label"] for item in ELEMENT_TYPES if item["type"] == element_type), NAMES[kind])
    format_name = body.get("format") or "xlsx"
    if format_name == "csv":
        output = io.StringIO(newline="")
        csv.writer(output).writerows(rows)
        return output.getvalue().encode("utf-8-sig"), name + ".csv", "text/csv; charset=utf-8"
    if format_name != "xlsx":
        raise ValueError("表格格式只能是 Excel 或 CSV")
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = name
    for row in rows:
        if any(len(str(value)) > 32767 for value in row):
            raise ValueError("内容超过 Excel 单元格的 32767 字符限制，请选择 CSV 格式导出")
        sheet.append(row)
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = "s"
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="4F6EF7")
    sheet.freeze_panes = "B2"
    sheet.auto_filter.ref = sheet.dimensions
    for index, label in enumerate(rows[0], start=1):
        sheet.column_dimensions[get_column_letter(index)].width = 40 if label in {"模板", "正文", "提示词", "节点与连线", "执行详情"} else 22
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue(), name + ".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def read_table(raw: bytes, filename: str) -> list[dict]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".xlsx":
        from openpyxl import load_workbook
        try:
            workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=False)
            try:
                rows = []
                for row in workbook.active.iter_rows():
                    if any(cell.data_type == "f" for cell in row):
                        raise ValueError("导入表格不能包含公式，请先粘贴为值")
                    rows.append(["" if cell.value is None else str(cell.value) for cell in row])
                    if len(rows) > 10001:
                        raise ValueError("每次最多导入 10000 行")
            finally:
                workbook.close()
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("无法读取 Excel 文件，请使用有效的 .xlsx 文件") from exc
    elif suffix in {".csv", ".tsv"}:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("gb18030")
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter="\t" if suffix == ".tsv" else ","))
    else:
        raise ValueError("请选择 .xlsx、.csv 或 .tsv 表格")
    rows = [row for row in rows if any(str(value).strip() for value in row)]
    if len(rows) < 2:
        raise ValueError("表格没有数据行，请先导出表格作为模板")
    if len(rows) > 10001:
        raise ValueError("每次最多导入 10000 行")
    header = [value.strip() for value in rows[0]]
    if len(set(header)) != len(header) or any(not value for value in header):
        raise ValueError("表头不能为空或重复")
    return [dict(zip(header, row + [""] * (len(header) - len(row)))) for row in rows[1:]]


def json_value(value: str, expected, label: str):
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(label + "需要有效的 JSON") from exc
    if not isinstance(parsed, expected):
        raise ValueError(label + "格式不正确")
    return parsed


def list_value(value: str, label: str) -> list:
    if not value.strip():
        return []
    if value.lstrip().startswith("["):
        return json_value(value, list, label)
    if label == "字段":
        return [{"key": item.partition("=")[0], "value": item.partition("=")[2]} for item in value.split("|") if item]
    return [item.strip() for item in value.replace("，", "|").replace(",", "|").split("|") if item.strip()]


def bool_value(value: str) -> bool:
    if value.lower() in {"true", "1", "是", "已出作品"}:
        return True
    if value.lower() in {"false", "0", "否", "未出作品", ""}:
        return False
    raise ValueError("布尔值应为 true/false 或 是/否")


def import_table(store, kind: str, element_type: str, raw: bytes, filename: str) -> dict:
    with store.transaction():
        return _import_table(store, kind, element_type, raw, filename)


def _import_table(store, kind: str, element_type: str, raw: bytes, filename: str) -> dict:
    columns = columns_for(kind, element_type)
    records = read_table(raw, filename)
    expected = {label for key, label in columns}
    if not expected.intersection(records[0].keys()) - {"id"}:
        raise ValueError("表头与当前列表不匹配，请先导出表格作为模板")
    existing = {record["id"]: record for record in records_for(store, kind)}
    prepared = []
    seen = set()
    for number, row in enumerate(records, start=2):
        try:
            record_id = str(row.get("id") or "").strip()
            id_kind = element_type if kind == "elements" else kind.rstrip("s")
            record_id = store.resolve_id("element" if kind == "elements" else id_kind, record_id)
            if record_id and record_id in seen:
                raise ValueError("编号重复")
            if record_id:
                seen.add(record_id)
            previous = existing.get(record_id)
            if kind == "elements" and previous and previous["type"] != element_type:
                raise ValueError("编号属于其他类型的元素")
            record = copy.deepcopy(previous or {})
            record["id"] = previous["id"] if previous else store.uuid_id(id_kind, record_id)
            if kind == "elements":
                record.setdefault("payload", blank_payload(element_type))
                record["type"] = element_type
                if row.get("类型") and row["类型"] != element_type:
                    raise ValueError("类型与当前元素列表不一致")
            for key, label in columns:
                if key in {"id", "type"} or label not in row:
                    continue
                value = row[label]
                if key.startswith("payload."):
                    payload_key = key[8:]
                    default = ELEMENT_DEFAULTS[element_type].get(payload_key)
                    if isinstance(default, list):
                        value = list_value(value, label)
                    elif isinstance(default, bool):
                        value = bool_value(value)
                    elif payload_key in {"temperature", "maxTokens", "concurrency", "useCount"}:
                        value = (float(value) if payload_key == "temperature" else int(value)) if value.strip() else default
                    record["payload"][payload_key] = value
                elif key in {"tags", "fields", "referenceTags"}:
                    record[key] = list_value(value, label)
                elif key in {"graph", "schedule", "trace"}:
                    record[key] = json_value(value, dict, label) if value.strip() else {}
                elif key == "worked":
                    record[key] = bool_value(value)
                else:
                    record[key] = value
            if kind in {"elements", "flows"}:
                if not str(record.get("name") or "").strip():
                    raise ValueError("名称不能为空")
                record.setdefault("status", "启用")
                if record["status"] not in {"启用", "停用"}:
                    raise ValueError("状态只能是启用或停用")
                if any(not isinstance(item, dict) or not item.get("key") for item in record.get("fields", [])):
                    raise ValueError("字段需要包含 key 和 value")
            if kind == "elements":
                payload = record["payload"]
                if any(not isinstance(item, str) for item in payload.get("tags", [])):
                    raise ValueError("内容标签需要文字数组")
                if payload.get("concurrency") is not None and not 1 <= payload["concurrency"] <= 12:
                    raise ValueError("并发数必须在 1 至 12 之间")
                for key in ("variants", "steps"):
                    if key in payload and any(not isinstance(item, dict) for item in payload[key]):
                        raise ValueError(LABELS[key] + "需要对象数组")
                for variant in payload.get("variants", []):
                    for key in ("width", "height", "count"):
                        if key in variant and int(variant[key]) < 1:
                            raise ValueError("生图宽高和数量必须为正整数")
            elif kind == "flows":
                record.setdefault("graph", {"nodes": [blank_node("start.theme", new_id("node"), 80, 160)], "edges": []})
                if not isinstance(record["graph"].get("nodes"), list) or not isinstance(record["graph"].get("edges"), list):
                    raise ValueError("节点与连线需要 nodes 和 edges 数组")
                record["schedule"] = normalize_schedule(record.get("schedule") or dict(BLANK_SCHEDULE))
            elif kind == "results":
                record.setdefault("name", "结果图")
                if not previous or record.get("path") != previous.get("path"):
                    source = resolve_path(str(record.get("path") or ""))
                    if not source.is_file() or source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                        raise ValueError("新增结果需要有效的本地图片路径")
                    record["_source"] = source
            else:
                record.setdefault("startedAt", now_iso())
                record.setdefault("mode", "once")
                record.setdefault("status", "succeeded")
                record.setdefault("trace", {})
                record.setdefault("flowId", "")
                if record["status"] not in {"succeeded", "failed"} or record["mode"] not in {"once", "schedule"}:
                    raise ValueError("状态应为 succeeded/failed，方式应为 once/schedule")
            for key in ("tags", "referenceTags"):
                if any(not isinstance(item, str) for item in record.get(key, [])):
                    raise ValueError("标签需要文字数组")
            prepared.append((record, previous))
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"第 {number} 行：{exc}") from exc
    created = updated = 0
    copied = []
    try:
        with store.transaction():
            for record, previous in prepared:
                if kind == "elements":
                    store.upsert_element(record)
                elif kind == "flows":
                    record["graph"] = store.normalize_graph(record["id"], record["graph"])
                    store.upsert_flow(record)
                elif kind == "results":
                    record.update(rewrite_document(store, record, record.get("flowId") or ""))
                    source = record.pop("_source", None)
                    if source:
                        relative = "results/" + uuid.uuid4().hex + source.suffix.lower()
                        target = resolve_path(relative)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source, target)
                        copied.append(target)
                        record["path"] = relative
                    if not previous:
                        store.save_result(record)
                    store.update_result(record["id"], record)
                elif previous:
                    record["flowId"] = store.uuid_id("flow", record["flowId"]) if record["flowId"] else ""
                    record["trace"] = rewrite_trace(store, record["trace"], record["flowId"])
                    store.update_run(record["id"], record)
                else:
                    record["flowId"] = store.uuid_id("flow", record["flowId"]) if record["flowId"] else ""
                    record["trace"] = rewrite_trace(store, record["trace"], record["flowId"])
                    store.add_run(record)
                if previous:
                    updated += 1
                else:
                    created += 1
    except Exception:
        for target in copied:
            target.unlink(missing_ok=True)
        raise
    return {"ok": True, "created": created, "updated": updated, "count": created + updated}
