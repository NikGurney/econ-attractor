"""Shared test fixtures (cached so the primary witness is generated once per process)."""

from __future__ import annotations

import contextlib
import functools
import io
from dataclasses import replace

from witness.config import PRIMARY_CONFIG, ScenarioConfig
from witness.grids import canonical_grid_hash, reference_dijkstra
from witness.records import PlannerInput, TaskSpec
from witness.scenario import generate_scenario

SMALL_CONFIG = ScenarioConfig(
    master_seed=7,
    requester_count=2,
    founding_provider_count=4,
    copy_count=2,
    tasks_per_requester=3,
    slot_count=2,
    requester_initial_balance=3,
    grid_size=7,
    min_reference_hops=4,
    max_reference_hops=20,
)


@functools.lru_cache(maxsize=None)
def primary_scenario():
    return generate_scenario(PRIMARY_CONFIG)


@functools.lru_cache(maxsize=None)
def primary_run():
    from witness.app import execute_witness

    return execute_witness()


def grid(rows):
    """Build (blocked, cost) from strings: '#' blocked, digits are entered costs, '.' is cost 1."""
    blocked = tuple(tuple(ch == "#" for ch in row) for row in rows)
    cost = tuple(tuple(None if ch == "#" else (1 if ch == "." else int(ch)) for ch in row) for row in rows)
    return blocked, cost


def pinput(rows, start, goal):
    blocked, cost = grid(rows)
    return PlannerInput(blocked, cost, tuple(start), tuple(goal))


def make_task(task_id, requester_id, rows, start, goal, reward=1):
    """Hand-built TaskSpec with a correct hash and reference solution."""
    blocked, cost = grid(rows)
    ref = reference_dijkstra(PlannerInput(blocked, cost, start, goal))
    return TaskSpec(
        task_id=task_id,
        requester_id=requester_id,
        reward=reward,
        size=len(rows),
        start=start,
        goal=goal,
        blocked=blocked,
        cost=cost,
        obstacle_probability=0.0,
        grid_hash=canonical_grid_hash(len(rows), start, goal, blocked, cost),
        reference_path=ref.path,
        reference_hops=ref.hops,
        reference_cost=ref.cost,
        candidate_attempts=1,
    )


def quiet(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


def with_seed(seed):
    return replace(PRIMARY_CONFIG, master_seed=seed)
