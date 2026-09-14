"""Vetted ComfyUI graph templates and the parameter binding that fills them in.

A template is a ComfyUI API-format graph plus a ``_gx`` metadata block that
declares WHICH node inputs a caller is allowed to influence, by logical name.
Callers never address nodes; they set ``prompt``/``width``/``seed``. Anything
not declared in ``bindings`` is unreachable from the network.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

from .errors import ValidationError


@dataclass(frozen=True)
class Workflow:
    """One vetted graph template."""

    name: str
    kind: str
    title: str
    output_node: str
    bindings: dict[str, tuple[str, str]]
    defaults: dict[str, object]
    graph: dict[str, dict]

    def build(self, params: dict[str, object]) -> dict[str, dict]:
        """Return a concrete ComfyUI graph with ``params`` applied.

        Unknown parameter names are a programming error in this service, not a
        caller error: the HTTP layer has already normalised the request.
        """
        graph = copy.deepcopy(self.graph)
        merged: dict[str, object] = dict(self.defaults)
        merged.update({k: v for k, v in params.items() if v is not None})

        for logical, value in merged.items():
            target = self.bindings.get(logical)
            if target is None:
                continue
            node_id, field = target
            node = graph.get(node_id)
            if node is None:  # pragma: no cover - guarded by load-time check
                raise KeyError(f"workflow {self.name}: binding {logical} -> missing node {node_id}")
            node["inputs"][field] = value
        return graph


def _parse_binding(name: str, spec: str) -> tuple[str, str]:
    node_id, _, field = spec.partition(".")
    if not node_id or not field:
        raise ValueError(f"binding {name!r}: expected 'NODE.input', got {spec!r}")
    return node_id, field


def load_workflow(path: Path) -> Workflow:
    """Load and structurally validate one template file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    meta = raw.pop("_gx", None)
    if not isinstance(meta, dict):
        raise ValueError(f"{path}: missing '_gx' metadata block")

    kind = meta.get("kind")
    if kind not in {"image", "video"}:
        raise ValueError(f"{path}: _gx.kind must be 'image' or 'video', got {kind!r}")

    output_node = str(meta.get("output_node", ""))
    if output_node not in raw:
        raise ValueError(f"{path}: _gx.output_node {output_node!r} is not a node in the graph")

    bindings: dict[str, tuple[str, str]] = {}
    for logical, spec in (meta.get("bindings") or {}).items():
        node_id, field = _parse_binding(logical, str(spec))
        if node_id not in raw:
            raise ValueError(f"{path}: binding {logical} -> unknown node {node_id}")
        if field not in raw[node_id].get("inputs", {}):
            raise ValueError(f"{path}: binding {logical} -> node {node_id} has no input {field!r}")
        bindings[logical] = (node_id, field)

    for node_id, node in raw.items():
        if "class_type" not in node or "inputs" not in node:
            raise ValueError(f"{path}: node {node_id} is not a valid API-format node")

    return Workflow(
        name=path.name.removesuffix(".api.json"),
        kind=kind,
        title=str(meta.get("title", path.stem)),
        output_node=output_node,
        bindings=bindings,
        defaults=dict(meta.get("defaults") or {}),
        graph=raw,
    )


class WorkflowRegistry:
    """All templates found in ``directory``, loaded and validated at start-up."""

    def __init__(self, directory: Path) -> None:
        self._workflows: dict[str, Workflow] = {}
        for path in sorted(directory.glob("*.api.json")):
            workflow = load_workflow(path)
            self._workflows[workflow.name] = workflow
        if not self._workflows:
            raise SystemExit(f"no *.api.json workflow templates found in {directory}")

    def __contains__(self, name: object) -> bool:
        return name in self._workflows

    def names(self) -> list[str]:
        return sorted(self._workflows)

    def get(self, name: str) -> Workflow:
        try:
            return self._workflows[name]
        except KeyError:
            raise ValidationError(
                f"unknown workflow {name!r}; available: {', '.join(self.names())}",
                param="workflow",
            ) from None

    def of_kind(self, kind: str) -> list[Workflow]:
        return [w for w in self._workflows.values() if w.kind == kind]
