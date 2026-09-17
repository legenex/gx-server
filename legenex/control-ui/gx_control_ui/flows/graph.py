"""Pure graph algorithms for Creative Flows (no I/O).

* ``Graph.topo_order``  deterministic Kahn ordering; raises CycleError with the
  nodes that remain in a cycle.
* ``upstream`` / ``downstream``  transitive closures.
* ``plan``  which nodes a run mode touches.
* ``runnable``  which pending nodes may start now (all dependencies terminal).
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable

#: Node states that count as finished for dependency purposes.
TERMINAL = frozenset({"succeeded", "cached", "failed", "cancelled", "skipped", "bypassed", "interrupted",
                      "reused", "blocked"})
#: States whose outputs downstream nodes may consume.
PRODUCED = frozenset({"succeeded", "cached", "bypassed", "reused"})
RUN_MODES = ("full", "node", "from", "downstream", "rerun_failed", "regenerate")


class CycleError(ValueError):
    def __init__(self, nodes: list[str]) -> None:
        super().__init__("the graph has a cycle")
        self.nodes = nodes


class Graph:
    def __init__(self, nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> None:
        self.nodes: list[str] = list(dict.fromkeys(nodes))
        self._index = {n: i for i, n in enumerate(self.nodes)}
        self.succ: dict[str, list[str]] = defaultdict(list)
        self.pred: dict[str, list[str]] = defaultdict(list)
        for a, b in edges:
            if a not in self._index or b not in self._index:
                raise KeyError(f"edge {a}->{b} refers to an unknown node")
            if b not in self.succ[a]:
                self.succ[a].append(b)
                self.pred[b].append(a)

    def topo_order(self) -> list[str]:
        indeg = {n: len(self.pred[n]) for n in self.nodes}
        ready = deque(n for n in self.nodes if indeg[n] == 0)
        order: list[str] = []
        while ready:
            n = ready.popleft()
            order.append(n)
            for m in sorted(self.succ[n], key=self._index.__getitem__):
                indeg[m] -= 1
                if indeg[m] == 0:
                    ready.append(m)
        if len(order) != len(self.nodes):
            rest = [n for n in self.nodes if indeg[n] > 0]
            raise CycleError(self._cycle_members(set(rest)) or rest)
        return order

    def _cycle_members(self, rest: set[str]) -> list[str]:
        """Nodes that lie on a cycle (not merely downstream of one)."""
        on_cycle = []
        for n in self.nodes:
            if n in rest and n in self._reach(n, rest):
                on_cycle.append(n)
        return on_cycle

    def _reach(self, start: str, within: set[str]) -> set[str]:
        seen: set[str] = set()
        stack = [m for m in self.succ[start] if m in within]
        while stack:
            m = stack.pop()
            if m in seen:
                continue
            seen.add(m)
            stack.extend(x for x in self.succ[m] if x in within)
        return seen

    def upstream(self, node: str) -> set[str]:
        seen: set[str] = set()
        stack = list(self.pred[node])
        while stack:
            m = stack.pop()
            if m not in seen:
                seen.add(m)
                stack.extend(self.pred[m])
        return seen

    def downstream(self, node: str) -> set[str]:
        seen: set[str] = set()
        stack = list(self.succ[node])
        while stack:
            m = stack.pop()
            if m not in seen:
                seen.add(m)
                stack.extend(self.succ[m])
        return seen

    def plan(self, mode: str, node: str | None = None, failed: Iterable[str] = ()) -> tuple[set[str], set[str]]:
        """(nodes in the run, nodes that must execute even if cached).

        * full / node / rerun_failed: everything may resolve from the cache;
        * regenerate: the node executes again;
        * from / downstream: the targets execute again.

        Ancestors of the targets are always part of the run so their outputs
        are available; they normally resolve from the cache.
        """
        if mode not in RUN_MODES:
            raise ValueError(f"unknown run mode {mode!r}")
        if mode == "full":
            return set(self.nodes), set()
        if mode == "rerun_failed":
            targets: set[str] = set()
            for f in failed:
                if f in self._index:
                    targets |= {f} | self.downstream(f)
            if not targets:
                raise ValueError("nothing failed in that run")
            members = set(targets)
            for t in targets:
                members |= self.upstream(t)
            return members, set()
        if node is None or node not in self._index:
            raise ValueError("choose a node for this run mode")
        if mode in ("node", "regenerate"):
            members = {node} | self.upstream(node)
            return members, ({node} if mode == "regenerate" else set())
        if mode == "from":
            targets = {node} | self.downstream(node)
        else:  # downstream
            targets = self.downstream(node)
            if not targets:
                raise ValueError("nothing is connected downstream of that node")
        members = set(targets)
        for t in targets:
            members |= self.upstream(t)
        # "Run from" / "Run downstream" re-execute their targets; ancestors still come from the cache.
        return members, set(targets)

    def runnable(self, states: dict[str, str], members: set[str]) -> list[str]:
        """Pending members whose in-run predecessors are all terminal, in topo order."""
        out = []
        for n in self.topo_order():
            if n not in members or states.get(n, "pending") != "pending":
                continue
            if all(states.get(p) in TERMINAL for p in self.pred[n] if p in members):
                out.append(n)
        return out
