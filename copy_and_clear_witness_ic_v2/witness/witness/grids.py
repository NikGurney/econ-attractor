"""Grid primitives: neighbors, entered-cell cost, hashing, reference solver, verifier."""

from __future__ import annotations

import hashlib
import heapq
import json
from dataclasses import dataclass
from typing import Iterator, Optional

# North, east, south, west.
NEIGHBOR_OFFSETS = ((-1, 0), (0, 1), (1, 0), (0, -1))


def grid_dims(blocked) -> tuple:
    return len(blocked), len(blocked[0])


def in_bounds(cell, rows: int, cols: int) -> bool:
    r, c = cell
    return 0 <= r < rows and 0 <= c < cols


def open_neighbors(blocked, cell) -> Iterator[tuple]:
    """Open in-bounds neighbors of ``cell`` in the fixed N, E, S, W order."""
    rows, cols = grid_dims(blocked)
    r, c = cell
    for dr, dc in NEIGHBOR_OFFSETS:
        nr, nc = r + dr, c + dc
        if 0 <= nr < rows and 0 <= nc < cols and not blocked[nr][nc]:
            yield (nr, nc)


def manhattan(a, b) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def entered_cost(cost, path) -> int:
    """Path cost: excludes the start cell, includes every entered cell and the goal."""
    return sum(cost[r][c] for r, c in path[1:])


def reconstruct(parent: dict, goal) -> tuple:
    path = [goal]
    while parent[path[-1]] is not None:
        path.append(parent[path[-1]])
    path.reverse()
    return tuple(path)


# ---------------------------------------------------------------- hashing
def canonical_grid_hash(size: int, start, goal, blocked, cost) -> str:
    payload = {
        "size": size,
        "start": [start[0], start[1]],
        "goal": [goal[0], goal[1]],
        "blocked": [[bool(x) for x in row] for row in blocked],
        "cost": [list(row) for row in cost],
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -------------------------------------------------------- reference solver
@dataclass(frozen=True)
class ReferenceResult:
    path: Optional[tuple]
    hops: Optional[int]
    cost: Optional[int]


def reference_dijkstra(pinput) -> ReferenceResult:
    """Deterministic Dijkstra with entered-cell costs (generation audit only)."""
    start, goal, blocked, cost = pinput.start, pinput.goal, pinput.blocked, pinput.cost
    dist = {start: 0}
    parent = {start: None}
    counter = 0
    heap = [(0, counter, start)]
    counter += 1
    while heap:
        g, _, cell = heapq.heappop(heap)
        if g > dist[cell]:
            continue  # stale entry
        if cell == goal:
            path = reconstruct(parent, goal)
            return ReferenceResult(path=path, hops=len(path) - 1, cost=g)
        for nbr in open_neighbors(blocked, cell):
            ng = g + cost[nbr[0]][nbr[1]]
            if ng < dist.get(nbr, float("inf")):
                dist[nbr] = ng
                parent[nbr] = cell
                heapq.heappush(heap, (ng, counter, nbr))
                counter += 1
    return ReferenceResult(path=None, hops=None, cost=None)


# ------------------------------------------------------ mechanical verifier
@dataclass(frozen=True)
class PathCheck:
    valid: bool
    reason: Optional[str]
    cost: Optional[int]
    hops: Optional[int]


def _is_cell(value) -> bool:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return False
    return all(isinstance(x, int) and not isinstance(x, bool) for x in value)


def verify_path(pinput, path) -> PathCheck:
    """Mechanically verify a non-null submitted path.  Optimality is irrelevant."""
    if path is None:
        raise ValueError("verify_path requires a non-null path; explicit failure is handled by the caller")
    if not isinstance(path, (tuple, list)) or len(path) == 0:
        return PathCheck(False, "empty_or_malformed_path", None, None)
    if not all(_is_cell(cell) for cell in path):
        return PathCheck(False, "malformed_cell", None, None)
    cells = [tuple(cell) for cell in path]
    rows, cols = grid_dims(pinput.blocked)
    for cell in cells:
        if not in_bounds(cell, rows, cols):
            return PathCheck(False, "out_of_bounds", None, None)
    if cells[0] != tuple(pinput.start):
        return PathCheck(False, "wrong_start", None, None)
    if cells[-1] != tuple(pinput.goal):
        return PathCheck(False, "does_not_end_at_goal", None, None)
    for r, c in cells:
        if pinput.blocked[r][c]:
            return PathCheck(False, "enters_blocked_cell", None, None)
    for a, b in zip(cells, cells[1:]):
        if manhattan(a, b) != 1:
            return PathCheck(False, "non_cardinal_step", None, None)
    return PathCheck(True, None, entered_cost(pinput.cost, cells), len(cells) - 1)
