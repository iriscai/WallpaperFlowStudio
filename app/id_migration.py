"""Back up and atomically migrate live identities, preserving historical paths and text."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime

from app.ids import PREFIXES


def rewrite_document(store, value, flow_id: str, context: str = ""):
    if isinstance(value, list):
        return [rewrite_document(store, item, flow_id, context) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    fields = {
        "flowId": "flow", "runId": "run", "resultId": "result", "referenceId": "reference_image",
        "templateId": "prompt", "themeId": "theme", "configGroupId": "image_config_group",
        "elementId": value.get("elementType") or "prompt",
    }
    for key, item in value.items():
        if key in fields and isinstance(item, str) and item:
            kind = fields[key]
            result[key] = store.uuid_id(kind if kind in PREFIXES else "prompt", item)
        elif key == "id" and isinstance(item, str) and item:
            kind = {"domain": "domain", "referenceUsed": "reference_image", "referencesUsed": "reference_image", "results": "result"}.get(context)
            result[key] = store.uuid_id(kind, item) if kind else item
        else:
            result[key] = rewrite_document(store, item, flow_id, key)
    return result


def rewrite_trace(store, trace: dict, flow_id: str) -> dict:
    trace = rewrite_document(store, trace, flow_id)
    if isinstance(trace.get("order"), list):
        trace["order"] = [store.uuid_id("node", value, flow_id) for value in trace["order"]]
    if isinstance(trace.get("nodes"), dict):
        trace["nodes"] = {store.uuid_id("node", key, flow_id): value for key, value in trace["nodes"].items()}
    return trace


def migrate_uuid_ids(store) -> dict:
    with store.lock:
        if store.get_setting("uuid_ids") == "1":
            return {"migrated": False}
        backup_dir = store.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / (store.path.stem + "-before-uuid-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".sqlite3")
        with closing(sqlite3.connect(backup_path)) as backup:
            store.conn.backup(backup)
        counts = {}
        with store.transaction():
            elements = store.list_elements()
            flows = store.list_flows()
            runs = store.list_runs(limit=0)
            results = store.list_results(limit=0)
            # Allocate identities before rewriting any references or table keys.
            for element in elements:
                store.uuid_id(element["type"], element["id"])
            for kind, records in (("flow", flows), ("run", runs), ("result", results)):
                for record in records:
                    store.uuid_id(kind, record["id"])
                counts[kind] = len(records)
            for flow in flows:
                old = flow["id"]
                allocated = store.uuid_id("flow", old)
                graph = store.normalize_graph(allocated, flow["graph"])
                store.conn.execute("UPDATE flows SET id = ?, graph = ? WHERE id = ?", (allocated, json.dumps(graph, ensure_ascii=False), old))
                store.conn.execute("UPDATE flow_fields SET flow_id = ? WHERE flow_id = ?", (allocated, old))
            for element in elements:
                old = element["id"]
                allocated = store.uuid_id(element["type"], old)
                for table in ("element_registry", store_table(element["type"])):
                    store.conn.execute(f"UPDATE {table} SET id = ? WHERE id = ?", (allocated, old))
                for table, column in (("element_fields", "element_id"), ("image_variants", "group_id"), ("rule_steps", "rule_id")):
                    store.conn.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (allocated, old))
                store.conn.execute("UPDATE settings SET key = ? WHERE key = ?", ("secret:" + allocated, "secret:" + old))
            for run in runs:
                flow_id = store.uuid_id("flow", run["flowId"]) if run["flowId"] else ""
                trace = rewrite_trace(store, run["trace"], flow_id)
                store.conn.execute("UPDATE runs SET id = ?, flow_id = ?, trace = ? WHERE id = ?", (store.uuid_id("run", run["id"]), flow_id, json.dumps(trace, ensure_ascii=False), run["id"]))
            for record in results:
                store.conn.execute(
                    "UPDATE results SET id = ?, flow_id = ?, run_id = ?, reference_id = ? WHERE id = ?",
                    (store.uuid_id("result", record["id"]),
                     store.uuid_id("flow", record["flowId"]) if record["flowId"] else "",
                     store.uuid_id("run", record["runId"]) if record["runId"] else "",
                     store.uuid_id("reference_image", record["referenceId"]) if record["referenceId"] else "", record["id"]),
                )
            store.set_setting("uuid_ids", "1")
            store.set_setting("uuid_backup", str(backup_path))
            counts["element"] = len(elements)
        return {"migrated": True, "counts": counts, "backup": str(backup_path)}


def store_table(kind: str) -> str:
    # Import lazily so Store can expose migration helpers without a cycle.
    from app.store import RECORD_TABLES
    return RECORD_TABLES[kind]
