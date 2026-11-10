"""Plugin protocol and reusable, institution-neutral helpers (spec Section 3.6).

Helpers here are shared building blocks a plugin *may* use.  They carry no
institution-specific meaning: the FIFO/round-robin matcher only needs an
eligibility predicate and a plugin-owned cursor, and the standard settlement
helper only needs the payee the plugin has already determined.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol, runtime_checkable

from ..records import (
    AttemptAuthorization,
    EventDraft,
    InstitutionDescriptor,
    LedgerOp,
    MatchDecision,
    PluginInit,
    SettlementInstruction,
    Violation,
)


@runtime_checkable
class InstitutionPlugin(Protocol):
    """Conceptual plugin protocol.

    Signature notes relative to the spec's sketch (responsibilities unchanged):

    * ``initialize`` returns a :class:`PluginInit` bundling the plugin state
      with any principals the kernel should create (zero balance) and any
      initialization events.  Plugins never create accounts themselves.
    * An optional ``attempt_started(view, attempt, plugin_state)`` hook lets a
      plugin record a lifecycle transition (e.g. executor ``running``) between
      authorization and completion.
    """

    institution_id: str

    def descriptor(self) -> InstitutionDescriptor: ...

    def initialize(self, scenario, core_state) -> PluginInit: ...

    def register_supply(self, scenario, core_state, plugin_state) -> list: ...

    def choose_match(self, view, plugin_state) -> Optional[MatchDecision]: ...

    def authorize_attempt(self, view, match, plugin_state) -> AttemptAuthorization: ...

    def settlement_instruction(self, view, attempt, verification, plugin_state) -> SettlementInstruction: ...

    def finalize_attempt(self, view, attempt, verification, plugin_state) -> list: ...

    def validate_invariants(self, view, plugin_state) -> list: ...

    def summary_fields(self, view, plugin_state) -> dict: ...


# ------------------------------------------------------------- allocation
def fifo_round_robin_match(
    view, cursor: int, eligible: Callable[[str, str], bool]
) -> tuple:
    """Lowest-ID open task; first available eligible capacity scanning from ``cursor``.

    Returns ``(decision_or_None, new_cursor)``.  After selecting capacity index
    ``i`` the cursor becomes ``(i + 1) mod n``.  A busy record is never returned.
    """
    open_tasks = view.open_task_ids()
    if not open_tasks:
        return None, cursor
    task_id = open_tasks[0]
    capacities = view.capacity_ids()
    n = len(capacities)
    for step in range(n):
        index = (cursor + step) % n
        capacity_id = capacities[index]
        if view.capacity_state(capacity_id) == "available" and eligible(capacity_id, task_id):
            return MatchDecision(task_id, capacity_id), (index + 1) % n
    return None, cursor


# ------------------------------------------------------------- settlement
def standard_settlement(view, attempt, verification, payee_id: str, events=()) -> SettlementInstruction:
    """Transfer the task's reservation to ``payee_id`` on verified success; release it on failure."""
    reservation = view.reservation(attempt.task_id)
    if verification.outcome == "succeeded":
        op = LedgerOp("transfer_reserved", attempt.task_id, reservation.amount, payee_id)
    else:
        op = LedgerOp("release_reservation", attempt.task_id, reservation.amount)
    return SettlementInstruction(ops=(op,), events=tuple(events))


def settlement_violations(view, payee_of: Callable[[object], str], source: str) -> list:
    """Transfer only after verified success, to the institution's payee; release after failure."""
    v = []
    for attempt in view.attempts():
        state = view.task_state(attempt.task_id)
        reservation = view.reservation(attempt.task_id)
        if state == "succeeded":
            if reservation.status != "transferred" or reservation.payee_id != payee_of(attempt):
                v.append(Violation("payee_rule", f"{attempt.task_id} not paid to its institutional payee", source))
        elif state == "failed":
            if reservation.status != "released":
                v.append(Violation("failure_release", f"{attempt.task_id} failed without a release", source))
        elif reservation.status != "open":
            v.append(Violation("premature_settlement", f"{attempt.task_id} settled before verification", source))
    return v


# ------------------------------------------------- common trace evidence
def common_trace_requirements() -> list:
    """Common posting, reservation, matching, verification and settlement evidence."""
    return [
        {
            "id": "common.tasks_posted",
            "description": "Every task is posted exactly once by a principal with an account.",
            "select": {"event_type": "task_posted"},
            "quantifier": "exactly",
            "count": {"ref": "task_count"},
            "distinct": "task_id",
            "constraints": [{"op": "has_account", "field": "principal_id"}],
        },
        {
            "id": "common.tasks_prefunded",
            "description": "Every posted task is prefunded by a reservation from its requester.",
            "select": {"event_type": "credit_reserved"},
            "quantifier": "exactly",
            "count": {"ref": "task_count"},
            "distinct": "task_id",
            "constraints": [
                {"op": "preceded_by", "select": {"event_type": "task_posted"},
                 "join": {"task_id": "task_id", "principal_id": "principal_id"}}
            ],
        },
        {
            "id": "common.tasks_matched",
            "description": "Every task is matched exactly once, after its reservation.",
            "select": {"event_type": "task_matched"},
            "quantifier": "exactly",
            "count": {"ref": "task_count"},
            "distinct": "task_id",
            "constraints": [
                {"op": "preceded_by", "select": {"event_type": "credit_reserved"}, "join": {"task_id": "task_id"}}
            ],
        },
        {
            "id": "common.tasks_verified",
            "description": "Every task's attempt is mechanically verified after it completes.",
            "select": {"event_type": ["verification_succeeded", "verification_failed"]},
            "quantifier": "exactly",
            "count": {"ref": "task_count"},
            "distinct": "task_id",
            "constraints": [
                {"op": "preceded_by", "select": {"event_type": "attempt_completed"},
                 "join": {"task_id": "task_id", "capacity_id": "capacity_id"}}
            ],
        },
        {
            "id": "common.tasks_settled",
            "description": "Every task's reservation is transferred or released exactly once.",
            "select": {"event_type": ["credit_transferred", "credit_released"]},
            "quantifier": "exactly",
            "count": {"ref": "task_count"},
            "distinct": "task_id",
        },
        {
            "id": "common.copied_capacity_settled_success",
            "description": "At least one task succeeds through a copy-derived capacity and is paid after verification.",
            "select": {"event_type": "credit_transferred", "where": {"details.capacity_origin": "copy"}},
            "quantifier": "at_least",
            "count": 1,
            "distinct": "task_id",
            "constraints": [
                {"op": "preceded_by", "select": {"event_type": "verification_succeeded"},
                 "join": {"task_id": "task_id", "capacity_id": "capacity_id"}}
            ],
        },
        {
            "id": "common.run_completed",
            "description": "The run terminates cleanly exactly once.",
            "select": {"event_type": "run_completed"},
            "quantifier": "exactly",
            "count": 1,
        },
    ]


def common_positions() -> list:
    return [
        {
            "position": "requester",
            "holders": "founding requesters",
            "powers_or_liberties": ["post its prefunded tasks at initialization"],
            "duties": ["maintain each reservation until verified success or failure"],
            "claims_or_liabilities": ["an admissible attempt", "correct verification",
                                      "successful performance if produced"],
        },
        {
            "position": "mechanism",
            "holders": "the deterministic kernel",
            "powers_or_liberties": ["register supply", "match tasks", "assign slots", "verify paths",
                                    "update state", "settle accounts"],
            "duties": ["apply the published rules", "preserve all invariants"],
            "claims_or_liabilities": ["no economic stake in any outcome"],
        },
    ]
