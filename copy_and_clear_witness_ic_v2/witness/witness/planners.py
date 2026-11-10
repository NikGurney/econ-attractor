"""The four stateless participant planning modules (spec Section 8)."""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from .grids import manhattan, open_neighbors, reconstruct

BEAM_WIDTH = 4
WEIGHT = 2


@dataclass(frozen=True)
class PlanResult:
    status: str  # "found" | "failed"
    path: Optional[tuple]
    expansions: int


def _found(parent, goal, expansions) -> PlanResult:
    return PlanResult("found", reconstruct(parent, goal), expansions)


def _failed(expansions) -> PlanResult:
    return PlanResult("failed", None, expansions)


def plan_greedy(p) -> PlanResult:
    """Graph-search greedy best-first; priority (h, insertion_counter)."""
    start, goal = p.start, p.goal
    counter = 0
    heap = [(manhattan(start, goal), counter, start)]
    counter += 1
    seen = {start}
    parent = {start: None}
    expansions = 0
    while heap:
        _, _, cell = heapq.heappop(heap)
        expansions += 1
        if cell == goal:
            return _found(parent, goal, expansions)
        for nbr in open_neighbors(p.blocked, cell):
            if nbr in seen:
                continue
            seen.add(nbr)
            parent[nbr] = cell
            heapq.heappush(heap, (manhattan(nbr, goal), counter, nbr))
            counter += 1
    return _failed(expansions)


def plan_bfs(p) -> PlanResult:
    """Graph-search breadth-first search; ignores cell costs."""
    start, goal = p.start, p.goal
    queue = deque([start])
    seen = {start}
    parent = {start: None}
    expansions = 0
    while queue:
        cell = queue.popleft()
        expansions += 1
        if cell == goal:
            return _found(parent, goal, expansions)
        for nbr in open_neighbors(p.blocked, cell):
            if nbr in seen:
                continue
            seen.add(nbr)
            parent[nbr] = cell
            queue.append(nbr)
    return _failed(expansions)


def plan_weighted(p) -> PlanResult:
    """Weighted best-first: priority (g + 2h, h, g, insertion_counter), no reopening."""
    start, goal, cost = p.start, p.goal, p.cost
    h0 = manhattan(start, goal)
    counter = 0
    heap = [(WEIGHT * h0, h0, 0, counter, start)]
    counter += 1
    best_g = {start: 0}
    parent = {start: None}
    closed = set()
    expansions = 0
    while heap:
        _, _, g, _, cell = heapq.heappop(heap)
        if cell in closed or g > best_g[cell]:
            continue  # closed or stale entry
        closed.add(cell)
        expansions += 1
        if cell == goal:
            return _found(parent, goal, expansions)
        for nbr in open_neighbors(p.blocked, cell):
            if nbr in closed:
                continue
            ng = g + cost[nbr[0]][nbr[1]]
            if ng < best_g.get(nbr, float("inf")):
                best_g[nbr] = ng
                parent[nbr] = cell
                hn = manhattan(nbr, goal)
                heapq.heappush(heap, (ng + WEIGHT * hn, hn, ng, counter, nbr))
                counter += 1
    return _failed(expansions)


def plan_beam(p) -> PlanResult:
    """Level-synchronous beam search of width 4 with a global seen set.

    Candidates of the next layer are unique by cell; when a cell is generated
    more than once in a layer, the entry with the smallest ranking key
    (g + h, h, g, insertion_counter) is kept.  Every unique candidate is marked
    seen after the layer is formed, including candidates pruned from the beam.
    """
    start, goal, cost = p.start, p.goal, p.cost
    beam = [start]
    seen = {start}
    parent = {start: None}
    g_of = {start: 0}
    counter = 0
    expansions = 0
    while beam:
        candidates = {}
        for cell in beam:
            expansions += 1
            if cell == goal:
                return _found(parent, goal, expansions)
            for nbr in open_neighbors(p.blocked, cell):
                if nbr in seen:
                    continue
                ng = g_of[cell] + cost[nbr[0]][nbr[1]]
                hn = manhattan(nbr, goal)
                key = (ng + hn, hn, ng, counter)
                counter += 1
                existing = candidates.get(nbr)
                if existing is None or key < existing[0]:
                    candidates[nbr] = (key, cell, ng)
        seen.update(candidates)
        ranked = sorted(candidates.items(), key=lambda item: item[1][0])
        beam = []
        for nbr, (_, par, ng) in ranked[:BEAM_WIDTH]:
            parent[nbr] = par
            g_of[nbr] = ng
            beam.append(nbr)
    return _failed(expansions)


PLANNERS: Mapping[str, Callable] = {
    "greedy": plan_greedy,
    "bfs": plan_bfs,
    "weighted": plan_weighted,
    "beam": plan_beam,
}
