"""Small in-memory property graph with JSON persistence and the few algorithms the layers need.

No graph database: the graphs are a few hundred nodes, built per source set or per run, and must be reproducible
from a JSON file kept next to the run.
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..models.ids import sha256_text

# Provenance status of a node or edge.
EXTRACTED = "EXTRACTED"                # a verbatim span was found in the source text by code
DRAWING_OBSERVED = "DRAWING_OBSERVED"  # cites a drawing; code cannot check it
INFERRED = "INFERRED"                  # produced by a graph algorithm or rule; an idea, not evidence


@dataclass
class Span:
    source: str          # file relpath or material name
    sha256: str          # sha of the whole source text (or image)
    line_start: int = 0  # 1-based, inclusive; 0 when not a text span
    line_end: int = 0
    quote: str = ""

    def label(self) -> str:
        where = f"L{self.line_start}" + (f"-{self.line_end}" if self.line_end != self.line_start else "") if self.line_start else "도면"
        return f"{self.source}:{where}"


@dataclass
class Node:
    id: str
    type: str
    label: str
    status: str = EXTRACTED
    attrs: dict[str, Any] = field(default_factory=dict)
    spans: list[Span] = field(default_factory=list)


@dataclass
class Edge:
    src: str
    dst: str
    type: str
    status: str = EXTRACTED
    attrs: dict[str, Any] = field(default_factory=dict)
    spans: list[Span] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.src, self.dst, self.type)


class Graph:
    def __init__(self, kind: str, meta: dict[str, Any] | None = None):
        self.kind = kind
        self.meta: dict[str, Any] = dict(meta or {})
        self.nodes: dict[str, Node] = {}
        self.edges: dict[tuple[str, str, str], Edge] = {}
        self._out: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        self._in: dict[str, list[tuple[str, str, str]]] = defaultdict(list)

    # ---- building

    def add_node(self, node: Node) -> Node:
        cur = self.nodes.get(node.id)
        if cur is None:
            self.nodes[node.id] = node
            return node
        cur.spans.extend(s for s in node.spans if s not in cur.spans)
        for k, v in node.attrs.items():
            cur.attrs.setdefault(k, v)
        if cur.status == INFERRED and node.status != INFERRED:
            cur.status = node.status
        return cur

    def add_edge(self, edge: Edge) -> Edge | None:
        if edge.src not in self.nodes or edge.dst not in self.nodes:
            return None
        cur = self.edges.get(edge.key)
        if cur is None:
            self.edges[edge.key] = edge
            self._out[edge.src].append(edge.key)
            self._in[edge.dst].append(edge.key)
            return edge
        cur.spans.extend(s for s in edge.spans if s not in cur.spans)
        if cur.status == INFERRED and edge.status != INFERRED:
            cur.status = edge.status
        return cur

    # ---- reading

    def out_edges(self, node_id: str, type_: str | None = None) -> list[Edge]:
        return [self.edges[k] for k in self._out.get(node_id, []) if type_ is None or k[2] == type_]

    def in_edges(self, node_id: str, type_: str | None = None) -> list[Edge]:
        return [self.edges[k] for k in self._in.get(node_id, []) if type_ is None or k[2] == type_]

    def neighbors(self, node_id: str, types: set[str] | None = None) -> set[str]:
        out = {e.dst for e in self.out_edges(node_id) if types is None or e.type in types}
        inn = {e.src for e in self.in_edges(node_id) if types is None or e.type in types}
        return out | inn

    def of_type(self, *types: str) -> list[Node]:
        return [n for n in self.nodes.values() if n.type in types]

    def shortest_path(self, a: str, b: str, types: set[str] | None = None) -> list[str] | None:
        if a not in self.nodes or b not in self.nodes:
            return None
        prev: dict[str, str | None] = {a: None}
        q = deque([a])
        while q:
            cur = q.popleft()
            if cur == b:
                path = [cur]
                while prev[path[-1]] is not None:
                    path.append(prev[path[-1]])  # type: ignore[arg-type]
                return path[::-1]
            for nb in sorted(self.neighbors(cur, types)):
                if nb not in prev:
                    prev[nb] = cur
                    q.append(nb)
        return None

    def communities(self, node_types: set[str], edge_types: set[str] | None = None, rounds: int = 20) -> list[list[str]]:
        """Deterministic label propagation over the undirected subgraph of `node_types`.

        Ties break on the smallest label so the same graph always yields the same groups.
        """
        ids = sorted(n.id for n in self.nodes.values() if n.type in node_types)
        member = set(ids)
        label = {i: i for i in ids}
        for _ in range(rounds):
            changed = False
            for i in ids:
                nbs = [n for n in self.neighbors(i, edge_types) if n in member]
                if not nbs:
                    continue
                counts: dict[str, int] = defaultdict(int)
                for n in nbs:
                    counts[label[n]] += 1
                best = min(counts, key=lambda lab: (-counts[lab], lab))
                if best != label[i]:
                    label[i] = best
                    changed = True
            if not changed:
                break
        groups: dict[str, list[str]] = defaultdict(list)
        for i in ids:
            groups[label[i]].append(i)
        return sorted(groups.values(), key=lambda g: (-len(g), g[0]))

    def degree(self, node_id: str, types: set[str] | None = None) -> int:
        return len(self.neighbors(node_id, types))

    # ---- persistence

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "meta": self.meta,
            "nodes": [asdict(n) for n in sorted(self.nodes.values(), key=lambda n: n.id)],
            "edges": [asdict(e) for e in sorted(self.edges.values(), key=lambda e: e.key)],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Graph:
        g = cls(d["kind"], d.get("meta"))
        for n in d.get("nodes", []):
            g.add_node(Node(n["id"], n["type"], n["label"], n.get("status", EXTRACTED), n.get("attrs", {}), [Span(**s) for s in n.get("spans", [])]))
        for e in d.get("edges", []):
            g.add_edge(Edge(e["src"], e["dst"], e["type"], e.get("status", EXTRACTED), e.get("attrs", {}), [Span(**s) for s in e.get("spans", [])]))
        return g

    def digest(self) -> str:
        return sha256_text(json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Graph:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def stats(self) -> dict[str, Any]:
        by_node: dict[str, int] = defaultdict(int)
        by_edge: dict[str, int] = defaultdict(int)
        by_status: dict[str, int] = defaultdict(int)
        for n in self.nodes.values():
            by_node[n.type] += 1
            by_status[n.status] += 1
        for e in self.edges.values():
            by_edge[e.type] += 1
        return {"nodes": len(self.nodes), "edges": len(self.edges), "node_types": dict(by_node), "edge_types": dict(by_edge), "node_status": dict(by_status)}


def find_span(text: str, quote: str, source: str, sha: str) -> Span | None:
    """Locate `quote` verbatim in `text`, ignoring whitespace differences (line breaks inside PDFs/DOCX)."""
    q = "".join(quote.split())
    if len(q) < 2:
        return None
    # Map positions of the whitespace-free text back to the original.
    pos: list[int] = []
    chars: list[str] = []
    for i, ch in enumerate(text):
        if not ch.isspace():
            pos.append(i)
            chars.append(ch)
    flat = "".join(chars)
    at = flat.find(q)
    if at < 0:
        return None
    start, end = pos[at], pos[at + len(q) - 1]
    line_start = text.count("\n", 0, start) + 1
    line_end = text.count("\n", 0, end) + 1
    return Span(source, sha, line_start, line_end, quote.strip())
