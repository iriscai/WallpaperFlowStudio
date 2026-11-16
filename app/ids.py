"""Typed UUIDs and graph identity remapping."""

from __future__ import annotations

import copy
import uuid

PREFIXES = {
    "flow": "flow", "run": "run", "result": "pic", "reference_image": "pic",
    "reference_article": "article", "reference_copy": "copy", "prompt": "prompt",
    "domain": "domain", "theme": "theme", "image_config_group": "image_config",
    "model_profile": "model", "output_target": "output", "rule": "rule",
    "node": "node", "edge": "edge", "work": "work",
}


def new_id(kind: str) -> str:
    return PREFIXES[kind] + "_" + str(uuid.uuid4())


def is_uuid_id(kind: str, value: str) -> bool:
    prefix = PREFIXES[kind] + "_"
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    try:
        parsed = uuid.UUID(value[len(prefix):])
        return parsed.version == 4 and str(parsed) == value[len(prefix):]
    except ValueError:
        return False


def remap_graph(graph: dict, node_id, edge_id, element_id) -> dict:
    graph = copy.deepcopy(graph)
    nodes = graph.get("nodes") or []
    mapping = {node["id"]: node_id(node["id"]) for node in nodes}
    for node in nodes:
        node["id"] = mapping[node["id"]]
        for imported in node.get("imports") or []:
            if imported.get("elementId"):
                imported["elementId"] = element_id(imported["elementId"], imported.get("elementType"))
        for bucket in ("rulesBefore", "rulesAfter"):
            if bucket in node:
                node[bucket] = [element_id(value, "rule") for value in node[bucket]]
    for edge in graph.get("edges") or []:
        edge["id"] = edge_id(edge["id"])
        for endpoint in ("from", "to"):
            old = edge.get(endpoint)
            if old not in mapping:
                mapping[old] = node_id(old)
            edge[endpoint] = mapping[old]
        if "rules" in edge:
            edge["rules"] = [element_id(value, "rule") for value in edge["rules"]]
    return graph


def copy_graph(graph: dict) -> dict:
    return remap_graph(graph, lambda _: new_id("node"), lambda _: new_id("edge"), lambda value, _: value)
