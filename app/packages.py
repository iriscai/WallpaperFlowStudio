"""Import and export a flow together with the elements it points at."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from app.carry import FlowError
from app.paths import PROJECT_ROOT
from app.schedule import normalize_schedule

SECRET_KEYS = {"apiKey", "api_key", "secret", "key"}


def collect_element_ids(graph: dict) -> list[str]:
    found: list[str] = []

    def add(element_id):
        if element_id and element_id not in found:
            found.append(element_id)

    for node in graph.get("nodes") or []:
        for item in node.get("imports") or []:
            add(item.get("elementId"))
        for bucket in ("rulesBefore", "rulesAfter"):
            for rule_id in node.get(bucket) or []:
                add(rule_id)
    for edge in graph.get("edges") or []:
        for rule_id in edge.get("rules") or []:
            add(rule_id)
    return found


def public_element(element: dict) -> dict:
    payload = copy.deepcopy(element.get("payload") or {})
    for key in SECRET_KEYS:
        payload.pop(key, None)
    return {
        "id": element["id"],
        "type": element["type"],
        "name": element["name"],
        "group": element.get("group") or "",
        "tags": element.get("tags") if isinstance(element.get("tags"), list) else [],
        "fields": element.get("fields") if isinstance(element.get("fields"), list) else [],
        "payload": payload,
    }


def build_flow_package(store, flow: dict) -> dict:
    elements = []
    for element_id in collect_element_ids(flow.get("graph") or {}):
        element = store.get_element(element_id)
        if element is not None:
            elements.append(public_element(element))
    return {
        "format": "mp-content-flow",
        "version": 1,
        "exportedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "flow": {
            "id": flow["id"],
            "name": flow["name"],
            "description": flow.get("description") or "",
            "group": flow.get("group") or "",
            "tags": flow.get("tags") if isinstance(flow.get("tags"), list) else [],
            "fields": flow.get("fields") if isinstance(flow.get("fields"), list) else [],
            "graph": flow.get("graph") or {"nodes": [], "edges": []},
            "schedule": flow.get("schedule") or {},
        },
        "elements": elements,
    }


def build_library_package(store) -> dict:
    return {
        "format": "mp-content-flow-library",
        "version": 1,
        "exportedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elements": [public_element(element) for element in store.list_elements()],
    }


def build_flow_batch(store, flow_ids: list[str] | None = None) -> dict:
    chosen = store.list_flows()
    if flow_ids:
        wanted = set(flow_ids)
        chosen = [flow for flow in chosen if flow["id"] in wanted]
    return {
        "format": "mp-content-flow-batch",
        "version": 1,
        "exportedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "packages": [build_flow_package(store, flow) for flow in chosen],
    }


def import_package(store, package: dict) -> dict:
    with store.transaction():
        return _import_package(store, package)


def _import_package(store, package: dict) -> dict:
    if isinstance(package, dict) and package.get("format") == "mp-content-flow-batch":
        imported = ""
        count = 0
        for item in package.get("packages") or []:
            result = import_package(store, item)
            count += 1
            imported = result.get("flowId") or imported
        return {"elements": count, "flowId": imported}
    if not isinstance(package, dict) or package.get("format") not in ("mp-content-flow", "mp-content-flow-library"):
        raise FlowError("这不是壁纸生创流程台的导出文件")
    if int(package.get("version") or 0) != 1:
        raise FlowError("导出文件的版本不是 1")
    count = 0
    for element in package.get("elements") or []:
        if not element.get("id") or not element.get("type") or not element.get("name"):
            raise FlowError("有一个元素缺少编号、类型或名称")
        element = public_element(element)
        previous = store.get_element(element["id"])
        element["id"] = previous["id"] if previous else store.uuid_id(element["type"], element["id"])
        store.upsert_element(element)
        count += 1
    flow = package.get("flow")
    flow_id = ""
    if flow:
        if not flow.get("id") or not flow.get("name"):
            raise FlowError("流程缺少编号或名称")
        previous = store.get_flow(flow["id"])
        flow_id = previous["id"] if previous else store.uuid_id("flow", flow["id"])
        store.upsert_flow(
            {
                "id": flow_id,
                "name": flow["name"],
                "description": flow.get("description") or "",
                "group": flow.get("group") or "",
                "tags": flow.get("tags") if isinstance(flow.get("tags"), list) else [],
                "fields": flow.get("fields") if isinstance(flow.get("fields"), list) else [],
                "graph": store.normalize_graph(flow_id, flow.get("graph") or {"nodes": [], "edges": []}),
                "schedule": normalize_schedule(flow.get("schedule") or {}),
            }
        )
    return {"elements": count, "flowId": flow_id}


def write_example_if_missing(store) -> Path | None:
    path = PROJECT_ROOT / "resources" / "examples" / "wallpaper-article.json"
    if path.exists():
        return path
    flow = store.get_flow("flow-wallpaper-article")
    if flow is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_flow_package(store, flow), ensure_ascii=False, indent=2), encoding="utf-8")
    return path
