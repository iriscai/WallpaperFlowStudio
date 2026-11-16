"""Run a flow in topological order with explicit field carry and named parameters."""

from __future__ import annotations

import copy
import re
import shutil
import threading
from datetime import datetime, timezone

from app.carry import FlowError, apply_carry, apply_rule, field_path, get_path, merge_docs
from app.ids import new_id
from app.nodes import Ctx, run_node
from app.execution_config import CONFIG_KEY, get_execution_config
from app.paths import data_home
from app.schedule import mark_after_run
from app.store import Store, now_iso


class Busy(Exception):
    pass


class Engine:
    def __init__(self, store: Store):
        self.store = store
        self.lock = threading.Lock()
        self._active_flows = set()

    def capacity(self):
        with self.lock:
            return max(0, get_execution_config(self.store)['flowConcurrency'] - len(self._active_flows))

    def run(self, flow_id: str, mode: str = "once", overrides: dict | None = None) -> dict:
        if mode not in ("once", "schedule"):
            raise FlowError("执行方式只能是单次或定时")
        flow_id = self.store.resolve_id('flow', flow_id)
        with self.lock:
            if flow_id in self._active_flows:
                raise Busy("这个流程已在执行，等待完成后再启动下一次。")
            if len(self._active_flows) >= get_execution_config(self.store)['flowConcurrency']:
                raise Busy("流程并发数已满，等待空闲位置。")
            self._active_flows.add(flow_id)
        try:
            return self._run(flow_id, mode, overrides or {})
        finally:
            with self.lock:
                self._active_flows.discard(flow_id)

    def _run(self, flow_id: str, mode: str, overrides: dict) -> dict:
        flow = self.store.get_flow(flow_id)
        if flow is None:
            raise FlowError("流程不存在")
        flow_id = flow["id"]
        if flow.get("status") == "停用":
            raise FlowError("这个流程已停用。")
        started = now_iso()
        run_id = new_id("run")
        scratch = data_home() / "work" / run_id
        scratch.mkdir(parents=True, exist_ok=True)
        trace = {"order": [], "nodes": {}, "output": {}}
        status = "succeeded"
        error = None
        execution_config = get_execution_config(self.store) if self.store.get_setting(CONFIG_KEY) else None
        try:
            graph = copy.deepcopy(flow["graph"] or {})
            nodes = graph.get("nodes") or []
            edges = graph.get("edges") or []
            if not nodes:
                raise FlowError("流程里还没有节点")
            order = _ordered(nodes, edges)
            trace["order"] = order
            by_id = {node["id"]: node for node in nodes}
            exports: dict[str, dict] = {}
            carried_parameters: dict[str, dict] = {}
            key_pools = {}
            for node_id in order:
                node = by_id[node_id]
                if node.get("type") == "start.theme" and str(overrides.get("themeText") or "").strip():
                    node = copy.deepcopy(node)
                    params = dict(node.get("params") or {})
                    params["themeText"] = str(overrides["themeText"]).strip()
                    node["params"] = params
                inlet = _inlet(self.store, node_id, edges, exports, carried_parameters)
                elements, missing = _imports(self.store, node)
                inlet, before_message = _rules(self.store, inlet, node.get("rulesBefore") or [])
                ctx = Ctx(
                    node=node,
                    inlet=inlet,
                    elements=elements,
                    by_type=_group(elements),
                    flow=flow,
                    run_id=run_id,
                    key_for=self.store.get_secret,
                    keys_for=self.store.get_secrets,
                    scratch=scratch,
                    mark_used=self.store.mark_reference_used,
                    save_result=self.store.save_result,
                    list_results=self.store.list_results,
                    mark_results_worked=self.store.mark_results_worked,
                    key_pools=key_pools,
                    execution_config=execution_config,
                )
                result = run_node(ctx)
                export = result.export
                if (node.get("params") or {}).get("keepInlet"):
                    merged = copy.deepcopy(inlet)
                    merged.update(export)
                    export = merged
                names = {key for edge in edges if edge.get("to") == node_id for key in carried_parameters.get(edge.get("from"), {})}
                for edge in edges:
                    if edge.get("to") != node_id:
                        continue
                    for mapping in edge.get("carry") or []:
                        if not isinstance(mapping, dict):
                            continue
                        name = field_path(mapping.get("target"))
                        if re.fullmatch(r"[^\W\d]\w*", name) and (
                            mapping.get("op") == "template"
                            or field_path(mapping.get("source")) != name
                        ):
                            names.add(name)
                for name in names:
                    if name in inlet:
                        export.setdefault(name, copy.deepcopy(inlet[name]))
                bindings = (node.get("params") or {}).get("carryParams") or []
                context = merge_docs(copy.deepcopy(inlet), export)
                for binding in bindings:
                    literal = binding.get("sourceType") == "literal"
                    source = field_path(binding.get("source"))
                    name = field_path(binding.get("target"))
                    if not literal and not source and not name:
                        continue
                    if not re.fullmatch(r"[^\W\d]\w*", name) or (not literal and not source):
                        raise FlowError("携带参数需要来源字段或固定文案，以及有效参数名，例如 title 或 ${title}。")
                    if literal:
                        value = binding.get("value")
                        if not isinstance(value, str) or not value.strip():
                            raise FlowError(f"携带参数 {name} 的固定文案不能为空，请填写文案。")
                    else:
                        value = get_path(context, source)
                        if value is None:
                            raise FlowError(f"携带参数 {name} 的来源字段 {source} 不存在。")
                    export[name] = copy.deepcopy(value)
                    context[name] = copy.deepcopy(value)
                    names.add(name)
                export, _after = _rules(self.store, export, node.get("rulesAfter") or [])
                carried_parameters[node_id] = {name: copy.deepcopy(export[name]) for name in names if name in export}
                message = result.message
                if missing:
                    message = f"{message} 缺少元素：{', '.join(missing)}".strip()
                exports[node_id] = export
                trace["nodes"][node_id] = {
                    "status": "succeeded",
                    "name": node.get("name") or node_id,
                    "type": node.get("type"),
                    "provider": result.provider,
                    "message": message,
                    "inlet": inlet,
                    "export": export,
                }
                output = export.get("output") if isinstance(export, dict) else None
                if isinstance(output, dict) and (output.get("folder") or output.get("zip")):
                    trace["output"] = {"folder": output.get("folder") or "", "zip": output.get("zip") or ""}
        except Exception as exc:
            status = "failed"
            error = str(exc) or exc.__class__.__name__
            failed_id = trace["order"][len(trace["nodes"])] if len(trace["nodes"]) < len(trace["order"]) else ""
            if failed_id and failed_id not in trace["nodes"]:
                node = next((item for item in flow["graph"].get("nodes") or [] if item.get("id") == failed_id), {})
                trace["nodes"][failed_id] = {
                    "status": "failed",
                    "name": node.get("name") or failed_id,
                    "type": node.get("type") or "",
                    "provider": "",
                    "message": error,
                    "inlet": {},
                    "export": {},
                }
        finished = now_iso()
        run = {
            "id": run_id,
            "flowId": flow_id,
            "mode": mode,
            "status": status,
            "startedAt": started,
            "finishedAt": finished,
            "trace": trace,
            "error": error,
        }
        self.store.add_run(run)
        if mode == "schedule":
            current = self.store.get_flow(flow_id)
            if current is not None:
                self.store.update_schedule(flow_id, mark_after_run(current["schedule"], datetime.now().astimezone()))
        return run


def _ordered(nodes: list, edges: list) -> list[str]:
    by_id = {node["id"]: node for node in nodes}
    indegree = {node["id"]: 0 for node in nodes}
    outgoing = {node["id"]: [] for node in nodes}
    for edge in edges:
        source = edge.get("from")
        target = edge.get("to")
        if source not in by_id or target not in by_id:
            raise FlowError("有一条连线指向了不存在的节点")
        if source == target:
            raise FlowError("节点不能连接自己")
        outgoing[source].append(target)
        indegree[target] += 1
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    order = []
    while ready:
        ready.sort(key=lambda node_id: (by_id[node_id].get("x") or 0, node_id))
        node_id = ready.pop(0)
        order.append(node_id)
        for nxt in outgoing[node_id]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
    if len(order) != len(nodes):
        raise FlowError("流程里有循环连线，没法从头执行")
    return order


def _inlet(store: Store, node_id: str, edges: list, exports: dict, parameters: dict | None = None) -> dict:
    inlet: dict = {}
    for edge in edges:
        if edge.get("to") != node_id:
            continue
        upstream = exports.get(edge.get("from")) or {}
        rules = []
        for rule_id in edge.get("rules") or []:
            element = store.get_element(rule_id)
            if element is None:
                raise FlowError(f"连线上的规则不存在：{rule_id}")
            if element["type"] != "rule":
                raise FlowError(f"「{element['name']}」不是规则")
            rules.append(element["payload"])
        fragment = apply_carry(upstream, edge.get("carry") or [], rules, (parameters or {}).get(edge.get("from")))
        merge_docs(inlet, fragment)
    return inlet


def _imports(store: Store, node: dict) -> tuple[list, list[str]]:
    elements = []
    missing = []
    for item in node.get("imports") or []:
        element_id = item.get("elementId")
        if not element_id:
            continue
        element = store.get_element(element_id)
        if element is None:
            missing.append(str(element_id))
            continue
        elements.append(element)
    return elements, missing


def _rules(store: Store, document: dict, rule_ids: list) -> tuple[dict, str]:
    current = document
    for rule_id in rule_ids or []:
        element = store.get_element(rule_id)
        if element is None:
            raise FlowError(f"规则不存在：{rule_id}")
        current, blocked = apply_rule(current, element.get("payload") or {})
        if blocked:
            raise FlowError(blocked)
    return current, ""


def _group(elements: list) -> dict:
    grouped: dict[str, list] = {}
    for element in elements:
        grouped.setdefault(element["type"], []).append(element)
    return grouped


def cleanup_work(days: int = 7) -> None:
    root = data_home() / "work"
    if not root.exists():
        return
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    for child in root.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue
