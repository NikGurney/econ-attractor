"""Normative configuration, named random streams, and stable identifiers."""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

SCHEMA_VERSION = "1.0"
MASTER_SEED = 20261112
STREAM_LABELS = ("modules", "copies", "tasks")
MODULE_LABELS = ("greedy", "bfs", "weighted", "beam")
OBSTACLE_PROBABILITIES = (0.10, 0.15, 0.20, 0.25, 0.30)
# (entered-cell cost, probability) in cumulative-draw order.
COST_DISTRIBUTION = ((1, 0.60), (2, 0.30), (4, 0.10))


def derive_stream_seed(label: str, master_seed: int = MASTER_SEED) -> int:
    """Derive the 64-bit seed of a named stream exactly as in spec Section 5.1."""
    payload = f"{master_seed}:{label}".encode("ascii")
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def make_rng(label: str, master_seed: int = MASTER_SEED) -> random.Random:
    return random.Random(derive_stream_seed(label, master_seed))


# ---------------------------------------------------------------- identifiers
def requester_id(index: int) -> str:
    return f"R{index:03d}"


def provider_id(index: int) -> str:
    return f"P{index:03d}"


def capacity_id(index: int) -> str:
    return f"C{index:03d}"


def task_id(index: int) -> str:
    return f"T{index:03d}"


def slot_id(index: int) -> str:
    return f"S{index}"


class ConfigurationError(ValueError):
    """Raised when a scenario configuration is outside the supported envelope."""


@dataclass(frozen=True)
class ScenarioConfig:
    """Declared witness envelope.  Defaults are the normative primary values."""

    master_seed: int = MASTER_SEED
    requester_count: int = 8
    founding_provider_count: int = 16
    copy_count: int = 16
    tasks_per_requester: int = 8
    slot_count: int = 4
    reward: int = 1
    requester_initial_balance: int = 8
    founding_provider_initial_balance: int = 0
    institution_principal_initial_balance: int = 0
    grid_size: int = 9
    min_reference_hops: int = 8
    max_reference_hops: int = 32
    attempts_per_task: int = 1
    copy_fidelity: int = 1
    runtime_copy_cost: int = 0
    max_candidate_attempts: int = 10_000
    max_module_vector_draws: int = 10_000
    obstacle_probabilities: tuple = OBSTACLE_PROBABILITIES
    cost_distribution: tuple = COST_DISTRIBUTION

    # ------------------------------------------------------------ derived
    @property
    def task_count(self) -> int:
        return self.requester_count * self.tasks_per_requester

    @property
    def capacity_count(self) -> int:
        return self.founding_provider_count + self.copy_count

    @property
    def total_credit_supply(self) -> int:
        return (
            self.requester_count * self.requester_initial_balance
            + self.founding_provider_count * self.founding_provider_initial_balance
        )

    def is_primary(self) -> bool:
        return self == PRIMARY_CONFIG

    # ---------------------------------------------------------- validation
    def validate(self) -> None:
        ints = {
            "requester_count": self.requester_count,
            "founding_provider_count": self.founding_provider_count,
            "copy_count": self.copy_count,
            "tasks_per_requester": self.tasks_per_requester,
            "slot_count": self.slot_count,
            "reward": self.reward,
            "grid_size": self.grid_size,
            "min_reference_hops": self.min_reference_hops,
            "max_reference_hops": self.max_reference_hops,
            "max_candidate_attempts": self.max_candidate_attempts,
            "max_module_vector_draws": self.max_module_vector_draws,
        }
        for name, value in ints.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigurationError(f"{name} must be a positive integer, got {value!r}")
        for name in ("requester_count", "founding_provider_count", "copy_count"):
            if getattr(self, name) > 999:
                raise ConfigurationError(f"{name} exceeds the three-digit identifier range")
        if self.task_count > 1000 or self.capacity_count > 1000:
            raise ConfigurationError("task or capacity count exceeds the three-digit identifier range")
        if self.founding_provider_count < len(MODULE_LABELS):
            raise ConfigurationError(
                "founding_provider_count must be at least the number of module labels "
                "so the every-label-present conditioning can succeed"
            )
        if self.requester_initial_balance < self.tasks_per_requester * self.reward:
            raise ConfigurationError("requesters cannot prefund all of their tasks")
        if self.founding_provider_initial_balance < 0 or self.requester_initial_balance < 0:
            raise ConfigurationError("initial balances must be nonnegative")
        if self.institution_principal_initial_balance != 0:
            raise ConfigurationError("institution-created principals must start with zero balance")
        if self.min_reference_hops > self.max_reference_hops:
            raise ConfigurationError("min_reference_hops exceeds max_reference_hops")
        if self.grid_size < 2:
            raise ConfigurationError("grid_size must be at least 2")
        if (self.attempts_per_task, self.copy_fidelity, self.runtime_copy_cost) != (1, 1, 0):
            raise ConfigurationError(
                "attempts_per_task=1, copy_fidelity=1 and runtime_copy_cost=0 are fixed by the witness"
            )
        if not self.obstacle_probabilities or any(
            not (0.0 <= p < 1.0) for p in self.obstacle_probabilities
        ):
            raise ConfigurationError("obstacle probabilities must lie in [0, 1)")
        costs = [c for c, _ in self.cost_distribution]
        if any(isinstance(c, bool) or not isinstance(c, int) or c < 1 for c in costs):
            raise ConfigurationError("open-cell costs must be positive integers (minimum step cost 1)")
        if abs(sum(p for _, p in self.cost_distribution) - 1.0) > 1e-12:
            raise ConfigurationError("cost distribution probabilities must sum to 1")

    def to_dict(self) -> dict:
        return {
            "master_seed": self.master_seed,
            "requester_count": self.requester_count,
            "founding_provider_count": self.founding_provider_count,
            "copy_count": self.copy_count,
            "baseline_capacity_count": self.founding_provider_count,
            "capacity_count": self.capacity_count,
            "task_count": self.task_count,
            "tasks_per_requester": self.tasks_per_requester,
            "slot_count": self.slot_count,
            "reward": self.reward,
            "requester_initial_balance": self.requester_initial_balance,
            "founding_provider_initial_balance": self.founding_provider_initial_balance,
            "institution_principal_initial_balance": self.institution_principal_initial_balance,
            "total_credit_supply": self.total_credit_supply,
            "grid_size": self.grid_size,
            "movement": "deterministic four-neighbor (N, E, S, W)",
            "open_cell_costs": sorted(c for c, _ in self.cost_distribution),
            "min_reference_hops": self.min_reference_hops,
            "max_reference_hops": self.max_reference_hops,
            "attempts_per_task": self.attempts_per_task,
            "copy_fidelity": self.copy_fidelity,
            "runtime_copy_cost": self.runtime_copy_cost,
            "max_candidate_attempts_per_task": self.max_candidate_attempts,
            "max_module_vector_draws": self.max_module_vector_draws,
            "obstacle_probabilities": list(self.obstacle_probabilities),
            "cost_distribution": [
                {"cost": c, "probability": p} for c, p in self.cost_distribution
            ],
            "module_labels": list(MODULE_LABELS),
        }


PRIMARY_CONFIG = ScenarioConfig()
