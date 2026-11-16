"""Local workbench. Bind to 127.0.0.1 only."""

from __future__ import annotations

import json
import os
import sys
import uuid
from tempfile import SpooledTemporaryFile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from app import __version__
from app.catalog import BLANK_SCHEDULE, ELEMENT_DEFAULTS, ELEMENT_TYPES, NODE_TYPES, blank_node, blank_payload
from app.carry import FlowError
from app.engine import Busy, Engine, cleanup_work
from app.batch_runs import BatchRunner
from app.execution_config import CONFIG_KEY, get_execution_config, save_execution_config
from app.ids import new_id
from app.image_sizes import IMAGE_SIZE_PRESETS
from app.id_migration import migrate_uuid_ids
from app.packages import build_flow_batch, build_flow_package, build_library_package, import_package, write_example_if_missing
from app.paths import PROJECT_ROOT, data_home, resolve_path
from app.providers import KeyPool, ProviderError, chat
from app.reference_import import MAX_ZIP_BYTES, import_reference_zip
from app.scheduler import Scheduler
from app.schedule import normalize_schedule
from app.seed import seed_if_empty
from app.store import Store
from app.tables import export_table, import_table

WEB_ROOT = PROJECT_ROOT / "web"
MAX_JSON = 20_000_000
MAX_UPLOAD = 15_000_000


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    store = Store(data_home() / "app.sqlite3")
    seed_if_empty(store)
    migrate_uuid_ids(store)
    write_example_if_missing(store)
    cleanup_work()
    if not store.get_setting(CONFIG_KEY):
        save_execution_config(store, get_execution_config(store))
    engine = Engine(store)
    batches = BatchRunner(store, engine)
    batches.start()
    scheduler = Scheduler(store, engine)
    scheduler.start()
    port = int(os.environ.get("MP_CONTENT_PORT") or 8765)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, engine, batches))
    print(json.dumps({"ready": True, "port": port}), flush=True)
    if os.environ.get("MP_CONTENT_DESKTOP") == "1":
        print(f"壁纸生创流程台  WallpaperFlowStudio {__version__}  桌面窗口使用 http://127.0.0.1:{port}", flush=True)
    else:
        print(f"壁纸生创流程台  WallpaperFlowStudio {__version__}")
        print(f"打开  http://127.0.0.1:{port}")
        print("在这个窗口按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("已停止")
    finally:
        batches.stop.set()
        batches.wake.set()
        scheduler.stop.set()
        server.server_close()
        store.close()


def make_handler(store: Store, engine: Engine, batches: BatchRunner | None = None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            handle(self, store, engine, batches)

        def do_POST(self):
            handle(self, store, engine, batches)

        def do_PUT(self):
            handle(self, store, engine, batches)

        def do_DELETE(self):
            handle(self, store, engine, batches)

        def log_message(self, fmt, *args):
            print("%s %s" % (self.address_string(), fmt % args))

    return Handler


def handle(handler: BaseHTTPRequestHandler, store: Store, engine: Engine, batches: BatchRunner | None = None) -> None:
    parsed = urlparse(handler.path)
    path = unquote(parsed.path)
    query = parse_qs(parsed.query)
    method = handler.command
    try:
        if method == "GET" and path in ("/", "/index.html"):
            return send_file(handler, WEB_ROOT / "index.html", "text/html; charset=utf-8")
        if method == "GET" and path in ("/app.js", "/styles.css"):
            kind = "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8"
            return send_file(handler, WEB_ROOT / path.lstrip("/"), kind)
        if method == "GET" and path == "/favicon.ico":
            return send_file(handler, PROJECT_ROOT / "assets" / "icon.ico", "image/x-icon")
        if method == "GET" and path == "/app-icon.svg":
            return send_file(handler, PROJECT_ROOT / "assets" / "icon.svg", "image/svg+xml")
        if method == "GET" and path.startswith("/media/"):
            return send_media(handler, path[len("/media/") :])
        if method == "GET" and path == "/api/health":
            return send_json(handler, 200, {"ok": True, "version": __version__})
        if path == '/api/execution-config' and method in {'GET', 'PUT'}:
            config = get_execution_config(store) if method == 'GET' else save_execution_config(store, read_json(handler))
            if batches is not None:
                batches.wake.set()
            return send_json(handler, 200, {'config': config})
        if path == "/api/batch-runs" and method in {"GET", "POST"}:
            if batches is None:
                raise ValueError("批量执行服务未启动，请重新打开程序。")
            if method == "GET":
                return send_json(handler, 200, {"batches": batches.list()})
            return send_json(handler, 200, {"batch": batches.create(read_json(handler))})
        if method == "POST" and path.startswith("/api/batch-runs/") and path.endswith("/cancel"):
            if batches is None:
                raise ValueError("批量执行服务未启动，请重新打开程序。")
            return send_json(handler, 200, {"batch": batches.cancel(path.split("/")[3])})
        if method == "GET" and path == "/api/bootstrap":
            return send_json(handler, 200, bootstrap(store))
        if method == "POST" and path == "/api/reference-images/import-zip":
            length = int(handler.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_ZIP_BYTES:
                raise ValueError("ZIP 为空或超过 200MB")
            name = (query.get("name") or [""])[0]
            if Path(name).suffix.lower() != ".zip":
                raise ValueError("请选择 ZIP 压缩包")
            group = (query.get("group") or [""])[0]
            tags = json.loads((query.get("tags") or ["[]"])[0])
            note = (query.get("note") or [""])[0]
            with SpooledTemporaryFile(max_size=8_000_000) as uploaded:
                remaining = length
                while remaining:
                    chunk = handler.rfile.read(min(remaining, 1_048_576))
                    if not chunk:
                        raise ValueError("ZIP 上传不完整，请重新导入")
                    uploaded.write(chunk)
                    remaining -= len(chunk)
                uploaded.seek(0)
                result = import_reference_zip(store, uploaded, group, tags, note)
            return send_json(handler, 200, result)
        if method == "POST" and path == "/api/tables/export":
            data, filename, content_type = export_table(store, read_json(handler))
            handler.send_response(200)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename))
            handler.send_header("Content-Length", str(len(data)))
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            handler.wfile.write(data)
            return
        if method == "POST" and path == "/api/tables/import":
            length = int(handler.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_JSON:
                raise ValueError("表格为空或超过 20MB")
            kind = (query.get("kind") or [""])[0]
            element_type = (query.get("type") or [""])[0]
            filename = (query.get("name") or [""])[0]
            return send_json(handler, 200, import_table(store, kind, element_type, handler.rfile.read(length), filename))
        if method == "GET" and path == "/api/results":
            limit = int((query.get("limit") or ["500"])[0])
            group = (query.get("group") or [""])[0]
            unused = (query.get("unused") or [""])[0] in {"1", "true"}
            return send_json(handler, 200, {"results": store.list_results(limit, group, unused)})
        if method == "POST" and path == "/api/results":
            return create_result(handler, store, read_json(handler))
        if method == "POST" and path == "/api/results/batch":
            return batch_results(handler, store, read_json(handler))
        if method == "PUT" and path.startswith("/api/results/"):
            result_id = path.split("/")[3]
            updated = store.update_result(result_id, read_json(handler))
            if updated is None:
                return send_json(handler, 404, {"error": "结果不存在"})
            return send_json(handler, 200, updated)
        if method == "GET" and path == "/api/runs":
            flow_id = (query.get("flowId") or [""])[0] or None
            limit = int((query.get("limit") or ["20"])[0])
            return send_json(handler, 200, {"runs": store.list_runs(flow_id, limit)})
        if method == "POST" and path == "/api/runs":
            body = read_json(handler)
            body["id"] = new_id("run")
            from app.store import now_iso
            body.setdefault("startedAt", now_iso())
            body.setdefault("flowId", "")
            body.setdefault("mode", "once")
            body.setdefault("status", "succeeded")
            if body["status"] not in {"succeeded", "failed"} or body["mode"] not in {"once", "schedule"}:
                raise ValueError("运行方式或状态不正确")
            return send_json(handler, 200, store.add_run(body))
        if method == "PUT" and path.startswith("/api/runs/"):
            updated = store.update_run(path.split("/")[3], read_json(handler))
            return send_json(handler, 200 if updated else 404, updated or {"error": "记录不存在"})
        if method == "GET" and path == "/api/library/package":
            return send_json(handler, 200, build_library_package(store))
        if method == "GET" and path == "/api/flows/batch-package":
            raw_ids = (query.get("ids") or [""])[0]
            flow_ids = [item for item in raw_ids.split(",") if item] or None
            return send_json(handler, 200, build_flow_batch(store, flow_ids))
        if method == "GET" and path.startswith("/api/flows/") and path.endswith("/package"):
            flow_id = path.split("/")[3]
            flow = store.get_flow(flow_id)
            if flow is None:
                return send_json(handler, 404, {"error": "流程不存在"})
            return send_json(handler, 200, build_flow_package(store, flow))
        if method == "POST" and path == "/api/flows":
            body = read_json(handler)
            flow_id = new_id("flow")
            flow = store.upsert_flow(
                {
                    "id": flow_id,
                    "name": (body.get("name") or "未命名流程").strip() or "未命名流程",
                    "description": body.get("description") or "",
                    "group": body.get("group") or "",
                    "tags": body.get("tags") if isinstance(body.get("tags"), list) else [],
                    "status": body.get("status") or "启用",
                    "fields": body.get("fields") if isinstance(body.get("fields"), list) else [],
                    "graph": {"nodes": [blank_node("start.theme", new_id("node"), 80, 160)], "edges": []},
                    "schedule": dict(BLANK_SCHEDULE),
                }
            )
            return send_json(handler, 200, flow)
        if method == "POST" and path == "/api/flows/copy":
            body = read_json(handler)
            copied = store.copy_flows(body.get("ids"), body.get("count"), body.get("keepSchedule", False))
            return send_json(handler, 200, {"ok": True, "count": len(copied), "flows": copied})
        if method == "PUT" and path.startswith("/api/flows/"):
            flow_id = path.split("/")[3]
            current = store.get_flow(flow_id)
            if current is None:
                return send_json(handler, 404, {"error": "流程不存在"})
            body = read_json(handler)
            flow = store.upsert_flow(
                {
                    "id": flow_id,
                    "name": (body.get("name") or "未命名流程").strip() or "未命名流程",
                    "description": body.get("description") if "description" in body else current.get("description") or "",
                    "group": body.get("group") if "group" in body else current.get("group") or "",
                    "tags": body.get("tags") if isinstance(body.get("tags"), list) else current.get("tags") or [],
                    "status": body.get("status") or current.get("status") or "启用",
                    "fields": body.get("fields") if isinstance(body.get("fields"), list) else current.get("fields") or [],
                    "graph": body.get("graph") or current.get("graph") or {"nodes": [], "edges": []},
                    "schedule": normalize_schedule(body.get("schedule") or current.get("schedule") or {}),
                }
            )
            return send_json(handler, 200, flow)
        if method == "DELETE" and path.startswith("/api/flows/"):
            store.delete_flow(path.split("/")[3])
            return send_json(handler, 200, {"ok": True})
        if method == "POST" and path.startswith("/api/flows/") and path.endswith("/run"):
            flow_id = path.split("/")[3]
            body = read_json(handler)
            run = engine.run(flow_id, mode="once", overrides={"themeText": body.get("themeText") or ""})
            return send_json(handler, 200, run)
        if method == "POST" and path == "/api/elements/batch":
            return batch_elements(handler, store, read_json(handler))
        if method == "POST" and path == "/api/flows/batch":
            return batch_flows(handler, store, read_json(handler))
        if method == "POST" and path == "/api/runs/batch":
            body = read_json(handler)
            ids = body.get("ids") if isinstance(body.get("ids"), list) else []
            if body.get("action") == "delete":
                count = store.delete_runs([str(item) for item in ids])
            elif body.get("action") == "status":
                status = body.get("status")
                if status not in {"succeeded", "failed"}:
                    raise ValueError("运行状态不正确")
                count = sum(store.update_run(str(item), {"status": status}) is not None for item in dict.fromkeys(ids))
            else:
                raise ValueError("不认识的批量操作")
            return send_json(handler, 200, {"ok": True, "count": count})
        if method == "POST" and path == "/api/elements":
            body = read_json(handler)
            element_type = body.get("type")
            if element_type not in ELEMENT_DEFAULTS:
                return send_json(handler, 400, {"error": "不认识的元素类型"})
            element = store.upsert_element(
                {
                    "id": new_id(element_type),
                    "type": element_type,
                    "name": (body.get("name") or "未命名").strip() or "未命名",
                    "group": body.get("group") or "",
                    "tags": body.get("tags") if isinstance(body.get("tags"), list) else [],
                    "status": body.get("status") or "启用",
                    "fields": body.get("fields") if isinstance(body.get("fields"), list) else [],
                    "payload": body.get("payload") if isinstance(body.get("payload"), dict) else blank_payload(element_type),
                }
            )
            return send_json(handler, 200, element)
        if method == "PUT" and path.startswith("/api/elements/"):
            element_id = path.split("/")[3]
            current = store.get_element(element_id)
            if current is None:
                return send_json(handler, 404, {"error": "元素不存在"})
            body = read_json(handler)
            element = store.upsert_element(
                {
                    "id": element_id,
                    "type": body.get("type") or current["type"],
                    "name": (body.get("name") or current["name"]).strip() or current["name"],
                    "group": body.get("group") if "group" in body else current.get("group") or "",
                    "tags": body.get("tags") if isinstance(body.get("tags"), list) else current.get("tags") or [],
                    "status": body.get("status") or current.get("status") or "启用",
                    "fields": body.get("fields") if isinstance(body.get("fields"), list) else current.get("fields") or [],
                    "payload": body.get("payload") if isinstance(body.get("payload"), dict) else current["payload"],
                }
            )
            return send_json(handler, 200, element)
        if method == "DELETE" and path.startswith("/api/elements/"):
            element_id = path.split("/")[3]
            users = store.element_users(element_id)
            force = (query.get("force") or ["0"])[0] == "1"
            if users and not force:
                return send_json(handler, 409, {"error": "还有流程在用这个元素", "flows": users})
            store.delete_element(element_id)
            return send_json(handler, 200, {"ok": True})
        if method == "POST" and path.startswith("/api/elements/") and path.endswith("/secret"):
            element_id = path.split("/")[3]
            if store.get_element(element_id) is None:
                return send_json(handler, 404, {"error": "元素不存在"})
            body = read_json(handler)
            if "removeIndex" in body:
                store.remove_secret_at(element_id, int(body.get("removeIndex") or 0))
            elif isinstance(body.get("appendKeys"), list):
                store.append_secrets(element_id, [str(item) for item in body["appendKeys"]])
            elif isinstance(body.get("apiKeys"), list):
                store.set_secrets(element_id, [str(item) for item in body["apiKeys"]])
            else:
                store.set_secret(element_id, str(body.get("apiKey") or ""))
            return send_json(handler, 200, {"ok": True, "keyStatus": store.secret_status().get(element_id) or {"hasKey": False, "count": 0, "last4": "", "keys": []}})
        if method == "POST" and path.startswith("/api/elements/") and path.endswith("/test"):
            element_id = path.split("/")[3]
            element = store.get_element(element_id)
            if element is None or element["type"] != "model_profile":
                return send_json(handler, 404, {"error": "模型配置不存在"})
            if element["payload"].get("kind") != "text":
                return send_json(handler, 400, {"error": "这里只测试文本模型。生图模型请直接跑一次流程。"})
            pool = KeyPool(store.get_secrets(element_id))
            reply = pool.call(lambda key: chat(element, key, "只回复两个字母 OK"))
            return send_json(handler, 200, {"ok": True, "reply": reply[:200]})
        if method == "POST" and path == "/api/packages":
            result = import_package(store, read_json(handler))
            return send_json(handler, 200, result)
        if method == "POST" and path == "/api/uploads":
            return upload(handler, query)
        if method == "POST" and path == "/api/open":
            body = read_json(handler)
            target = str(body.get("path") or "")
            resolved = open_target(target)
            if resolved is None or not allowed_open(store, target):
                return send_json(handler, 403, {"error": "这个路径不能从这里打开"})
            os.startfile(str(resolved))  # noqa: S606 - local workbench, path checked above
            return send_json(handler, 200, {"ok": True})
        send_json(handler, 404, {"error": "没有这个地址"})
    except Busy as exc:
        send_json(handler, 409, {"error": str(exc)})
    except (FlowError, ProviderError, ValueError, json.JSONDecodeError) as exc:
        send_json(handler, 400, {"error": str(exc)})
    except Exception as exc:
        send_json(handler, 500, {"error": str(exc)})


def bootstrap(store: Store) -> dict:
    return {
        "version": __version__,
        "nodeTypes": NODE_TYPES,
        "elementTypes": ELEMENT_TYPES,
        "elementDefaults": ELEMENT_DEFAULTS,
        "imageSizePresets": IMAGE_SIZE_PRESETS,
        "elements": store.list_elements(),
        "flows": store.list_flows(),
        "keyStatus": store.secret_status(),
        "executionConfig": get_execution_config(store),
    }


def batch_elements(handler: BaseHTTPRequestHandler, store: Store, body: dict) -> None:
    ids = [str(item) for item in body.get("ids") or [] if str(item).strip()]
    if body.get("action") != "status":
        return send_json(handler, 400, {"error": "不认识的批量操作"})
    count = store.set_elements_status(ids, str(body.get("status") or ""))
    return send_json(handler, 200, {"ok": True, "count": count})


def batch_flows(handler: BaseHTTPRequestHandler, store: Store, body: dict) -> None:
    ids = [str(item) for item in body.get("ids") or [] if str(item).strip()]
    if body.get("action") != "status":
        return send_json(handler, 400, {"error": "不认识的批量操作"})
    count = store.set_flows_status(ids, str(body.get("status") or ""))
    return send_json(handler, 200, {"ok": True, "count": count})


def create_result(handler: BaseHTTPRequestHandler, store: Store, body: dict) -> None:
    source = str(body.get("sourcePath") or "").strip()
    resolved = open_target(source)
    uploads = (data_home() / "uploads").resolve()
    if resolved is None or not resolved.is_file() or uploads not in resolved.parents:
        return send_json(handler, 400, {"error": "请先上传图片"})
    result_id = new_id("result")
    suffix = resolved.suffix.lower() or ".png"
    relative = f"results/{result_id}{suffix}"
    dest = resolve_path(relative)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resolved.read_bytes())
    tags = body.get("tags") if isinstance(body.get("tags"), list) else []
    record = store.save_result(
        {
            "id": result_id,
            "name": str(body.get("name") or resolved.stem or "结果图").strip() or "结果图",
            "group": str(body.get("group") or ""),
            "tags": [str(item).strip() for item in tags if str(item).strip()],
            "referenceGroup": str(body.get("group") or ""),
            "referenceTags": [str(item).strip() for item in tags if str(item).strip()],
            "path": relative,
            "prompt": str(body.get("prompt") or ""),
        }
    )
    worked = bool(body.get("worked"))
    if worked:
        store.set_results_status([result_id], True)
        record = store.get_result(result_id) or record
    return send_json(handler, 200, record)


def batch_results(handler: BaseHTTPRequestHandler, store: Store, body: dict) -> None:
    ids = body.get("ids") if isinstance(body.get("ids"), list) else []
    action = str(body.get("action") or "")
    if action == "delete":
        paths = store.delete_results([str(item) for item in ids])
        home = data_home().resolve()
        for raw in paths:
            target = open_target(raw)
            if target and target.is_file() and home in target.parents and "results" in target.parts:
                target.unlink()
        return send_json(handler, 200, {"ok": True, "count": len(paths)})
    if action == "status":
        count = store.set_results_status([str(item) for item in ids], bool(body.get("worked")))
        return send_json(handler, 200, {"ok": True, "count": count})
    if action == "update":
        fields = {}
        if "group" in body:
            fields["group"] = body.get("group") or ""
        if isinstance(body.get("tags"), list):
            fields["tags"] = body.get("tags")
        if "worked" in body:
            fields["worked"] = bool(body.get("worked"))
        updated = []
        for item in ids:
            row = store.update_result(str(item), fields)
            if row:
                updated.append(row)
        return send_json(handler, 200, {"ok": True, "count": len(updated)})
    return send_json(handler, 400, {"error": "不认识的批量操作"})


def open_target(raw: str) -> Path | None:
    text = (raw or "").strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        try:
            return resolve_path(text)
        except ValueError:
            return None
    try:
        return path.resolve()
    except OSError:
        return None


def allowed_open(store: Store, raw: str) -> bool:
    target = open_target(raw)
    if target is None or target.suffix == ".sqlite3":
        return False
    if not target.exists():
        if target == data_home() / "results":
            target.mkdir(parents=True, exist_ok=True)
        else:
            return False
    home = data_home()
    if target == home or home in target.parents:
        return True
    candidates = []
    for recorded in store.output_dirs():
        try:
            candidates.append(Path(recorded).resolve())
        except OSError:
            continue
    for element in store.list_elements():
        if element["type"] != "output_target":
            continue
        directory = (element.get("payload") or {}).get("directory")
        if not directory:
            continue
        try:
            candidates.append(resolve_path(str(directory)))
        except ValueError:
            continue
    for candidate in candidates:
        if target == candidate or candidate in target.parents or target in candidate.parents:
            return True
    return False


def upload(handler: BaseHTTPRequestHandler, query: dict) -> None:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0 or length > MAX_UPLOAD:
        return send_json(handler, 400, {"error": "文件为空或超过 15MB"})
    original = Path((query.get("name") or ["upload.bin"])[0]).name
    suffix = Path(original).suffix.lower()[:8]
    if suffix and not all(ch.isalnum() or ch == "." for ch in suffix):
        suffix = ""
    name = uuid.uuid4().hex[:10] + suffix
    folder = data_home() / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    target.write_bytes(handler.rfile.read(length))
    send_json(handler, 200, {"path": f"uploads/{name}"})


def send_media(handler: BaseHTTPRequestHandler, relative: str) -> None:
    relative = relative.replace("\\", "/").lstrip("/")
    if ".." in relative.split("/"):
        return send_json(handler, 403, {"error": "路径不对"})
    if relative.startswith("resources/"):
        root = PROJECT_ROOT
    elif relative.startswith(("uploads/", "outputs/", "work/", "results/")):
        root = data_home()
        relative = relative
    else:
        return send_json(handler, 403, {"error": "这个文件不能预览"})
    target = (root / relative).resolve()
    if root.resolve() not in target.parents or not target.is_file():
        return send_json(handler, 404, {"error": "文件不存在"})
    if target.suffix == ".sqlite3":
        return send_json(handler, 403, {"error": "这个文件不能预览"})
    kind = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".json": "application/json; charset=utf-8",
        ".md": "text/plain; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
    }.get(target.suffix.lower(), "application/octet-stream")
    send_file(handler, target, kind)


def read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length > MAX_JSON:
        raise FlowError("请求太大")
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise FlowError("请求体要是一个对象")
    return data


def send_json(handler: BaseHTTPRequestHandler, code: int, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def send_file(handler: BaseHTTPRequestHandler, path: Path, content_type: str) -> None:
    if not path.is_file():
        return send_json(handler, 404, {"error": "文件不存在"})
    data = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)
