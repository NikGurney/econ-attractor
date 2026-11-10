"""Typed records shared by the kernel, plugins, validators and writers.

Everything here is institution-agnostic.  Plugins communicate with the kernel
exclusively through the request records defined below (``EventDraft``,
``MatchDecision``, ``AttemptAuthorization``, ``LedgerOp``,
``SettlementInstruction``, ``PrincipalRequest``, ``PluginInit``,
``Violation``); the kernel validates and applies them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional

from .config import ScenarioConfig

Cell = tuple  # (row, column)

KERNEL_EVENT_TYPES = (
    "principal_created",
    "capacity_registered",
    "task_posted",
    "credit_reserved",
    "task_matched",
    "attempt_started",
    "attempt_completed",
    "verification_succeeded",
    "verification_failed",
    "credit_transferred",
    "credit_released",
    "capacity_released",
    "run_blocked",
    "run_completed",
    "invariant_violation",
)

EVENT_FIELDS = (
    "schema_version",
    "seq",
    "theta",
    "institution",
    "event_type",
    "task_id",
    "capacity_id",
    "principal_id",
    "executor_id",
    "slot_id",
    "amount",
    "details",
)

TASK_STATES = ("open", "matched", "executing", "succeeded", "failed")
TERMINAL_TASK_STATES = ("succeeded", "failed")
TASK_TRANSITIONS = {
    "open": ("matched",),
    "matched": ("executing",),
    "executing": ("succeeded", "failed"),
    "succeeded": (),
    "failed": (),
}
CAPACITY_STATES = ("available", "busy")
EXECUTOR_KINDS = ("principal", "temporary")

SUPPORTED_SETTLEMENT_RULES = {
    "on_success": ("transfer_reservation_to_prospective_payee",),
    "on_failure": ("release_reservation",),
}
SUPPORTED_ELIGIBILITY_KINDS = ("registered_supply",)

DESCRIPTOR_FIELDS = (
    "institution_id",
    "label",
    "positions_and_assignments",
    "relations",
    "allocation",
    "information",
    "temporal",
    "state",
    "process",
    "value_and_settlement",
    "external_interfaces",
    "trace_requirements",
)

INSTITUTION_ID_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*\Z")
EVENT_TYPE_PATTERN = re.compile(r"[a-z][a-z0-9_]*\Z")


# ------------------------------------------------------------ JSON freezing
def freeze(value: Any) -> Any:
    """Deep-freeze a JSON-compatible value (dict -> mappingproxy, list -> tuple)."""
    if isinstance(value, Mapping):
        frozen = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON object keys must be strings, got {key!r}")
            frozen[key] = freeze(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"value of type {type(value).__name__} is not JSON-serializable")


def thaw(value: Any) -> Any:
    """Inverse of :func:`freeze`; returns fresh mutable JSON data."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value


def cell_list(path) -> Optional[list]:
    if path is None:
        return None
    return [[int(r), int(c)] for r, c in path]


# -------------------------------------------------------------- scenario data
@dataclass(frozen=True)
class PlannerInput:
    """What a participant planning module sees: no reference solution."""

    blocked: tuple
    cost: tuple
    start: Cell
    goal: Cell


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    requester_id: str
    reward: int
    size: int
    start: Cell
    goal: Cell
    blocked: tuple
    cost: tuple
    obstacle_probability: float
    grid_hash: str
    reference_path: tuple
    reference_hops: int
    reference_cost: int
    candidate_attempts: int

    def planner_input(self) -> PlannerInput:
        return PlannerInput(blocked=self.blocked, cost=self.cost, start=self.start, goal=self.goal)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "requester_id": self.requester_id,
            "reward": self.reward,
            "size": self.size,
            "start": list(self.start),
            "goal": list(self.goal),
            "blocked": [list(row) for row in self.blocked],
            "cost": [list(row) for row in self.cost],
            "obstacle_probability": self.obstacle_probability,
            "grid_hash": self.grid_hash,
            "reference_path": cell_list(self.reference_path),
            "reference_hops": self.reference_hops,
            "reference_cost": self.reference_cost,
            "generation_candidate_attempts": self.candidate_attempts,
        }


@dataclass(frozen=True)
class CapacityRecord:
    """Institution-neutral technical capacity record."""

    capacity_id: str
    origin: str  # "baseline" | "copy"
    source_provider_id: str
    module: str
    max_concurrency: int = 1
    copy_draw_index: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "capacity_id": self.capacity_id,
            "origin": self.origin,
            "source_provider_id": self.source_provider_id,
            "module": self.module,
            "max_concurrency": self.max_concurrency,
            "copy_draw_index": self.copy_draw_index,
        }


@dataclass(frozen=True)
class PrincipalTemplate:
    principal_id: str
    template: str  # "requester" | "founding_provider"
    initial_balance: int

    def to_dict(self) -> dict:
        return {
            "principal_id": self.principal_id,
            "template": self.template,
            "initial_balance": self.initial_balance,
        }


@dataclass(frozen=True)
class CopyDraw:
    draw_index: int
    capacity_id: str
    source_provider_id: str
    module: str

    def to_dict(self) -> dict:
        return {
            "draw_index": self.draw_index,
            "capacity_id": self.capacity_id,
            "source_provider_id": self.source_provider_id,
            "module": self.module,
        }


@dataclass(frozen=True)
class Scenario:
    config: ScenarioConfig
    stream_seeds: Mapping
    principal_templates: tuple
    module_assignment: tuple  # ((provider_id, module), ...)
    module_vector_draws: int
    copy_draws: tuple
    capacities: tuple
    tasks: tuple
    slot_ids: tuple
    candidate_attempts_total: int
    scenario_hash: str = ""

    def __post_init__(self):
        object.__setattr__(self, "stream_seeds", MappingProxyType(dict(self.stream_seeds)))
        object.__setattr__(
            self, "_task_index", MappingProxyType({t.task_id: t for t in self.tasks})
        )
        object.__setattr__(
            self, "_capacity_index", MappingProxyType({c.capacity_id: c for c in self.capacities})
        )
        object.__setattr__(
            self,
            "_capacity_order",
            MappingProxyType({c.capacity_id: i for i, c in enumerate(self.capacities)}),
        )

    def task(self, task_id: str) -> TaskSpec:
        return self._task_index[task_id]

    def capacity(self, capacity_id: str) -> CapacityRecord:
        return self._capacity_index[capacity_id]

    def capacity_position(self, capacity_id: str) -> int:
        return self._capacity_order[capacity_id]

    def has_task(self, task_id: str) -> bool:
        return task_id in self._task_index

    def has_capacity(self, capacity_id: str) -> bool:
        return capacity_id in self._capacity_index

    def reference_counts(self) -> dict:
        """Named scenario quantities that descriptors may reference."""
        baseline = sum(1 for c in self.capacities if c.origin == "baseline")
        copied = sum(1 for c in self.capacities if c.origin == "copy")
        return {
            "task_count": len(self.tasks),
            "capacity_count": len(self.capacities),
            "baseline_capacity_count": baseline,
            "copied_capacity_count": copied,
            "requester_count": sum(1 for p in self.principal_templates if p.template == "requester"),
            "founding_provider_count": sum(
                1 for p in self.principal_templates if p.template == "founding_provider"
            ),
            "slot_count": len(self.slot_ids),
        }


# ---------------------------------------------------------- plugin requests
@dataclass(frozen=True)
class EventDraft:
    """Institution-specific event submitted to the shared trace writer."""

    event_type: str
    task_id: Optional[str] = None
    capacity_id: Optional[str] = None
    principal_id: Optional[str] = None
    executor_id: Optional[str] = None
    slot_id: Optional[str] = None
    amount: Optional[int] = None
    details: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class MatchDecision:
    task_id: str
    capacity_id: str


@dataclass(frozen=True)
class AttemptAuthorization:
    executor_id: str
    executor_kind: str  # one of EXECUTOR_KINDS, declared in the descriptor
    capacity_owner_id: str
    prospective_payee_id: str
    events: tuple = ()


@dataclass(frozen=True)
class LedgerOp:
    kind: str  # "transfer_reserved" | "release_reservation"
    task_id: str
    amount: int
    to_principal_id: Optional[str] = None


@dataclass(frozen=True)
class SettlementInstruction:
    ops: tuple
    events: tuple = ()


@dataclass(frozen=True)
class PrincipalRequest:
    """A plugin's request that the kernel create a zero-balance principal."""

    principal_id: str
    position: str
    details: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class PluginInit:
    state: Any
    principals: tuple = ()
    events: tuple = ()


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    source: str = "kernel"
    details: Mapping = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "source": self.source,
            "details": thaw(freeze(dict(self.details))),
        }


@dataclass(frozen=True)
class Verification:
    task_id: str
    outcome: str  # "succeeded" | "failed"
    reason: Optional[str]
    realized_cost: Optional[int]
    hops: Optional[int]


@dataclass(frozen=True)
class AttemptRecord:
    task_id: str
    requester_id: str
    capacity_id: str
    capacity_origin: str
    source_provider_id: str
    module: str
    slot_id: str
    executor_id: str
    executor_kind: str
    capacity_owner_id: str
    prospective_payee_id: str
    start_theta: int
    duration: int
    completion_theta: int
    planner_status: str  # "found" | "failed"
    path: Optional[tuple]
    expansions: int
    verification_outcome: Optional[str] = None
    realized_cost: Optional[int] = None
    hops: Optional[int] = None
    settlement_kind: Optional[str] = None  # "transferred" | "released"
    payee_id: Optional[str] = None


# -------------------------------------------------------------- descriptors
@dataclass(frozen=True)
class InstitutionDescriptor:
    """Immutable, JSON-serializable institutional location L(I)."""

    institution_id: str
    label: str
    positions_and_assignments: Mapping
    relations: Mapping
    allocation: Mapping
    information: Mapping
    temporal: Mapping
    state: Mapping
    process: Mapping
    value_and_settlement: Mapping
    external_interfaces: Mapping
    trace_requirements: tuple

    def __post_init__(self):
        for name in DESCRIPTOR_FIELDS[2:]:
            object.__setattr__(self, name, freeze(getattr(self, name)))

    def to_dict(self) -> dict:
        return {name: thaw(getattr(self, name)) for name in DESCRIPTOR_FIELDS}

    @property
    def event_vocabulary(self) -> tuple:
        return tuple(self.information.get("event_vocabulary", ()))


@dataclass(frozen=True)
class ComparisonProfile:
    profile_id: str
    institution_ids: tuple
    require_same_technical_projection: bool
    paired_trace_selector: Mapping
    comparison_invariants: tuple

    def __post_init__(self):
        object.__setattr__(self, "institution_ids", tuple(self.institution_ids))
        object.__setattr__(self, "paired_trace_selector", freeze(self.paired_trace_selector))
        object.__setattr__(self, "comparison_invariants", freeze(self.comparison_invariants))

    def to_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "institution_ids": list(self.institution_ids),
            "require_same_technical_projection": self.require_same_technical_projection,
            "paired_trace_selector": thaw(self.paired_trace_selector),
            "comparison_invariants": thaw(self.comparison_invariants),
        }
