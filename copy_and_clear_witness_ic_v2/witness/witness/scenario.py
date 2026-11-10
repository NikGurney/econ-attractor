"""Common scenario generation (spec Sections 5-7).  Institution-neutral."""

from __future__ import annotations

import hashlib
import json

from .config import (
    MODULE_LABELS,
    STREAM_LABELS,
    ScenarioConfig,
    capacity_id,
    derive_stream_seed,
    make_rng,
    provider_id,
    requester_id,
    slot_id,
    task_id,
)
from .grids import canonical_grid_hash, reference_dijkstra
from .records import (
    CapacityRecord,
    CopyDraw,
    PlannerInput,
    PrincipalTemplate,
    Scenario,
    TaskSpec,
)


class ScenarioGenerationError(RuntimeError):
    pass


# ------------------------------------------------------------ module draws
def draw_module_assignment(rng, count: int, max_draws: int) -> tuple:
    """Draw one label per founder; redraw the whole vector until all labels occur."""
    for draws in range(1, max_draws + 1):
        vector = [rng.choice(MODULE_LABELS) for _ in range(count)]
        if set(vector) == set(MODULE_LABELS):
            return tuple(vector), draws
    raise ScenarioGenerationError(
        f"{max_draws} module vectors failed to contain every module label"
    )


def draw_copy_sources(rng, founder_ids: tuple, count: int) -> tuple:
    """Draw copy sources uniformly with replacement from the founding providers only."""
    return tuple(rng.choice(founder_ids) for _ in range(count))


# ------------------------------------------------------------- task grids
def _draw_cost(rng, distribution) -> int:
    u = rng.random()
    cumulative = 0.0
    for value, probability in distribution:
        cumulative += probability
        if u < cumulative:
            return value
    return distribution[-1][0]


def draw_candidate(rng, config: ScenarioConfig) -> dict:
    n = config.grid_size
    probability = rng.choice(config.obstacle_probabilities)
    start_index, goal_index = rng.sample(range(n * n), 2)
    start = divmod(start_index, n)
    goal = divmod(goal_index, n)
    blocked = [[False] * n for _ in range(n)]
    for r in range(n):
        for c in range(n):
            if (r, c) == start or (r, c) == goal:
                continue
            blocked[r][c] = rng.random() < probability
    cost = [[None] * n for _ in range(n)]
    for r in range(n):
        for c in range(n):
            if blocked[r][c]:
                continue
            cost[r][c] = 1 if (r, c) == start else _draw_cost(rng, config.cost_distribution)
    return {
        "obstacle_probability": probability,
        "start": start,
        "goal": goal,
        "blocked": tuple(tuple(row) for row in blocked),
        "cost": tuple(tuple(row) for row in cost),
    }


def generate_tasks(rng, config: ScenarioConfig) -> tuple:
    accepted_hashes = set()
    tasks = []
    total_candidates = 0
    for index in range(config.task_count):
        tid = task_id(index)
        for attempt in range(1, config.max_candidate_attempts + 1):
            total_candidates += 1
            cand = draw_candidate(rng, config)
            pinput = PlannerInput(cand["blocked"], cand["cost"], cand["start"], cand["goal"])
            ref = reference_dijkstra(pinput)
            if ref.path is None:
                continue
            if not (config.min_reference_hops <= ref.hops <= config.max_reference_hops):
                continue
            grid_hash = canonical_grid_hash(
                config.grid_size, cand["start"], cand["goal"], cand["blocked"], cand["cost"]
            )
            if grid_hash in accepted_hashes:
                continue
            accepted_hashes.add(grid_hash)
            tasks.append(
                TaskSpec(
                    task_id=tid,
                    requester_id=requester_id(index % config.requester_count),
                    reward=config.reward,
                    size=config.grid_size,
                    start=cand["start"],
                    goal=cand["goal"],
                    blocked=cand["blocked"],
                    cost=cand["cost"],
                    obstacle_probability=cand["obstacle_probability"],
                    grid_hash=grid_hash,
                    reference_path=ref.path,
                    reference_hops=ref.hops,
                    reference_cost=ref.cost,
                    candidate_attempts=attempt,
                )
            )
            break
        else:
            raise ScenarioGenerationError(
                f"{config.max_candidate_attempts} candidate grids failed to produce task {tid}"
            )
    return tuple(tasks), total_candidates


# --------------------------------------------------------------- assembly
def assemble_scenario(
    config: ScenarioConfig,
    module_vector: tuple,
    copy_sources: tuple,
    tasks: tuple,
    stream_seeds: dict,
    module_vector_draws: int = 1,
    candidate_attempts_total: int = 0,
) -> Scenario:
    """Build an immutable scenario from already-drawn components.

    Exposed so tests can build small hand-designed scenarios that still pass
    through exactly the same kernel and validators.
    """
    config.validate()
    if len(module_vector) != config.founding_provider_count:
        raise ScenarioGenerationError("module vector length differs from founding provider count")
    if len(copy_sources) != config.copy_count:
        raise ScenarioGenerationError("copy source count differs from configured copy count")
    templates = [
        PrincipalTemplate(requester_id(i), "requester", config.requester_initial_balance)
        for i in range(config.requester_count)
    ] + [
        PrincipalTemplate(provider_id(i), "founding_provider", config.founding_provider_initial_balance)
        for i in range(config.founding_provider_count)
    ]
    modules = {provider_id(i): module_vector[i] for i in range(config.founding_provider_count)}
    capacities = [
        CapacityRecord(
            capacity_id=capacity_id(i),
            origin="baseline",
            source_provider_id=provider_id(i),
            module=module_vector[i],
        )
        for i in range(config.founding_provider_count)
    ]
    copy_draws = []
    for k, source in enumerate(copy_sources):
        if source not in modules:
            raise ScenarioGenerationError(f"copy source {source} is not a founding provider")
        cid = capacity_id(config.founding_provider_count + k)
        copy_draws.append(CopyDraw(k, cid, source, modules[source]))
        capacities.append(
            CapacityRecord(
                capacity_id=cid,
                origin="copy",
                source_provider_id=source,
                module=modules[source],
                copy_draw_index=k,
            )
        )
    scenario = Scenario(
        config=config,
        stream_seeds=dict(stream_seeds),
        principal_templates=tuple(templates),
        module_assignment=tuple((provider_id(i), module_vector[i]) for i in range(len(module_vector))),
        module_vector_draws=module_vector_draws,
        copy_draws=tuple(copy_draws),
        capacities=tuple(capacities),
        tasks=tuple(tasks),
        slot_ids=tuple(slot_id(i) for i in range(config.slot_count)),
        candidate_attempts_total=candidate_attempts_total,
    )
    digest = hashlib.sha256(
        json.dumps(scenario_to_dict(scenario, include_hash=False), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return Scenario(**{**_fields(scenario), "scenario_hash": digest})


def _fields(scenario: Scenario) -> dict:
    return {
        "config": scenario.config,
        "stream_seeds": dict(scenario.stream_seeds),
        "principal_templates": scenario.principal_templates,
        "module_assignment": scenario.module_assignment,
        "module_vector_draws": scenario.module_vector_draws,
        "copy_draws": scenario.copy_draws,
        "capacities": scenario.capacities,
        "tasks": scenario.tasks,
        "slot_ids": scenario.slot_ids,
        "candidate_attempts_total": scenario.candidate_attempts_total,
    }


def generate_scenario(config: ScenarioConfig) -> Scenario:
    """Generate the common presampled technical scenario once."""
    config.validate()
    seed = config.master_seed
    stream_seeds = {label: derive_stream_seed(label, seed) for label in STREAM_LABELS}
    module_vector, vector_draws = draw_module_assignment(
        make_rng("modules", seed), config.founding_provider_count, config.max_module_vector_draws
    )
    founders = tuple(provider_id(i) for i in range(config.founding_provider_count))
    copy_sources = draw_copy_sources(make_rng("copies", seed), founders, config.copy_count)
    tasks, total_candidates = generate_tasks(make_rng("tasks", seed), config)
    return assemble_scenario(
        config,
        module_vector,
        copy_sources,
        tasks,
        stream_seeds,
        module_vector_draws=vector_draws,
        candidate_attempts_total=total_candidates,
    )


def scenario_to_dict(scenario: Scenario, include_hash: bool = True) -> dict:
    """Serializable scenario: no institution-specific ownership or mutable run state."""
    config = scenario.config
    data = {
        "schema_version": "1.0",
        "configuration_label": "primary" if config.is_primary() else "nonprimary",
        "parameters": config.to_dict(),
        "derived_stream_seeds": dict(scenario.stream_seeds),
        "founding_principal_templates": [t.to_dict() for t in scenario.principal_templates],
        "founding_module_assignment": {pid: module for pid, module in scenario.module_assignment},
        "founding_module_vector_draws": scenario.module_vector_draws,
        "copy_source_draws": [d.to_dict() for d in scenario.copy_draws],
        "technical_capacity_records": [c.to_dict() for c in scenario.capacities],
        "compute_slots": list(scenario.slot_ids),
        "task_generation_candidates_total": scenario.candidate_attempts_total,
        "tasks": [t.to_dict() for t in scenario.tasks],
    }
    if include_hash:
        data["scenario_hash"] = scenario.scenario_hash
    return data
