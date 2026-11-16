"""SQLite storage for elements, flows, runs, and model secrets."""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.catalog import BLANK_SCHEDULE
from app.ids import PREFIXES, copy_graph, is_uuid_id, new_id, remap_graph
from app.schedule import normalize_schedule

RECORD_TABLES = {
    "domain": "domains",
    "theme": "themes",
    "reference_image": "reference_images",
    "reference_article": "reference_articles",
    "reference_copy": "reference_copies",
    "prompt": "prompts",
    "image_config_group": "image_groups",
    "model_profile": "model_profiles",
    "output_target": "output_targets",
    "rule": "rules",
}
CHILD_TABLES = {"image_config_group", "rule"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_secrets(values) -> list[str]:
    cleaned = []
    seen = set()
    for item in values or []:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned


def _parse_secrets(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return _clean_secrets(parsed)
    if "\n" in text:
        return _clean_secrets(text.splitlines())
    return [text]


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self._transaction_depth = 0
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        with self.lock:
            self.conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS element_registry (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS domains (
                    id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL DEFAULT '',
                    audience TEXT NOT NULL DEFAULT '',
                    tone TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS themes (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS reference_images (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS reference_articles (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL DEFAULT '',
                    usage TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS reference_copies (
                    id TEXT PRIMARY KEY,
                    copy_text TEXT NOT NULL DEFAULT '',
                    usage TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS prompts (
                    id TEXT PRIMARY KEY,
                    purpose TEXT NOT NULL DEFAULT '',
                    template TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS image_groups (
                    id TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS image_variants (
                    group_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    width INTEGER NOT NULL DEFAULT 1080,
                    height INTEGER NOT NULL DEFAULT 1920,
                    count INTEGER NOT NULL DEFAULT 1,
                    style TEXT NOT NULL DEFAULT '',
                    negative TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (group_id, position)
                );
                CREATE TABLE IF NOT EXISTS model_profiles (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL DEFAULT 'text',
                    base_url TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    temperature REAL NOT NULL DEFAULT 0.7,
                    max_tokens INTEGER
                );
                CREATE TABLE IF NOT EXISTS output_targets (
                    id TEXT PRIMARY KEY,
                    directory TEXT NOT NULL DEFAULT '',
                    write_folder INTEGER NOT NULL DEFAULT 1,
                    write_zip INTEGER NOT NULL DEFAULT 0,
                    zip_name TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS rules (
                    id TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS rule_steps (
                    rule_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    op TEXT NOT NULL DEFAULT '',
                    field TEXT NOT NULL DEFAULT '',
                    source_path TEXT NOT NULL DEFAULT '',
                    template TEXT NOT NULL DEFAULT '',
                    value TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (rule_id, position)
                );
                CREATE TABLE IF NOT EXISTS flows (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    graph TEXT NOT NULL,
                    schedule TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    flow_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    trace TEXT NOT NULL,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runs_flow ON runs(flow_id, started_at);
                CREATE TABLE IF NOT EXISTS works (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    downloaded_at TEXT,
                    UNIQUE(run_id, node_id)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS id_aliases (
                    kind TEXT NOT NULL,
                    old_id TEXT NOT NULL,
                    new_id TEXT NOT NULL,
                    PRIMARY KEY (kind, old_id)
                );
                """
            )
            self._ensure_library_meta()
            self._ensure_flow_meta()
            self._ensure_reference_usage()
            self._ensure_results()
            model_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(model_profiles)")}
            if "concurrency" not in model_columns:
                self.conn.execute("ALTER TABLE model_profiles ADD COLUMN concurrency INTEGER")
            self._migrate_legacy_elements()
            self._commit()

    def _commit(self) -> None:
        if self._transaction_depth == 0:
            self.conn.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            self._transaction_depth += 1
            try:
                yield
                if self._transaction_depth == 1:
                    self.conn.commit()
            except Exception:
                if self._transaction_depth == 1:
                    self.conn.rollback()
                raise
            finally:
                self._transaction_depth -= 1

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def get_setting(self, key: str) -> str | None:
        with self.lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_setting(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._commit()

    def resolve_id(self, kind: str, value: str) -> str:
        with self.lock:
            row = self.conn.execute("SELECT new_id FROM id_aliases WHERE kind = ? AND old_id = ?", (kind, value)).fetchone()
        return row["new_id"] if row else value

    def uuid_id(self, kind: str, value: str = "", scope: str = "") -> str:
        namespace = kind + (":" + scope if scope else "") if kind in {"flow", "run", "result", "node", "edge"} else "element"
        with self.lock:
            resolved = self.resolve_id(namespace, value) if value else ""
            if resolved != value or is_uuid_id(kind, resolved):
                return resolved
            allocated = new_id(kind)
            if value:
                self.conn.execute("INSERT INTO id_aliases(kind, old_id, new_id) VALUES (?, ?, ?) ON CONFLICT(kind, old_id) DO UPDATE SET new_id = excluded.new_id", (namespace, value, allocated))
                self._commit()
            return allocated

    def normalize_graph(self, flow_id: str, graph: dict) -> dict:
        def element_id(value, kind):
            element = self.get_element(value)
            if element:
                return element["id"]
            kind = (element or {}).get("type") or kind or "prompt"
            return self.uuid_id(kind if kind in PREFIXES else "prompt", value)
        return remap_graph(graph, lambda value: self.uuid_id("node", value, flow_id), lambda value: self.uuid_id("edge", value, flow_id), element_id)

    def _write_id(self, kind: str, value: str | None) -> str:
        if self.get_setting("uuid_ids") == "1":
            return self.uuid_id(kind, value or "")
        namespace = kind if kind in {"flow", "run", "result"} else "element"
        return self.resolve_id(namespace, value) if value else new_id(kind)

    def get_secret(self, element_id: str) -> str:
        keys = self.get_secrets(element_id)
        return keys[0] if keys else ""

    def get_secrets(self, element_id: str) -> list[str]:
        element_id = self.resolve_id("element", element_id)
        return _parse_secrets(self.get_setting(f"secret:{element_id}") or "")

    def set_secret(self, element_id: str, api_key: str) -> None:
        self.set_secrets(element_id, _parse_secrets(api_key))

    def set_secrets(self, element_id: str, api_keys: list[str]) -> None:
        self._write_secrets(element_id, _clean_secrets(api_keys))

    def append_secrets(self, element_id: str, api_keys: list[str]) -> None:
        self._write_secrets(element_id, _clean_secrets(self.get_secrets(element_id) + list(api_keys or [])))

    def remove_secret_at(self, element_id: str, index: int) -> None:
        keys = self.get_secrets(element_id)
        if 0 <= index < len(keys):
            del keys[index]
        self._write_secrets(element_id, keys)

    def _write_secrets(self, element_id: str, api_keys: list[str]) -> None:
        element_id = self.resolve_id("element", element_id)
        key = f"secret:{element_id}"
        if not api_keys:
            with self.lock:
                self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))
                self._commit()
            return
        self.set_setting(key, json.dumps(api_keys, ensure_ascii=False))

    def secret_status(self) -> dict:
        with self.lock:
            rows = self.conn.execute("SELECT key, value FROM settings WHERE key LIKE 'secret:%'").fetchall()
        status = {}
        for row in rows:
            element_id = row["key"].split(":", 1)[1]
            keys = _parse_secrets(row["value"] or "")
            status[element_id] = {
                "hasKey": bool(keys),
                "count": len(keys),
                "last4": keys[0][-4:] if keys and len(keys[0]) >= 4 else "",
                "keys": [{"last4": item[-4:] if len(item) >= 4 else item} for item in keys],
            }
        return status

    def upsert_element(self, element: dict) -> dict:
        if element.get("type") not in RECORD_TABLES and element.get("type") not in CHILD_TABLES:
            raise ValueError("不认识的元素类型")
        with self.lock:
            element = copy.deepcopy(element)
            element["id"] = self._write_id(element["type"], element.get("id"))
            self._save_record(element, now_iso())
            self._commit()
            return self._load_record(element["id"])

    def get_element(self, element_id: str) -> dict | None:
        element_id = self.resolve_id("element", element_id)
        with self.lock:
            return self._load_record(element_id)

    def list_elements(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT id FROM element_registry ORDER BY type, name").fetchall()
            return [item for row in rows if (item := self._load_record(row["id"]))]

    def delete_element(self, element_id: str) -> None:
        element_id = self.resolve_id("element", element_id)
        with self.lock:
            current = self.conn.execute("SELECT type FROM element_registry WHERE id = ?", (element_id,)).fetchone()
            if current is not None:
                self._delete_record(element_id, current["type"])
            self.conn.execute("DELETE FROM settings WHERE key = ?", (f"secret:{element_id}",))
            self._commit()

    def _migrate_legacy_elements(self) -> None:
        names = {row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "elements" not in names:
            return
        columns = [row[1] for row in self.conn.execute("PRAGMA table_info(elements)")]
        if "payload" not in columns:
            return
        rows = self.conn.execute("SELECT id, type, name, payload, updated_at FROM elements").fetchall()
        for row in rows:
            self._save_record(
                {
                    "id": row["id"],
                    "type": row["type"],
                    "name": row["name"],
                    "payload": json.loads(row["payload"] or "{}"),
                },
                row["updated_at"],
            )
        self.conn.execute("ALTER TABLE elements RENAME TO elements_json_backup")

    def _save_record(self, element: dict, stamp: str) -> None:
        element_id = element["id"]
        kind = element["type"]
        name = element.get("name") or "未命名"
        payload = element.get("payload") or {}
        previous = self.conn.execute("SELECT type FROM element_registry WHERE id = ?", (element_id,)).fetchone()
        if previous is not None and previous["type"] != kind:
            self._delete_record(element_id, previous["type"])
        tags = element.get("tags") if isinstance(element.get("tags"), list) else []
        self.conn.execute(
            """
            INSERT INTO element_registry(id, type, name, updated_at, group_name, tags, status)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                type = excluded.type,
                name = excluded.name,
                updated_at = excluded.updated_at,
                group_name = excluded.group_name,
                tags = excluded.tags,
                status = excluded.status
            """,
            (
                element_id,
                kind,
                name,
                stamp,
                element.get("group") or "",
                json.dumps(tags, ensure_ascii=False),
                element.get("status") or "启用",
            ),
        )
        self.conn.execute("DELETE FROM element_fields WHERE element_id = ?", (element_id,))
        position = 0
        for item in element.get("fields") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            self.conn.execute(
                "INSERT INTO element_fields(element_id, position, field_key, field_value) VALUES(?, ?, ?, ?)",
                (element_id, position, key, "" if item.get("value") is None else str(item.get("value"))),
            )
            position += 1
        if kind == "domain":
            self._upsert_row(
                "domains",
                element_id,
                {
                    "summary": payload.get("summary") or "",
                    "audience": payload.get("audience") or "",
                    "tone": payload.get("tone") or "",
                    "tags": json.dumps(payload.get("tags") or [], ensure_ascii=False),
                },
            )
        elif kind == "theme":
            self._upsert_row("themes", element_id, {"text": payload.get("text") or "", "notes": payload.get("notes") or ""})
        elif kind == "reference_image":
            self._upsert_row(
                "reference_images",
                element_id,
                {"path": payload.get("path") or "", "role": payload.get("role") or "", "note": payload.get("note") or ""},
            )
            if "useCount" in payload:
                self.conn.execute("UPDATE reference_images SET use_count = ? WHERE id = ?", (max(0, int(payload["useCount"])), element_id))
        elif kind == "reference_article":
            self._upsert_row(
                "reference_articles",
                element_id,
                {"title": payload.get("title") or "", "body": payload.get("body") or "", "usage": payload.get("usage") or ""},
            )
        elif kind == "reference_copy":
            self._upsert_row(
                "reference_copies",
                element_id,
                {"copy_text": payload.get("text") or "", "usage": payload.get("usage") or ""},
            )
        elif kind == "prompt":
            self._upsert_row(
                "prompts",
                element_id,
                {
                    "purpose": payload.get("purpose") or "",
                    "template": payload.get("template") or "",
                    "notes": payload.get("notes") or "",
                },
            )
        elif kind == "image_config_group":
            self._upsert_row("image_groups", element_id, {})
            self.conn.execute("DELETE FROM image_variants WHERE group_id = ?", (element_id,))
            for index, variant in enumerate(payload.get("variants") or []):
                if not isinstance(variant, dict):
                    continue
                self.conn.execute(
                    """
                    INSERT INTO image_variants(group_id, position, name, width, height, count, style, negative)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        element_id,
                        index,
                        variant.get("name") or "",
                        int(variant.get("width") or 1080),
                        int(variant.get("height") or 1920),
                        int(variant.get("count") or 0),
                        variant.get("style") or "",
                        variant.get("negative") or "",
                    ),
                )
        elif kind == "model_profile":
            max_tokens = payload.get("maxTokens")
            self._upsert_row(
                "model_profiles",
                element_id,
                {
                    "kind": payload.get("kind") or "text",
                    "base_url": payload.get("baseUrl") or "",
                    "model": payload.get("model") or "",
                    "temperature": float(payload.get("temperature") if payload.get("temperature") is not None else 0.7),
                    "max_tokens": int(max_tokens) if isinstance(max_tokens, int) and max_tokens > 0 else None,
                    "concurrency": int(payload["concurrency"]) if payload.get("concurrency") else None,
                },
            )
        elif kind == "output_target":
            self._upsert_row(
                "output_targets",
                element_id,
                {
                    "directory": payload.get("directory") or "",
                    "write_folder": 1 if payload.get("writeFolder", True) else 0,
                    "write_zip": 1 if payload.get("writeZip") else 0,
                    "zip_name": payload.get("zipName") or "",
                },
            )
        elif kind == "rule":
            self._upsert_row("rules", element_id, {})
            self.conn.execute("DELETE FROM rule_steps WHERE rule_id = ?", (element_id,))
            for index, step in enumerate(payload.get("steps") or []):
                if not isinstance(step, dict):
                    continue
                self.conn.execute(
                    """
                    INSERT INTO rule_steps(rule_id, position, op, field, source_path, template, value, message)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        element_id,
                        index,
                        step.get("op") or "",
                        step.get("field") or "",
                        step.get("from") or "",
                        step.get("template") or "",
                        "" if step.get("value") is None else str(step.get("value")),
                        step.get("message") or "",
                    ),
                )
        else:
            raise ValueError("不认识的元素类型")

    def _load_record(self, element_id: str) -> dict | None:
        registry = self.conn.execute("SELECT * FROM element_registry WHERE id = ?", (element_id,)).fetchone()
        if registry is None:
            return None
        kind = registry["type"]
        payload = self._payload(kind, element_id)
        try:
            tags = json.loads(registry["tags"] or "[]")
        except json.JSONDecodeError:
            tags = []
        field_rows = self.conn.execute(
            "SELECT field_key, field_value FROM element_fields WHERE element_id = ? ORDER BY position",
            (element_id,),
        ).fetchall()
        return {
            "id": element_id,
            "type": kind,
            "name": registry["name"],
            "group": registry["group_name"] or "",
            "tags": tags if isinstance(tags, list) else [],
            "status": registry["status"] if "status" in registry.keys() and registry["status"] else "启用",
            "fields": [{"key": row["field_key"], "value": row["field_value"]} for row in field_rows],
            "payload": payload,
            "useCount": int(payload.get("useCount") or 0) if kind == "reference_image" else 0,
            "updatedAt": registry["updated_at"],
        }

    def _payload(self, kind: str, element_id: str) -> dict:
        if kind == "domain":
            row = self._one("domains", element_id)
            return {
                "summary": row["summary"] if row else "",
                "audience": row["audience"] if row else "",
                "tone": row["tone"] if row else "",
                "tags": json.loads(row["tags"] or "[]") if row else [],
            }
        if kind == "theme":
            row = self._one("themes", element_id)
            return {"text": row["text"] if row else "", "notes": row["notes"] if row else ""}
        if kind == "reference_image":
            row = self._one("reference_images", element_id)
            return {
                "path": row["path"] if row else "",
                "role": row["role"] if row else "",
                "note": row["note"] if row else "",
                "useCount": int(row["use_count"] or 0) if row and "use_count" in row.keys() else 0,
            }
        if kind == "reference_article":
            row = self._one("reference_articles", element_id)
            return {
                "title": row["title"] if row else "",
                "body": row["body"] if row else "",
                "usage": row["usage"] if row else "",
            }
        if kind == "reference_copy":
            row = self._one("reference_copies", element_id)
            return {"text": row["copy_text"] if row else "", "usage": row["usage"] if row else ""}
        if kind == "prompt":
            row = self._one("prompts", element_id)
            return {
                "purpose": row["purpose"] if row else "",
                "template": row["template"] if row else "",
                "notes": row["notes"] if row else "",
            }
        if kind == "image_config_group":
            variants = self.conn.execute(
                "SELECT * FROM image_variants WHERE group_id = ? ORDER BY position",
                (element_id,),
            ).fetchall()
            return {
                "variants": [
                    {
                        "name": item["name"],
                        "width": item["width"],
                        "height": item["height"],
                        "count": item["count"],
                        "style": item["style"],
                        "negative": item["negative"],
                    }
                    for item in variants
                ]
            }
        if kind == "model_profile":
            row = self._one("model_profiles", element_id)
            return {
                "kind": row["kind"] if row else "text",
                "baseUrl": row["base_url"] if row else "",
                "model": row["model"] if row else "",
                "temperature": row["temperature"] if row else 0.7,
                "maxTokens": row["max_tokens"] if row else None,
                "concurrency": row["concurrency"] if row else None,
            }
        if kind == "output_target":
            row = self._one("output_targets", element_id)
            return {
                "directory": row["directory"] if row else "",
                "writeFolder": bool(row["write_folder"]) if row else True,
                "writeZip": bool(row["write_zip"]) if row else False,
                "zipName": row["zip_name"] if row else "",
            }
        if kind == "rule":
            steps = self.conn.execute(
                "SELECT * FROM rule_steps WHERE rule_id = ? ORDER BY position",
                (element_id,),
            ).fetchall()
            return {
                "steps": [
                    {
                        "op": item["op"],
                        "field": item["field"],
                        "from": item["source_path"],
                        "template": item["template"],
                        "value": item["value"],
                        "message": item["message"],
                    }
                    for item in steps
                ]
            }
        return {}

    def _one(self, table: str, element_id: str):
        return self.conn.execute(f"SELECT * FROM {table} WHERE id = ?", (element_id,)).fetchone()

    def _upsert_row(self, table: str, element_id: str, fields: dict) -> None:
        columns = ["id", *fields.keys()]
        values = [element_id, *fields.values()]
        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(f"{column} = excluded.{column}" for column in fields)
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        if assignments:
            sql += f" ON CONFLICT(id) DO UPDATE SET {assignments}"
        else:
            sql += " ON CONFLICT(id) DO NOTHING"
        self.conn.execute(sql, values)

    def _ensure_flow_meta(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS flow_fields (
                flow_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                field_key TEXT NOT NULL DEFAULT '',
                field_value TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (flow_id, position)
            )
            """
        )
        columns = [row[1] for row in self.conn.execute("PRAGMA table_info(flows)")]
        if not columns:
            return
        if "group_name" not in columns:
            self.conn.execute("ALTER TABLE flows ADD COLUMN group_name TEXT NOT NULL DEFAULT ''")
        if "tags" not in columns:
            self.conn.execute("ALTER TABLE flows ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        if "status" not in columns:
            self.conn.execute("ALTER TABLE flows ADD COLUMN status TEXT NOT NULL DEFAULT '启用'")

    def mark_reference_used(self, element_id: str) -> int:
        element_id = self.resolve_id("element", element_id)
        with self.lock:
            self.conn.execute(
                "UPDATE reference_images SET use_count = COALESCE(use_count, 0) + 1 WHERE id = ?",
                (element_id,),
            )
            row = self.conn.execute("SELECT use_count FROM reference_images WHERE id = ?", (element_id,)).fetchone()
            self._commit()
        return int(row["use_count"]) if row else 0

    def save_result(self, record: dict) -> dict:
        tags = record.get("tags") if isinstance(record.get("tags"), list) else []
        reference_tags = record.get("referenceTags") if isinstance(record.get("referenceTags"), list) else tags
        stored = {
            "id": self._write_id("result", record.get("id")),
            "flow_id": self.resolve_id("flow", record.get("flowId") or ""),
            "flow_name": record.get("flowName") or "",
            "run_id": self.resolve_id("run", record.get("runId") or ""),
            "created_at": record.get("createdAt") or now_iso(),
            "name": record.get("name") or "",
            "group_name": record.get("group") or "",
            "tags": json.dumps(tags, ensure_ascii=False),
            "reference_id": self.resolve_id("element", record.get("referenceId") or ""),
            "reference_name": record.get("referenceName") or "",
            "reference_group": record.get("referenceGroup") or record.get("group") or "",
            "reference_tags": json.dumps(reference_tags, ensure_ascii=False),
            "path": record.get("path") or "",
            "prompt": record.get("prompt") or "",
        }
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO results(
                    id, flow_id, flow_name, run_id, created_at, name, group_name, tags,
                    reference_id, reference_name, reference_group, reference_tags, path, prompt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored["id"], stored["flow_id"], stored["flow_name"], stored["run_id"], stored["created_at"],
                    stored["name"], stored["group_name"], stored["tags"], stored["reference_id"], stored["reference_name"],
                    stored["reference_group"], stored["reference_tags"], stored["path"], stored["prompt"],
                ),
            )
            self._commit()
        return self._result_row(stored)

    def list_results(self, limit: int = 100, group: str = "", unused_only: bool = False) -> list[dict]:
        limit = -1 if int(limit) == 0 else max(1, min(int(limit), 1000))
        sql = "SELECT * FROM results WHERE 1 = 1"
        values: list = []
        if group:
            sql += " AND group_name = ?"
            values.append(group)
        if unused_only:
            sql += " AND COALESCE(worked, 0) = 0"
        sql += " ORDER BY created_at DESC LIMIT ?"
        values.append(limit)
        with self.lock:
            rows = self.conn.execute(sql, values).fetchall()
        return [self._result_row(row) for row in rows]

    def get_result(self, result_id: str) -> dict | None:
        result_id = self.resolve_id("result", result_id)
        with self.lock:
            row = self.conn.execute("SELECT * FROM results WHERE id = ?", (result_id,)).fetchone()
        return self._result_row(row) if row else None

    def update_result(self, result_id: str, fields: dict) -> dict | None:
        result_id = self.resolve_id("result", result_id)
        current = self.get_result(result_id)
        if current is None:
            return None
        name = str(fields.get("name") if "name" in fields else current["name"] or "").strip() or current["name"]
        group = str(fields.get("group") if "group" in fields else current["group"] or "")
        tags = fields.get("tags") if "tags" in fields else current["tags"]
        if not isinstance(tags, list):
            tags = current["tags"]
        prompt = str(fields.get("prompt") if "prompt" in fields else current["prompt"] or "")
        worked = current["worked"] if "worked" not in fields else bool(fields.get("worked"))
        with self.lock:
            self.conn.execute(
                """
                UPDATE results
                SET name = ?, group_name = ?, tags = ?, prompt = ?, worked = ?
                WHERE id = ?
                """,
                (name, group, json.dumps(tags, ensure_ascii=False), prompt, 1 if worked else 0, result_id),
            )
            for key, column in {"path": "path", "flowId": "flow_id", "flowName": "flow_name", "runId": "run_id", "referenceId": "reference_id", "referenceName": "reference_name", "referenceGroup": "reference_group", "createdAt": "created_at"}.items():
                if key in fields:
                    self.conn.execute(f"UPDATE results SET {column} = ? WHERE id = ?", (str(fields[key] or ""), result_id))
            if "referenceTags" in fields:
                self.conn.execute("UPDATE results SET reference_tags = ? WHERE id = ?", (json.dumps(fields["referenceTags"], ensure_ascii=False), result_id))
            self._commit()
        return self.get_result(result_id)

    def delete_results(self, ids: list[str]) -> list[str]:
        cleaned = [self.resolve_id("result", str(item)) for item in ids if str(item).strip()]
        if not cleaned:
            return []
        marks = ", ".join("?" for _ in cleaned)
        with self.lock:
            rows = self.conn.execute(f"SELECT path FROM results WHERE id IN ({marks})", cleaned).fetchall()
            self.conn.execute(f"DELETE FROM results WHERE id IN ({marks})", cleaned)
            self._commit()
        return [str(row["path"] or "") for row in rows]

    def set_results_status(self, ids: list[str], worked: bool) -> int:
        cleaned = [self.resolve_id("result", str(item)) for item in ids if str(item).strip()]
        if not cleaned:
            return 0
        marks = ", ".join("?" for _ in cleaned)
        with self.lock:
            cursor = self.conn.execute(
                f"UPDATE results SET worked = ? WHERE id IN ({marks})",
                [1 if worked else 0, *cleaned],
            )
            self._commit()
        return cursor.rowcount

    def mark_results_worked(self, ids: list[str]) -> None:
        self.set_results_status(ids, True)

    def _result_row(self, row) -> dict:
        data = row if isinstance(row, dict) else dict(row)
        try:
            tags = json.loads(data.get("tags") or "[]")
        except json.JSONDecodeError:
            tags = []
        try:
            reference_tags = json.loads(data.get("reference_tags") or "[]")
        except json.JSONDecodeError:
            reference_tags = []
        return {
            "id": data.get("id") or "",
            "flowId": data.get("flow_id") or "",
            "flowName": data.get("flow_name") or "",
            "runId": data.get("run_id") or "",
            "createdAt": data.get("created_at") or "",
            "name": data.get("name") or "",
            "group": data.get("group_name") or "",
            "tags": tags if isinstance(tags, list) else [],
            "referenceId": data.get("reference_id") or "",
            "referenceName": data.get("reference_name") or "",
            "referenceGroup": data.get("reference_group") or "",
            "referenceTags": reference_tags if isinstance(reference_tags, list) else [],
            "path": data.get("path") or "",
            "prompt": data.get("prompt") or "",
            "worked": bool(int(data.get("worked") or 0)),
        }

    def _ensure_reference_usage(self) -> None:
        columns = [item[1] for item in self.conn.execute("PRAGMA table_info(reference_images)")]
        if columns and "use_count" not in columns:
            self.conn.execute("ALTER TABLE reference_images ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0")

    def _ensure_results(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS results (
                id TEXT PRIMARY KEY,
                flow_id TEXT NOT NULL DEFAULT '',
                flow_name TEXT NOT NULL DEFAULT '',
                run_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                group_name TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                reference_id TEXT NOT NULL DEFAULT '',
                reference_name TEXT NOT NULL DEFAULT '',
                reference_group TEXT NOT NULL DEFAULT '',
                reference_tags TEXT NOT NULL DEFAULT '[]',
                path TEXT NOT NULL DEFAULT '',
                prompt TEXT NOT NULL DEFAULT '',
                worked INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        columns = [item[1] for item in self.conn.execute("PRAGMA table_info(results)")]
        if columns and "worked" not in columns:
            self.conn.execute("ALTER TABLE results ADD COLUMN worked INTEGER NOT NULL DEFAULT 0")

    def _ensure_library_meta(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS element_fields (
                element_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                field_key TEXT NOT NULL DEFAULT '',
                field_value TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (element_id, position)
            )
            """
        )
        columns = [row[1] for row in self.conn.execute("PRAGMA table_info(element_registry)")]
        if "group_name" not in columns:
            self.conn.execute("ALTER TABLE element_registry ADD COLUMN group_name TEXT NOT NULL DEFAULT ''")
        if "tags" not in columns:
            self.conn.execute("ALTER TABLE element_registry ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        if "status" not in columns:
            self.conn.execute("ALTER TABLE element_registry ADD COLUMN status TEXT NOT NULL DEFAULT '启用'")

    def _delete_record(self, element_id: str, kind: str) -> None:
        if kind == "image_config_group":
            self.conn.execute("DELETE FROM image_variants WHERE group_id = ?", (element_id,))
        if kind == "rule":
            self.conn.execute("DELETE FROM rule_steps WHERE rule_id = ?", (element_id,))
        self.conn.execute("DELETE FROM element_fields WHERE element_id = ?", (element_id,))
        table = RECORD_TABLES.get(kind)
        if table:
            self.conn.execute(f"DELETE FROM {table} WHERE id = ?", (element_id,))
        self.conn.execute("DELETE FROM element_registry WHERE id = ?", (element_id,))

    def upsert_flow(self, flow: dict) -> dict:
        stamp = now_iso()
        schedule = flow.get("schedule") or BLANK_SCHEDULE
        with self.lock:
            flow = copy.deepcopy(flow)
            flow["id"] = self._write_id("flow", flow.get("id"))
            if self.get_setting("uuid_ids") == "1":
                flow["graph"] = self.normalize_graph(flow["id"], flow.get("graph") or {"nodes": [], "edges": []})
            tags = flow.get("tags") if isinstance(flow.get("tags"), list) else []
            self.conn.execute(
                """
                INSERT INTO flows(id, name, description, graph, schedule, updated_at, group_name, tags, status)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    graph = excluded.graph,
                    schedule = excluded.schedule,
                    updated_at = excluded.updated_at,
                    group_name = excluded.group_name,
                    tags = excluded.tags,
                    status = excluded.status
                """,
                (
                    flow["id"],
                    flow["name"],
                    flow.get("description") or "",
                    json.dumps(flow.get("graph") or {"nodes": [], "edges": []}, ensure_ascii=False),
                    json.dumps(schedule, ensure_ascii=False),
                    stamp,
                    flow.get("group") or "",
                    json.dumps(tags, ensure_ascii=False),
                    flow.get("status") or "启用",
                ),
            )
            self._replace_flow_fields(flow["id"], flow.get("fields") or [])
            self._commit()
        return self.get_flow(flow["id"])

    def update_schedule(self, flow_id: str, schedule: dict) -> None:
        flow_id = self.resolve_id("flow", flow_id)
        with self.lock:
            self.conn.execute(
                "UPDATE flows SET schedule = ?, updated_at = ? WHERE id = ?",
                (json.dumps(schedule, ensure_ascii=False), now_iso(), flow_id),
            )
            self._commit()

    def copy_flows(self, ids: list[str], count: int, keep_schedule: bool = False) -> list[dict]:
        if not isinstance(ids, list) or not ids or any(not isinstance(item, str) or not item.strip() for item in ids):
            raise ValueError("请选择要复制的流程")
        source_ids = list(dict.fromkeys(self.resolve_id("flow", value) for value in ids))
        if isinstance(count, bool) or not isinstance(count, int) or count < 1 or count * len(source_ids) > 1000:
            raise ValueError("复制份数必须为正整数，每次最多生成 1000 条流程")
        if not isinstance(keep_schedule, bool):
            raise ValueError("沿用定时状态需要布尔值")
        created = []
        with self.transaction():
            existing = self.list_flows()
            by_id = {flow["id"]: flow for flow in existing}
            sources = []
            for source_id in source_ids:
                source = by_id.get(source_id)
                if source is None:
                    raise ValueError("要复制的流程不存在：" + source_id)
                sources.append(source)
            used_names = {flow["name"] for flow in existing}
            for source in sources:
                suffix = 1
                for position in range(count):
                    while source["name"] + "-副本" + str(suffix) in used_names:
                        suffix += 1
                    duplicate = copy.deepcopy(source)
                    duplicate["id"] = new_id("flow")
                    duplicate["graph"] = copy_graph(source.get("graph") or {"nodes": [], "edges": []})
                    duplicate["name"] = source["name"] + "-副本" + str(suffix)
                    duplicate.pop("updatedAt", None)
                    duplicate["schedule"] = copy.deepcopy(source.get("schedule") or BLANK_SCHEDULE)
                    if keep_schedule:
                        duplicate["schedule"] = normalize_schedule(duplicate["schedule"])
                    else:
                        duplicate["schedule"]["enabled"] = False
                        duplicate["schedule"]["nextRunAt"] = None
                    saved = self.upsert_flow(duplicate)
                    created.append(saved)
                    used_names.add(saved["name"])
                    suffix += 1
        return created

    def get_flow(self, flow_id: str) -> dict | None:
        flow_id = self.resolve_id("flow", flow_id)
        with self.lock:
            row = self.conn.execute("SELECT * FROM flows WHERE id = ?", (flow_id,)).fetchone()
            return None if row is None else self._flow(row)

    def list_flows(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM flows ORDER BY updated_at DESC").fetchall()
            return [self._flow(row) for row in rows]

    def set_elements_status(self, ids: list[str], status: str) -> int:
        cleaned = [self.resolve_id("element", str(item)) for item in ids if str(item).strip()]
        if status not in {"启用", "停用"} or not cleaned:
            return 0
        marks = ", ".join("?" for _ in cleaned)
        with self.lock:
            cursor = self.conn.execute(
                f"UPDATE element_registry SET status = ?, updated_at = ? WHERE id IN ({marks})",
                [status, now_iso(), *cleaned],
            )
            self._commit()
        return cursor.rowcount

    def set_flows_status(self, ids: list[str], status: str) -> int:
        cleaned = [self.resolve_id("flow", str(item)) for item in ids if str(item).strip()]
        if status not in {"启用", "停用"} or not cleaned:
            return 0
        marks = ", ".join("?" for _ in cleaned)
        with self.lock:
            cursor = self.conn.execute(
                f"UPDATE flows SET status = ?, updated_at = ? WHERE id IN ({marks})",
                [status, now_iso(), *cleaned],
            )
            self._commit()
        return cursor.rowcount

    def delete_runs(self, ids: list[str]) -> int:
        cleaned = [self.resolve_id("run", str(item)) for item in ids if str(item).strip()]
        if not cleaned:
            return 0
        marks = ", ".join("?" for _ in cleaned)
        with self.lock:
            cursor = self.conn.execute(f"DELETE FROM runs WHERE id IN ({marks})", cleaned)
            self._commit()
        return cursor.rowcount

    def delete_flow(self, flow_id: str) -> None:
        flow_id = self.resolve_id("flow", flow_id)
        with self.lock:
            self.conn.execute("DELETE FROM flow_fields WHERE flow_id = ?", (flow_id,))
            self.conn.execute("DELETE FROM flows WHERE id = ?", (flow_id,))
            self._commit()

    def _replace_flow_fields(self, flow_id: str, fields) -> None:
        self.conn.execute("DELETE FROM flow_fields WHERE flow_id = ?", (flow_id,))
        position = 0
        for item in fields or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            self.conn.execute(
                "INSERT INTO flow_fields(flow_id, position, field_key, field_value) VALUES(?, ?, ?, ?)",
                (flow_id, position, key, "" if item.get("value") is None else str(item.get("value"))),
            )
            position += 1

    def add_run(self, run: dict) -> dict:
        with self.lock:
            run = copy.deepcopy(run)
            run["id"] = self._write_id("run", run.get("id"))
            run["flowId"] = self.resolve_id("flow", run.get("flowId") or "")
            self.conn.execute(
                """
                INSERT INTO runs(id, flow_id, mode, status, started_at, finished_at, trace, error)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["id"],
                    run["flowId"],
                    run["mode"],
                    run["status"],
                    run["startedAt"],
                    run.get("finishedAt"),
                    json.dumps(run.get("trace") or {}, ensure_ascii=False),
                    run.get("error"),
                ),
            )
            self._commit()
        return run

    def list_runs(self, flow_id: str | None = None, limit: int = 20) -> list[dict]:
        flow_id = self.resolve_id("flow", flow_id) if flow_id else None
        limit = -1 if int(limit) == 0 else max(1, min(int(limit), 100))
        with self.lock:
            if flow_id:
                rows = self.conn.execute(
                    "SELECT * FROM runs WHERE flow_id = ? ORDER BY started_at DESC LIMIT ?",
                    (flow_id, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._run(row) for row in rows]

    def get_run(self, run_id: str) -> dict | None:
        run_id = self.resolve_id("run", run_id)
        with self.lock:
            row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._run(row) if row else None

    def update_run(self, run_id: str, fields: dict) -> dict | None:
        run_id = self.resolve_id("run", run_id)
        current = self.get_run(run_id)
        if current is None:
            return None
        current.update(fields)
        if current["status"] not in {"succeeded", "failed"}:
            raise ValueError("运行状态只能是已完成或失败")
        if current["mode"] not in {"once", "schedule"}:
            raise ValueError("执行方式只能是单次或定时")
        with self.lock:
            self.conn.execute(
                "UPDATE runs SET flow_id = ?, mode = ?, status = ?, started_at = ?, finished_at = ?, trace = ?, error = ? WHERE id = ?",
                (current["flowId"], current["mode"], current["status"], current["startedAt"], current.get("finishedAt"), json.dumps(current.get("trace") or {}, ensure_ascii=False), current.get("error"), run_id),
            )
            self._commit()
        return self.get_run(run_id)

    def output_dirs(self) -> list[str]:
        found = []
        with self.lock:
            rows = self.conn.execute("SELECT trace FROM runs ORDER BY started_at DESC LIMIT 200").fetchall()
        for row in rows:
            try:
                trace = json.loads(row["trace"] or "{}")
            except json.JSONDecodeError:
                continue
            output = trace.get("output") or {}
            for key in ("folder", "zip", "collection"):
                if output.get(key):
                    found.append(output[key])
        return found

    def element_users(self, element_id: str) -> list[str]:
        element_id = self.resolve_id("element", element_id)
        names = []
        for flow in self.list_flows():
            blob = json.dumps(flow.get("graph") or {}, ensure_ascii=False)
            if element_id in blob:
                names.append(flow["name"])
        return names

    def _flow(self, row) -> dict:
        try:
            tags = json.loads(row["tags"] or "[]")
        except (json.JSONDecodeError, IndexError, KeyError):
            tags = []
        field_rows = self.conn.execute(
            "SELECT field_key, field_value FROM flow_fields WHERE flow_id = ? ORDER BY position",
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "group": row["group_name"] or "" if "group_name" in row.keys() else "",
            "tags": tags if isinstance(tags, list) else [],
            "status": row["status"] if "status" in row.keys() and row["status"] else "启用",
            "fields": [{"key": item["field_key"], "value": item["field_value"]} for item in field_rows],
            "graph": json.loads(row["graph"]),
            "schedule": json.loads(row["schedule"]),
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _run(row) -> dict:
        return {
            "id": row["id"],
            "flowId": row["flow_id"],
            "mode": row["mode"],
            "status": row["status"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
            "trace": json.loads(row["trace"] or "{}"),
            "error": row["error"],
        }
