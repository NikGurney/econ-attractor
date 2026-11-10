"""Institution-agnostic execution kernel (spec Sections 9.3-11).

The kernel owns immutable scenario data, common task and capacity status,
compute slots, the completion schedule, planner invocation, mechanical
verification, ledger primitives, trace sequencing and common invariants.
It never branches on an institution ID and never imports an institution;
all institution-specific meaning arrives through a plugin's typed requests,
which are validated here before anything is applied.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from dataclasses import dataclass, replace
from typing import Mapping, Optional

from .config import SCHEMA_VERSION
from .grids import verify_path
from .ledger import Ledger, LedgerError
from .planners import PLANNERS, PlanResult
from .records import (
    TASK_STATES,
    TASK_TRANSITIONS,
    TERMINAL_TASK_STATES,
    AttemptAuthorization,
    AttemptRecord,
    EventDraft,
    MatchDecision,
    PluginInit,
    PrincipalRequest,
    SettlementInstruction,
    Verification,
    Violation,
    freeze,
    thaw,
)
from .validation import validate_descriptor, validate_scenario


class KernelConfigurationError(ValueError):
    """Raised before execution when a plugin or descriptor cannot be run at all."""


class FatalInvariantViolation(Exception):
    def __init__(self, violations):
        self.violations = list(violations)
        super().__init__("; ".join(f"{v.code}: {v.message}" for v in self.violations))


def _fatal(code: str, message: str, **details):
    raise FatalInvariantViolation([Violation(code, message, "kernel", details)])


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _safe_path(path):
    try:
        return [[int(r), int(c)] for r, c in path]
    except Exception:  # malformed planner output; keep a diagnostic string
        return repr(path)


# ================================================================ read-only
@dataclass(frozen=True)
class AccountSnapshot:
    principal_id: str
    balance: int
    reserved: int

    @property
    def available(self) -> int:
        return self.balance - self.reserved


@dataclass(frozen=True)
class ReservationSnapshot:
    task_id: str
    principal_id: str
    amount: int
    status: str
    payee_id: Optional[str]


class KernelView:
    """Read-only view of common state handed to plugins and validators."""

    __slots__ = ("_x",)

    def __init__(self, execution):
        self._x = execution

    @property
    def scenario(self):
        return self._x.scenario

    @property
    def institution_id(self) -> str:
        return self._x.institution_id

    @property
    def theta(self) -> int:
        return self._x.theta

    @property
    def run_finished(self) -> bool:
        return self._x.finished

    @property
    def slot_ids(self) -> tuple:
        return self._x.scenario.slot_ids

    # tasks
    def task_ids(self) -> tuple:
        return tuple(t.task_id for t in self._x.scenario.tasks)

    def task(self, task_id):
        return self._x.scenario.task(task_id)

    def task_state(self, task_id) -> str:
        return self._x.task_state[task_id]

    def open_task_ids(self) -> tuple:
        return tuple(sorted(t for t, s in self._x.task_state.items() if s == "open"))

    # capacities
    def capacity_ids(self) -> tuple:
        return tuple(c.capacity_id for c in self._x.scenario.capacities)

    def capacity(self, capacity_id):
        return self._x.scenario.capacity(capacity_id)

    def capacity_state(self, capacity_id) -> str:
        return self._x.capacity_state[capacity_id]

    def is_registered_supply(self, capacity_id) -> bool:
        return capacity_id in self._x.registered_supply

    # slots
    def free_slot_ids(self) -> tuple:
        return tuple(s for s in self._x.scenario.slot_ids if self._x.slots[s] is None)

    def slot_occupant(self, slot_id) -> Optional[str]:
        return self._x.slots[slot_id]

    # accounts
    def has_account(self, principal_id) -> bool:
        return self._x.ledger.has_account(principal_id)

    def account_ids(self) -> tuple:
        return tuple(sorted(self._x.ledger.accounts))

    def account(self, principal_id) -> AccountSnapshot:
        acct = self._x.ledger.accounts[principal_id]
        return AccountSnapshot(principal_id, acct.balance, acct.reserved)

    def reservation(self, task_id) -> Optional[ReservationSnapshot]:
        res = self._x.ledger.reservations.get(task_id)
        if res is None:
            return None
        return ReservationSnapshot(res.task_id, res.principal_id, res.amount, res.status, res.payee_id)

    # attempts
    def attempts(self) -> tuple:
        return tuple(self._x.attempts.values())

    def attempt(self, task_id) -> Optional[AttemptRecord]:
        return self._x.attempts.get(task_id)

    def active_attempts(self) -> tuple:
        return tuple(self._x.attempts[t] for t in self._x.attempt_order if t in self._x.active)

    # trace
    def event_count(self) -> int:
        return len(self._x.events)

    def events(self, event_type: Optional[str] = None) -> tuple:
        return tuple(
            copy.deepcopy(e) for e in self._x.events if event_type is None or e["event_type"] == event_type
        )


# ================================================================== result
@dataclass
class RunResult:
    institution_id: str
    descriptor: object
    scenario_hash: str
    status: str  # "completed" | "blocked" | "violated"
    events: list
    attempts: tuple
    accounts: dict
    task_states: dict
    capacity_states: dict
    slots: dict
    violations: list
    ledger_log: list
    max_slot_occupancy: int
    final_theta: int
    plugin_state: object
    summary: dict


COMMON_SUMMARY_KEYS = (
    "institution_id",
    "run_status",
    "scenario_hash",
    "baseline_capacity_count",
    "copied_capacity_count",
    "capacity_count",
    "tasks_posted",
    "tasks_attempted",
    "tasks_succeeded",
    "tasks_failed",
    "tasks_terminal",
    "copied_capacity_attempts",
    "copied_capacity_successes",
    "initial_total_credits",
    "final_total_credits",
    "final_reserved_credits",
    "max_simultaneous_slot_occupancy",
    "distinct_capacities_used",
    "final_occupied_slots",
    "invariant_violation_count",
    "invariant_violations",
    "final_account_balances",
    "final_theta",
    "event_count",
)


# =============================================================== execution
class Execution:
    def __init__(self, scenario, plugin, planners: Optional[Mapping] = None):
        self.scenario = scenario
        self.plugin = plugin
        self.planners = dict(PLANNERS if planners is None else planners)
        descriptor = plugin.descriptor()
        errors = validate_descriptor(descriptor)
        if errors:
            raise KernelConfigurationError("incomplete or malformed descriptor: " + "; ".join(errors))
        if descriptor.institution_id != getattr(plugin, "institution_id", None):
            raise KernelConfigurationError("plugin institution_id differs from its descriptor")
        self.descriptor = descriptor
        self.institution_id = descriptor.institution_id
        self.vocabulary = frozenset(descriptor.event_vocabulary)
        self.executor_kinds = frozenset(descriptor.positions_and_assignments["executor_kinds"])
        self.supply_event_type = descriptor.allocation["eligibility"]["registration_event"]
        self.settlement_rules = dict(descriptor.value_and_settlement["settlement_rules"])

        self.theta = 0
        self.finished = False
        self.status: Optional[str] = None
        self.events: list = []
        self.violations: list = []
        self.ledger = Ledger()
        self.task_state = {t.task_id: "open" for t in scenario.tasks}
        self.capacity_state = {c.capacity_id: "available" for c in scenario.capacities}
        self.slots = {s: None for s in scenario.slot_ids}
        self.attempts: dict = {}
        self.attempt_order: list = []
        self.attempt_counts = Counter()
        self.active: set = set()
        self.registered_supply: set = set()
        self.temporary_executor_ids: set = set()
        self.max_occupancy = 0
        self.pstate = None
        self.view = KernelView(self)

    # ------------------------------------------------------------- public
    def run(self) -> RunResult:
        try:
            self._initialize()
            self._loop()
        except FatalInvariantViolation as exc:
            self._record_violations(exc.violations)
            self.status = "violated"
        return self._result()

    # ------------------------------------------------------------ events
    def _emit(self, event_type, *, task_id=None, capacity_id=None, principal_id=None, executor_id=None,
              slot_id=None, amount=None, details=None) -> dict:
        event = {
            "schema_version": SCHEMA_VERSION,
            "seq": len(self.events),
            "theta": self.theta,
            "institution": self.institution_id,
            "event_type": event_type,
            "task_id": task_id,
            "capacity_id": capacity_id,
            "principal_id": principal_id,
            "executor_id": executor_id,
            "slot_id": slot_id,
            "amount": amount,
            "details": thaw(freeze(dict(details or {}))),
        }
        self.events.append(event)
        return event

    def _apply_draft(self, draft) -> dict:
        if not isinstance(draft, EventDraft):
            _fatal("malformed_event_draft", f"plugin returned {type(draft).__name__}, not an EventDraft")
        if draft.event_type not in self.vocabulary:
            _fatal("unregistered_event_type", f"event type {draft.event_type!r} is not declared by the plugin")
        if draft.task_id is not None and not self.scenario.has_task(draft.task_id):
            _fatal("unknown_task", f"event references unknown task {draft.task_id!r}")
        if draft.capacity_id is not None and not self.scenario.has_capacity(draft.capacity_id):
            _fatal("unknown_capacity", f"event references unknown capacity {draft.capacity_id!r}")
        if draft.slot_id is not None and draft.slot_id not in self.slots:
            _fatal("unknown_slot", f"event references unknown slot {draft.slot_id!r}")
        if draft.principal_id is not None and not self.ledger.has_account(draft.principal_id):
            _fatal("unknown_principal", f"event principal {draft.principal_id!r} has no account")
        if draft.executor_id is not None and (not isinstance(draft.executor_id, str) or not draft.executor_id):
            _fatal("malformed_event_draft", "executor_id must be a non-empty string or null")
        if draft.amount is not None and not _is_int(draft.amount):
            _fatal("malformed_event_draft", "amount must be an integer or null")
        if not isinstance(draft.details, Mapping):
            _fatal("malformed_event_draft", "details must be a mapping")
        try:
            details = thaw(freeze(dict(draft.details)))
        except TypeError as exc:
            _fatal("malformed_event_draft", f"details are not JSON-serializable: {exc}")
        return self._emit(
            draft.event_type,
            task_id=draft.task_id,
            capacity_id=draft.capacity_id,
            principal_id=draft.principal_id,
            executor_id=draft.executor_id,
            slot_id=draft.slot_id,
            amount=draft.amount,
            details=details,
        )

    def _apply_drafts(self, drafts, hook: str) -> list:
        if drafts is None:
            drafts = ()
        if not isinstance(drafts, (list, tuple)):
            _fatal("malformed_plugin_output", f"{hook} must return a list of EventDraft records")
        return [self._apply_draft(d) for d in drafts]

    def _record_violations(self, violations) -> None:
        for violation in violations:
            self.violations.append(violation)
            try:
                extra = thaw(freeze(dict(violation.details))) if violation.details else {}
            except TypeError:
                extra = {"unserializable_details": repr(violation.details)}
            self._emit(
                "invariant_violation",
                details={
                    "code": violation.code,
                    "message": violation.message,
                    "source": violation.source,
                    "details": extra,
                },
            )

    # ------------------------------------------------------ plugin calls
    def _call(self, hook: str, *args):
        try:
            return getattr(self.plugin, hook)(*args)
        except FatalInvariantViolation:
            raise
        except Exception as exc:  # a plugin fault is a fatal contract failure, never silent
            raise FatalInvariantViolation(
                [Violation("plugin_exception", f"{hook} raised {type(exc).__name__}: {exc}", f"plugin:{self.institution_id}")]
            ) from exc

    def _plugin_state_dict(self) -> dict:
        to_dict = getattr(self.pstate, "to_dict", None)
        if to_dict is None:
            _fatal("plugin_state_not_serializable", "plugin state must provide to_dict()")
        try:
            data = to_dict()
            json.dumps(data, sort_keys=True)
        except (TypeError, ValueError) as exc:
            _fatal("plugin_state_not_serializable", f"plugin state is not JSON-serializable: {exc}")
        return data

    # --------------------------------------------------------- transitions
    def _transition(self, task_id: str, new_state: str) -> None:
        old = self.task_state[task_id]
        if new_state not in TASK_TRANSITIONS[old]:
            _fatal("illegal_task_transition", f"{task_id}: {old} -> {new_state}")
        self.task_state[task_id] = new_state

    def _create_account(self, principal_id: str, balance: int, details: dict) -> None:
        try:
            self.ledger.create_account(principal_id, balance)
        except LedgerError as exc:
            _fatal(exc.code, exc.message)
        self._emit("principal_created", principal_id=principal_id, amount=balance, details=details)

    # ------------------------------------------------------ initialization
    def _initialize(self) -> None:
        scenario_violations = validate_scenario(self.scenario)
        if scenario_violations:
            raise FatalInvariantViolation(scenario_violations)
        # 1-2. founding principals (institution-neutral templates)
        for tmpl in self.scenario.principal_templates:
            self._create_account(
                tmpl.principal_id, tmpl.initial_balance, {"origin": "scenario", "template": tmpl.template}
            )
        # 3. durable technical capacity records
        for cap in self.scenario.capacities:
            record = cap.to_dict()
            record.pop("capacity_id")
            self._emit("capacity_registered", capacity_id=cap.capacity_id, details=record)
        # 4-5. tasks, slots, capacity availability and theta were set in __init__.
        # 6. plugin initialization
        init = self._call("initialize", self.scenario, self.view)
        if not isinstance(init, PluginInit):
            _fatal("malformed_plugin_output", "initialize must return a PluginInit")
        self.pstate = init.state
        self._plugin_state_dict()
        for request in init.principals:
            if not isinstance(request, PrincipalRequest):
                _fatal("malformed_plugin_output", "principal requests must be PrincipalRequest records")
            if not isinstance(request.principal_id, str) or not request.principal_id:
                _fatal("malformed_plugin_output", "principal_id must be a non-empty string")
            if not isinstance(request.position, str) or not request.position:
                _fatal("malformed_plugin_output", "position must be a non-empty string")
            extra = thaw(freeze(dict(request.details)))
            if {"origin", "position"} & set(extra):
                _fatal("malformed_plugin_output", "principal details may not override origin or position")
            self._create_account(
                request.principal_id,
                self.scenario.config.institution_principal_initial_balance,
                {"origin": "institution", "position": request.position, **extra},
            )
        self.ledger.freeze_supply()
        self._apply_drafts(init.events, "initialize")
        # 7. supply registration
        for event in self._apply_drafts(self._call("register_supply", self.scenario, self.view, self.pstate),
                                        "register_supply"):
            if event["event_type"] == self.supply_event_type and event["capacity_id"] is not None:
                self.registered_supply.add(event["capacity_id"])
        # 8. post and prefund every task
        for spec in self.scenario.tasks:
            account = self.ledger.accounts.get(spec.requester_id)
            if account is None or account.available < spec.reward:
                _fatal("posting_not_prefunded", f"{spec.requester_id} cannot prefund {spec.task_id}")
            self._emit(
                "task_posted",
                task_id=spec.task_id,
                principal_id=spec.requester_id,
                amount=spec.reward,
                details={"grid_hash": spec.grid_hash},
            )
            try:
                self.ledger.reserve(spec.task_id, spec.requester_id, spec.reward)
            except LedgerError as exc:
                _fatal(exc.code, exc.message)
            self._emit(
                "credit_reserved",
                task_id=spec.task_id,
                principal_id=spec.requester_id,
                amount=spec.reward,
                details={"available_after": self.ledger.accounts[spec.requester_id].available},
            )
        self._check_invariants()

    # ----------------------------------------------------------- event loop
    def _loop(self) -> None:
        while True:
            self._fill_slots()
            if not self.active:
                open_tasks = sorted(t for t, s in self.task_state.items() if s == "open")
                if open_tasks:
                    self._emit(
                        "run_blocked",
                        details={"open_task_ids": open_tasks, "reason": "no active attempt and no legal match"},
                    )
                    self.status = "blocked"
                    return
                break
            self._advance()
        self.finished = True
        self._check_invariants()
        states = Counter(self.task_state.values())
        self._emit(
            "run_completed",
            details={
                "tasks_succeeded": states["succeeded"],
                "tasks_failed": states["failed"],
                "final_total_credits": self.ledger.total(),
                "final_reserved_credits": self.ledger.total_reserved(),
            },
        )
        self.status = "completed"

    def _fill_slots(self) -> None:
        while True:
            free = [s for s in self.scenario.slot_ids if self.slots[s] is None]
            if not free:
                return
            decision = self._call("choose_match", self.view, self.pstate)
            if decision is None:
                return
            self._start_attempt(free[0], decision)

    def _validate_match(self, decision) -> None:
        if not isinstance(decision, MatchDecision):
            _fatal("malformed_match", f"choose_match returned {type(decision).__name__}")
        if not self.scenario.has_task(decision.task_id) or self.task_state[decision.task_id] != "open":
            _fatal("illegal_match", f"task {decision.task_id!r} is not open")
        if not self.scenario.has_capacity(decision.capacity_id):
            _fatal("illegal_match", f"unknown capacity {decision.capacity_id!r}")
        if self.capacity_state[decision.capacity_id] != "available":
            _fatal("illegal_match", f"capacity {decision.capacity_id} is busy")
        if decision.capacity_id not in self.registered_supply:
            _fatal("ineligible_capacity", f"capacity {decision.capacity_id} has no {self.supply_event_type} registration")
        if self.attempt_counts[decision.task_id] >= self.scenario.config.attempts_per_task:
            _fatal("task_reattempted", f"task {decision.task_id} was already attempted")

    def _validate_authorization(self, spec, auth) -> None:
        if not isinstance(auth, AttemptAuthorization):
            _fatal("malformed_authorization", f"authorize_attempt returned {type(auth).__name__}")
        if auth.executor_kind not in self.executor_kinds:
            _fatal("undeclared_executor_kind", f"executor kind {auth.executor_kind!r} is not declared")
        if not isinstance(auth.executor_id, str) or not auth.executor_id:
            _fatal("malformed_authorization", "executor_id must be a non-empty string")
        if auth.executor_kind == "principal":
            if not self.ledger.has_account(auth.executor_id):
                _fatal("executor_without_account", f"principal executor {auth.executor_id} has no account")
        else:
            if self.ledger.has_account(auth.executor_id):
                _fatal("temporary_executor_has_account", f"temporary executor {auth.executor_id} has an account")
            if auth.executor_id in self.temporary_executor_ids:
                _fatal("temporary_executor_reused", f"temporary executor {auth.executor_id} was already used")
        for role, pid in (("capacity owner", auth.capacity_owner_id), ("prospective payee", auth.prospective_payee_id)):
            if not self.ledger.has_account(pid):
                _fatal("authorization_without_account", f"{role} {pid!r} has no account")
        if auth.prospective_payee_id == spec.requester_id:
            _fatal("self_dealing_payee", f"requester {spec.requester_id} cannot be its own payee")
        if not isinstance(auth.events, (list, tuple)):
            _fatal("malformed_authorization", "authorization events must be a list")

    def _start_attempt(self, slot_id: str, decision) -> None:
        self._validate_match(decision)
        spec = self.scenario.task(decision.task_id)
        cap = self.scenario.capacity(decision.capacity_id)
        auth = self._call("authorize_attempt", self.view, decision, self.pstate)
        self._validate_authorization(spec, auth)
        if auth.executor_kind == "temporary":
            self.temporary_executor_ids.add(auth.executor_id)
        self._apply_drafts(auth.events, "authorize_attempt")
        self._transition(spec.task_id, "matched")
        self.capacity_state[cap.capacity_id] = "busy"
        self._emit(
            "task_matched",
            task_id=spec.task_id,
            capacity_id=cap.capacity_id,
            principal_id=spec.requester_id,
            executor_id=auth.executor_id,
            slot_id=slot_id,
            details={"capacity_origin": cap.origin, "module": cap.module},
        )
        planner = self.planners.get(cap.module)
        if planner is None:
            _fatal("unknown_module", f"no planner for module {cap.module!r}")
        result = planner(spec.planner_input())
        if (
            not isinstance(result, PlanResult)
            or result.status not in ("found", "failed")
            or not _is_int(result.expansions)
            or result.expansions < 0
            or (result.status == "found") != (result.path is not None)
        ):
            _fatal("malformed_planner_result", f"{cap.module} returned {result!r}")
        duration = max(1, result.expansions)
        attempt = AttemptRecord(
            task_id=spec.task_id,
            requester_id=spec.requester_id,
            capacity_id=cap.capacity_id,
            capacity_origin=cap.origin,
            source_provider_id=cap.source_provider_id,
            module=cap.module,
            slot_id=slot_id,
            executor_id=auth.executor_id,
            executor_kind=auth.executor_kind,
            capacity_owner_id=auth.capacity_owner_id,
            prospective_payee_id=auth.prospective_payee_id,
            start_theta=self.theta,
            duration=duration,
            completion_theta=self.theta + duration,
            planner_status=result.status,
            path=result.path,
            expansions=result.expansions,
        )
        self._transition(spec.task_id, "executing")
        self.slots[slot_id] = spec.task_id
        self.attempts[spec.task_id] = attempt
        self.attempt_order.append(spec.task_id)
        self.attempt_counts[spec.task_id] += 1
        self.active.add(spec.task_id)
        self._emit(
            "attempt_started",
            task_id=spec.task_id,
            capacity_id=cap.capacity_id,
            principal_id=auth.capacity_owner_id,
            executor_id=auth.executor_id,
            slot_id=slot_id,
            details={
                "requester_id": spec.requester_id,
                "module": cap.module,
                "capacity_origin": cap.origin,
                "source_provider_id": cap.source_provider_id,
                "executor_kind": auth.executor_kind,
                "capacity_owner_id": auth.capacity_owner_id,
                "prospective_payee_id": auth.prospective_payee_id,
                "duration": duration,
                "scheduled_completion_theta": attempt.completion_theta,
            },
        )
        hook = getattr(self.plugin, "attempt_started", None)
        if hook is not None:
            self._apply_drafts(self._call("attempt_started", self.view, attempt, self.pstate), "attempt_started")
        self.max_occupancy = max(self.max_occupancy, len(self.active))
        self._check_invariants()

    def _advance(self) -> None:
        self.theta = min(self.attempts[t].completion_theta for t in self.active)
        batch = sorted(
            (self.attempts[t] for t in self.active if self.attempts[t].completion_theta == self.theta),
            key=lambda a: self.scenario.capacity_position(a.capacity_id),
        )
        for attempt in batch:
            self._complete(attempt)

    def _validate_settlement(self, attempt, verification, instruction) -> None:
        if not isinstance(instruction, SettlementInstruction):
            _fatal("malformed_settlement", f"settlement_instruction returned {type(instruction).__name__}")
        ops = tuple(instruction.ops)
        if len(ops) != 1:
            _fatal("settlement_op_count", f"{attempt.task_id}: expected exactly one ledger op, got {len(ops)}")
        op = ops[0]
        if op.task_id != attempt.task_id:
            _fatal("unauthorized_settlement", f"op targets {op.task_id}, not {attempt.task_id}")
        if verification.outcome == "succeeded":
            # settlement rule: transfer_reservation_to_prospective_payee
            if op.kind != "transfer_reserved":
                _fatal("unauthorized_settlement", f"{attempt.task_id}: verified success requires a transfer")
            if op.to_principal_id != attempt.prospective_payee_id:
                _fatal(
                    "unauthorized_payment",
                    f"{attempt.task_id}: payee {op.to_principal_id!r} is not the authorized "
                    f"prospective payee {attempt.prospective_payee_id!r}",
                )
        else:
            # settlement rule: release_reservation
            if op.kind != "release_reservation":
                _fatal("unauthorized_payment", f"{attempt.task_id}: verified failure permits only a release")
        if not isinstance(instruction.events, (list, tuple)):
            _fatal("malformed_settlement", "settlement events must be a list")

    def _complete(self, attempt) -> None:
        tid, cid = attempt.task_id, attempt.capacity_id
        spec = self.scenario.task(tid)
        common = dict(task_id=tid, capacity_id=cid, executor_id=attempt.executor_id, slot_id=attempt.slot_id)
        self._emit(
            "attempt_completed",
            principal_id=attempt.capacity_owner_id,
            details={
                "module": attempt.module,
                "capacity_origin": attempt.capacity_origin,
                "planner_status": attempt.planner_status,
                "path": _safe_path(attempt.path) if attempt.path is not None else None,
                "expansions": attempt.expansions,
                "duration": attempt.duration,
                "start_theta": attempt.start_theta,
            },
            **common,
        )
        if attempt.planner_status == "failed":
            verification = Verification(tid, "failed", "explicit_planner_failure", None, None)
        else:
            check = verify_path(spec.planner_input(), attempt.path)
            if not check.valid:
                _fatal(
                    "invalid_planner_path",
                    f"module {attempt.module} returned an invalid non-null path for {tid} on {cid}: {check.reason}",
                    task_id=tid,
                    capacity_id=cid,
                    reason=check.reason,
                )
            verification = Verification(tid, "succeeded", None, check.cost, check.hops)
        self._emit(
            "verification_succeeded" if verification.outcome == "succeeded" else "verification_failed",
            principal_id=attempt.requester_id,
            details={
                "capacity_origin": attempt.capacity_origin,
                "realized_cost": verification.realized_cost,
                "hops": verification.hops,
                "reason": verification.reason,
            },
            **common,
        )
        self._transition(tid, verification.outcome)
        attempt = replace(
            attempt,
            verification_outcome=verification.outcome,
            realized_cost=verification.realized_cost,
            hops=verification.hops,
        )
        self.attempts[tid] = attempt

        instruction = self._call("settlement_instruction", self.view, attempt, verification, self.pstate)
        self._validate_settlement(attempt, verification, instruction)
        try:
            entries = self.ledger.apply_ops(tuple(instruction.ops))
        except LedgerError as exc:
            _fatal(exc.code, exc.message)
        for entry in entries:
            if entry["op"] == "transfer_reserved":
                self._emit(
                    "credit_transferred",
                    principal_id=entry["to_principal_id"],
                    amount=entry["amount"],
                    details={
                        "from_principal_id": entry["from_principal_id"],
                        "to_principal_id": entry["to_principal_id"],
                        "capacity_origin": attempt.capacity_origin,
                        "source_provider_id": attempt.source_provider_id,
                        "rule": self.settlement_rules["on_success"],
                    },
                    **common,
                )
                attempt = replace(attempt, settlement_kind="transferred", payee_id=entry["to_principal_id"])
            else:
                self._emit(
                    "credit_released",
                    principal_id=entry["principal_id"],
                    amount=entry["amount"],
                    details={"capacity_origin": attempt.capacity_origin, "rule": self.settlement_rules["on_failure"]},
                    **common,
                )
                attempt = replace(attempt, settlement_kind="released")
        self.attempts[tid] = attempt
        self._apply_drafts(instruction.events, "settlement_instruction")
        self._apply_drafts(self._call("finalize_attempt", self.view, attempt, verification, self.pstate),
                           "finalize_attempt")
        self.capacity_state[cid] = "available"
        self.slots[attempt.slot_id] = None
        self.active.discard(tid)
        self._emit("capacity_released", principal_id=attempt.capacity_owner_id,
                   details={"capacity_state": "available"}, **common)
        self._check_invariants()

    # ----------------------------------------------------------- invariants
    def _check_invariants(self) -> None:
        violations = list(self.ledger.check())
        violations += self._resource_violations()
        found = self._call("validate_invariants", self.view, self.pstate)
        if not isinstance(found, (list, tuple)) or not all(isinstance(x, Violation) for x in found):
            _fatal("malformed_plugin_output", "validate_invariants must return a list of Violation records")
        violations += list(found)
        if violations:
            raise FatalInvariantViolation(violations)

    def _resource_violations(self) -> list:
        v = []
        slot_count = len(self.scenario.slot_ids)
        if len(self.active) > slot_count:
            v.append(Violation("slot_capacity_exceeded", f"{len(self.active)} active attempts > {slot_count} slots"))
        active = [self.attempts[t] for t in self.active]
        for cid, n in Counter(a.capacity_id for a in active).items():
            if n > 1:
                v.append(Violation("capacity_double_assigned", f"{cid} supports {n} active attempts"))
        for sid, n in Counter(a.slot_id for a in active).items():
            if n > 1:
                v.append(Violation("slot_double_occupied", f"{sid} holds {n} attempts"))
        for sid, tid in self.slots.items():
            if tid is not None and (tid not in self.active or self.attempts[tid].slot_id != sid):
                v.append(Violation("slot_inconsistent", f"slot {sid} records {tid} without a matching attempt"))
        busy = {a.capacity_id for a in active}
        for cid, state in self.capacity_state.items():
            if (state == "busy") != (cid in busy):
                v.append(Violation("capacity_state_inconsistent", f"{cid} is {state}"))
        for tid, n in self.attempt_counts.items():
            if n > self.scenario.config.attempts_per_task:
                v.append(Violation("task_attempted_twice", f"{tid} attempted {n} times"))
        for tid, state in self.task_state.items():
            if state not in TASK_STATES:
                v.append(Violation("unknown_task_state", f"{tid} in {state!r}"))
                continue
            reservation = self.ledger.reservations.get(tid)
            status = reservation.status if reservation else None
            if state == "executing" and tid not in self.active:
                v.append(Violation("task_state_inconsistent", f"{tid} executing without an active attempt"))
            if state in ("open", "matched") and tid in self.attempts:
                v.append(Violation("task_state_inconsistent", f"{tid} is {state} but has an attempt"))
            if state == "succeeded" and status != "transferred":
                v.append(Violation("settlement_missing", f"{tid} succeeded but reservation is {status}"))
            if state == "failed" and status != "released":
                v.append(Violation("settlement_missing", f"{tid} failed but reservation is {status}"))
            if state not in TERMINAL_TASK_STATES and status not in ("open", None):
                v.append(Violation("premature_settlement", f"{tid} is {state} but reservation is {status}"))
        return v

    # --------------------------------------------------------------- result
    def _summary(self, plugin_fields: dict) -> dict:
        attempts = list(self.attempts.values())
        states = Counter(self.task_state.values())
        copied = [a for a in attempts if a.capacity_origin == "copy"]
        summary = {
            "institution_id": self.institution_id,
            "run_status": self.status,
            "scenario_hash": self.scenario.scenario_hash,
            "baseline_capacity_count": sum(1 for c in self.scenario.capacities if c.origin == "baseline"),
            "copied_capacity_count": sum(1 for c in self.scenario.capacities if c.origin == "copy"),
            "capacity_count": len(self.scenario.capacities),
            "tasks_posted": sum(1 for e in self.events if e["event_type"] == "task_posted"),
            "tasks_attempted": len(attempts),
            "tasks_succeeded": states["succeeded"],
            "tasks_failed": states["failed"],
            "tasks_terminal": states["succeeded"] + states["failed"],
            "copied_capacity_attempts": len(copied),
            "copied_capacity_successes": sum(
                1 for a in copied if a.verification_outcome == "succeeded" and a.settlement_kind == "transferred"
            ),
            "initial_total_credits": self.ledger.initial_total,
            "final_total_credits": self.ledger.total(),
            "final_reserved_credits": self.ledger.total_reserved(),
            "max_simultaneous_slot_occupancy": self.max_occupancy,
            "distinct_capacities_used": len({a.capacity_id for a in attempts}),
            "final_occupied_slots": sum(1 for t in self.slots.values() if t is not None),
            "invariant_violation_count": len(self.violations),
            "invariant_violations": [f"{v.code}: {v.message}" for v in self.violations],
            "final_account_balances": self.ledger.snapshot(),
            "final_theta": self.theta,
            "event_count": len(self.events),
        }
        collisions = set(plugin_fields) & set(summary)
        if collisions:
            summary["plugin_summary_error"] = f"plugin fields collide with common fields: {sorted(collisions)}"
        else:
            summary.update(plugin_fields)
        return summary

    def _result(self) -> RunResult:
        plugin_fields, plugin_state = {}, None
        if self.pstate is not None:
            try:
                plugin_fields = self._call("summary_fields", self.view, self.pstate)
                if not isinstance(plugin_fields, dict):
                    raise FatalInvariantViolation(
                        [Violation("malformed_plugin_output", "summary_fields must return a dict")]
                    )
                plugin_fields = thaw(freeze(plugin_fields))
                plugin_state = self._plugin_state_dict()
            except (FatalInvariantViolation, TypeError) as exc:
                plugin_fields = {"plugin_summary_error": str(exc)}
        return RunResult(
            institution_id=self.institution_id,
            descriptor=self.descriptor,
            scenario_hash=self.scenario.scenario_hash,
            status=self.status,
            events=self.events,
            attempts=tuple(self.attempts[t] for t in self.attempt_order),
            accounts=self.ledger.snapshot(),
            task_states=dict(self.task_state),
            capacity_states=dict(self.capacity_state),
            slots=dict(self.slots),
            violations=list(self.violations),
            ledger_log=list(self.ledger.log),
            max_slot_occupancy=self.max_occupancy,
            final_theta=self.theta,
            plugin_state=plugin_state,
            summary=self._summary(plugin_fields),
        )


def run_institution(scenario, plugin, planners: Optional[Mapping] = None) -> RunResult:
    """Execute one plugin over the common scenario through the shared kernel."""
    return Execution(scenario, plugin, planners).run()
