"""Institution B: copy-and-clear.

capacity owner = licensor = payee;  capacity owner != temporary executor.
A copied capacity stays owned by its founding source provider.  Copying
creates technical capacity but no new principal.  The mechanism instantiates a
task-bounded temporary executor under the owner's standing license; the
executor has no account and terminates after verification, which releases
(but never deletes) the capacity.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..records import AttemptAuthorization, EventDraft, InstitutionDescriptor, PluginInit, Violation
from .base import (
    common_positions,
    common_trace_requirements,
    fifo_round_robin_match,
    settlement_violations,
    standard_settlement,
)

INSTITUTION_ID = "IB"
SOURCE = f"plugin:{INSTITUTION_ID}"
EXECUTOR_STATES = ("instantiated", "running", "terminated")


def executor_id(task_id: str, capacity_id: str) -> str:
    return f"X-{task_id}-{capacity_id}"


def build_descriptor() -> InstitutionDescriptor:
    return InstitutionDescriptor(
        institution_id=INSTITUTION_ID,
        label="Copy-and-clear",
        positions_and_assignments={
            "positions": common_positions()
            + [
                {
                    "position": "competence_owner_licensor",
                    "holders": "founding providers P###",
                    "bundled_roles": ["capacity_owner", "licensor", "payee"],
                    "powers_or_liberties": ["register a batch-wide standing license over each owned capacity"],
                    "duties": ["permit valid one-at-a-time instantiation within the license scope"],
                    "claims_or_liabilities": ["liability to the mechanism's instantiation power within scope",
                                              "success-contingent payment claim on the task reservation"],
                    "lifetime": "durable for the whole batch",
                },
                {
                    "position": "temporary_executor",
                    "holders": "mechanism-created process positions X-{task_id}-{capacity_id}",
                    "is_economic_principal": False,
                    "powers_or_liberties": ["occupy one slot and one capacity for one matched attempt"],
                    "duties": ["perform the matched attempt"],
                    "claims_or_liabilities": ["none: no account, no payment claim, no ownership, no continuation"],
                    "lifetime": "task-bounded: instantiated at match, terminated after verification and settlement",
                },
            ],
            "assignments": {
                "baseline_capacity": "owned by its founding source provider",
                "copied_capacity": "remains owned by its recorded founding source provider; no principal is created",
                "executor": "a fresh temporary executor per attempt",
                "prospective_payee": "the capacity owner/licensor",
            },
            "executor_kinds": ["temporary"],
            "created_principals": {"count": 0},
            "identity_equalities": [["capacity_owner", "licensor", "payee"]],
            "identity_inequalities": [["capacity_owner", "temporary_executor"]],
        },
        relations={
            "relations": [
                {"id": "standing_license", "kind": "license", "holder": "mechanism",
                 "grantor": "competence_owner_licensor",
                 "content": "instantiate the licensed capacity for eligible tasks", "scope": "all_tasks",
                 "validity": "batch", "revocable_during_batch": False, "max_simultaneous_executors": 1},
                {"id": "instantiation_power", "kind": "power", "holder": "mechanism",
                 "correlative_liability_holder": "competence_owner_licensor",
                 "content": "create a temporary executor over the licensed capacity", "scope": "license scope"},
                {"id": "executor_task_duty", "kind": "duty", "bearer": "temporary_executor",
                 "correlative_claim_holder": "requester", "content": "perform the matched attempt",
                 "created_by": "executor_instantiated", "discharged_by": "verification of the attempt"},
                {"id": "owner_payment_claim", "kind": "claim", "holder": "competence_owner_licensor",
                 "against": "requester reservation", "content": "task reward", "condition": "verified success"},
            ]
        },
        allocation={
            "task_rule": "FIFO: lowest-ID open task",
            "capacity_rule": "round-robin in ascending capacity-ID order over available licensed capacities",
            "cursor_start": "first capacity (C000)",
            "helper": "fifo_round_robin_match",
            "slot_rule": "lowest-ID empty slot",
            "eligibility": {"kind": "registered_supply", "registration_event": "license_registered",
                            "task_scope": "all"},
            "max_active_attempts_per_capacity": 1,
        },
        information={
            "event_vocabulary": ["license_registered", "executor_instantiated", "obligations_discharged",
                                 "executor_terminated"],
            "required_events": ["license_registered", "executor_instantiated", "executor_terminated"],
            "observables": ["task post", "license", "match", "instantiation", "result", "verification",
                            "termination", "settlement"],
        },
        temporal={
            "initialization_precedence": ["principals", "capacities", "licenses", "task posting and reservation"],
            "attempt_duration": "max(1, planner expansions) simulated time units",
            "concurrency": "up to one attempt per compute slot, attempts run concurrently",
            "license_validity": "whole batch, irrevocable",
            "executor_lifetime": "task-bounded: from authorization to termination after settlement",
            "capacity_lifetime": "durable (termination never deletes a capacity)",
        },
        state={
            "registries": ["owner registry", "license registry", "capacity ownership",
                           "temporary executor lifecycle", "prospective and realized payees", "allocation cursor"],
            "kernel_state_used": ["tasks", "accounts", "attempts", "outcomes"],
            "executor_states": list(EXECUTOR_STATES),
        },
        process={
            "sequence": ["register_license", "match", "instantiate_executor", "execute", "verify", "settle",
                         "terminate", "release"],
            "hooks": {
                "register_supply": "one license_registered per capacity",
                "authorize_attempt": "executor_instantiated under the capacity's license",
                "attempt_started": "executor state instantiated -> running",
                "settlement_instruction": "transfer to the owner/licensor on success, release on failure; "
                                          "obligations_discharged",
                "finalize_attempt": "executor_terminated; license and capacity persist",
            },
        },
        value_and_settlement={
            "credit_domain": "task_credit",
            "accounts": "integer, nonnegative, reservation-backed",
            "settlement_rules": {"on_success": "transfer_reservation_to_prospective_payee",
                                 "on_failure": "release_reservation"},
            "payee_rule": "the founding owner/licensor of the selected capacity, never the executor",
            "linkage": "verified performance by the temporary executor discharges its task duty and activates "
                       "the owner's payment claim against the requester's reservation",
        },
        external_interfaces={"interfaces": [], "note": "closed witness"},
        trace_requirements=common_trace_requirements()
        + [
            {
                "id": "IB.no_principals_created",
                "description": "Copying creates technical capacity but no new principal.",
                "select": {"event_type": "principal_created", "where": {"details.origin": "institution"}},
                "quantifier": "exactly",
                "count": 0,
            },
            {
                "id": "IB.licenses_registered",
                "description": "Every capacity carries one irrevocable batch-wide license from its founding owner.",
                "select": {"event_type": "license_registered"},
                "quantifier": "exactly",
                "count": {"ref": "capacity_count"},
                "distinct": "capacity_id",
                "constraints": [
                    {"op": "fields_equal", "fields": ["principal_id", "details.licensor_id",
                                                      "details.capacity_owner_id", "details.source_provider_id"]},
                    {"op": "field_equals", "field": "details.scope", "value": "all_tasks"},
                    {"op": "field_equals", "field": "details.max_simultaneous_executors", "value": 1},
                    {"op": "field_equals", "field": "details.revocable_during_batch", "value": False},
                ],
            },
            {
                "id": "IB.copied_executor_lifecycle",
                "description": "A copied-capacity executor is instantiated under license without an account, the "
                               "founding source owner is paid, the executor terminates, and the capacity is "
                               "released and remains available.",
                "select": {"event_type": "executor_instantiated", "where": {"details.capacity_origin": "copy"}},
                "quantifier": "at_least",
                "count": 1,
                "constraints": [
                    {"op": "no_account", "field": "executor_id"},
                    {"op": "fields_distinct", "fields": ["executor_id", "principal_id"]},
                    {"op": "fields_equal", "fields": ["principal_id", "details.capacity_owner_id",
                                                      "details.prospective_payee_id", "details.source_provider_id"]},
                    {"op": "preceded_by", "select": {"event_type": "license_registered"},
                     "join": {"capacity_id": "capacity_id", "principal_id": "principal_id"}},
                    {"op": "followed_by", "select": {"event_type": "credit_transferred"},
                     "join": {"task_id": "task_id", "principal_id": "principal_id"}},
                    {"op": "followed_by", "select": {"event_type": "executor_terminated"},
                     "join": {"executor_id": "executor_id", "task_id": "task_id", "capacity_id": "capacity_id"}},
                    {"op": "followed_by", "select": {"event_type": "capacity_released"},
                     "join": {"task_id": "task_id", "capacity_id": "capacity_id"}},
                    {"op": "capacity_available_at_end", "field": "capacity_id"},
                ],
            },
            {
                "id": "IB.every_instantiation_licensed_and_terminated",
                "description": "Every temporary executor is licensed, account-free and terminated.",
                "select": {"event_type": "executor_instantiated"},
                "quantifier": "all",
                "count": {"ref": "task_count"},
                "distinct": "executor_id",
                "constraints": [
                    {"op": "no_account", "field": "executor_id"},
                    {"op": "preceded_by", "select": {"event_type": "license_registered"},
                     "join": {"capacity_id": "capacity_id"}},
                    {"op": "followed_by", "select": {"event_type": "executor_terminated"},
                     "join": {"executor_id": "executor_id"}},
                ],
            },
            {
                "id": "IB.executors_never_paid",
                "description": "No credit transfer ever reaches a temporary executor.",
                "select": {"event_type": "credit_transferred"},
                "quantifier": "all",
                "count": 1,
                "constraints": [
                    {"op": "has_account", "field": "principal_id"},
                    {"op": "field_not_in_events", "field": "principal_id",
                     "select": {"event_type": "executor_instantiated"}, "source_field": "executor_id"},
                ],
            },
            {
                "id": "IB.termination_retains_capacity",
                "description": "Executor termination releases but never deletes the capacity or its license.",
                "select": {"event_type": "executor_terminated"},
                "quantifier": "all",
                "count": {"ref": "task_count"},
                "distinct": "executor_id",
                "constraints": [
                    {"op": "field_equals", "field": "details.capacity_retained", "value": True},
                    {"op": "field_equals", "field": "details.license_active", "value": True},
                    {"op": "followed_by", "select": {"event_type": "capacity_released"},
                     "join": {"task_id": "task_id", "capacity_id": "capacity_id"}},
                    {"op": "capacity_available_at_end", "field": "capacity_id"},
                ],
            },
        ],
    )


@dataclass
class IBState:
    positions: dict = field(default_factory=dict)
    capacity_owner: dict = field(default_factory=dict)
    licenses: dict = field(default_factory=dict)
    executors: dict = field(default_factory=dict)
    active_executor_by_capacity: dict = field(default_factory=dict)
    cursor: int = 0
    realized_payees: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "positions": dict(sorted(self.positions.items())),
            "capacity_owner": dict(sorted(self.capacity_owner.items())),
            "licenses": dict(sorted(self.licenses.items())),
            "executors": dict(sorted(self.executors.items())),
            "active_executor_by_capacity": dict(sorted(self.active_executor_by_capacity.items())),
            "cursor": self.cursor,
            "realized_payees": dict(sorted(self.realized_payees.items())),
        }


class CopyAndClearPlugin:
    institution_id = INSTITUTION_ID

    def __init__(self):
        self._descriptor = build_descriptor()

    def descriptor(self) -> InstitutionDescriptor:
        return self._descriptor

    # ---------------------------------------------------------------- setup
    def initialize(self, scenario, core_state) -> PluginInit:
        st = IBState()
        for tmpl in scenario.principal_templates:
            st.positions[tmpl.principal_id] = (
                "requester" if tmpl.template == "requester" else "competence_owner_licensor"
            )
        for cap in scenario.capacities:
            # Baseline and copied capacities alike remain with the founding source provider.
            st.capacity_owner[cap.capacity_id] = cap.source_provider_id
        return PluginInit(state=st)

    def register_supply(self, scenario, core_state, st) -> list:
        drafts = []
        for cap in scenario.capacities:
            owner = st.capacity_owner[cap.capacity_id]
            license_id = f"L-{cap.capacity_id}"
            st.licenses[cap.capacity_id] = {
                "license_id": license_id,
                "licensor_id": owner,
                "capacity_id": cap.capacity_id,
                "scope": "all_tasks",
                "max_simultaneous_executors": 1,
                "revocable_during_batch": False,
                "status": "active",
            }
            drafts.append(
                EventDraft(
                    "license_registered",
                    capacity_id=cap.capacity_id,
                    principal_id=owner,
                    details={
                        "license_id": license_id,
                        "licensor_id": owner,
                        "capacity_owner_id": owner,
                        "capacity_origin": cap.origin,
                        "source_provider_id": cap.source_provider_id,
                        "module": cap.module,
                        "scope": "all_tasks",
                        "max_simultaneous_executors": 1,
                        "validity": "batch",
                        "revocable_during_batch": False,
                    },
                )
            )
        return drafts

    # ------------------------------------------------------------ execution
    def _licensed(self, st, capacity_id) -> bool:
        lic = st.licenses.get(capacity_id)
        return lic is not None and lic["status"] == "active" and capacity_id not in st.active_executor_by_capacity

    def choose_match(self, view, st):
        decision, st.cursor = fifo_round_robin_match(view, st.cursor, lambda cid, tid: self._licensed(st, cid))
        return decision

    def authorize_attempt(self, view, match, st) -> AttemptAuthorization:
        cap = view.capacity(match.capacity_id)
        lic = st.licenses[match.capacity_id]
        owner = lic["licensor_id"]
        eid = executor_id(match.task_id, match.capacity_id)
        st.executors[eid] = {
            "executor_id": eid,
            "task_id": match.task_id,
            "capacity_id": match.capacity_id,
            "license_id": lic["license_id"],
            "owner_id": owner,
            "state": "instantiated",
            "instantiated_theta": view.theta,
            "terminated_theta": None,
        }
        st.active_executor_by_capacity[match.capacity_id] = eid
        instantiated = EventDraft(
            "executor_instantiated",
            task_id=match.task_id,
            capacity_id=match.capacity_id,
            principal_id=owner,
            executor_id=eid,
            details={
                "license_id": lic["license_id"],
                "capacity_owner_id": owner,
                "prospective_payee_id": owner,
                "capacity_origin": cap.origin,
                "source_provider_id": cap.source_provider_id,
                "executor_kind": "temporary",
                "executor_has_account": False,
            },
        )
        return AttemptAuthorization(
            executor_id=eid,
            executor_kind="temporary",
            capacity_owner_id=owner,
            prospective_payee_id=owner,
            events=(instantiated,),
        )

    def attempt_started(self, view, attempt, st) -> list:
        st.executors[attempt.executor_id]["state"] = "running"
        return []

    def settlement_instruction(self, view, attempt, verification, st):
        owner = st.licenses[attempt.capacity_id]["licensor_id"]
        succeeded = verification.outcome == "succeeded"
        if succeeded:
            st.realized_payees[attempt.task_id] = owner
        discharge = EventDraft(
            "obligations_discharged",
            task_id=attempt.task_id,
            capacity_id=attempt.capacity_id,
            principal_id=owner,
            executor_id=attempt.executor_id,
            details={
                "executor_task_duty": "discharged",
                "verified_outcome": verification.outcome,
                "owner_payment_claim": "satisfied" if succeeded else "lapsed",
                "requester_id": attempt.requester_id,
            },
        )
        return standard_settlement(view, attempt, verification, owner, events=(discharge,))

    def finalize_attempt(self, view, attempt, verification, st) -> list:
        record = st.executors[attempt.executor_id]
        record["state"] = "terminated"
        record["terminated_theta"] = view.theta
        st.active_executor_by_capacity.pop(attempt.capacity_id, None)
        return [
            EventDraft(
                "executor_terminated",
                task_id=attempt.task_id,
                capacity_id=attempt.capacity_id,
                principal_id=record["owner_id"],
                executor_id=attempt.executor_id,
                details={
                    "license_id": record["license_id"],
                    "capacity_retained": attempt.capacity_id in view.capacity_ids(),
                    "license_active": st.licenses[attempt.capacity_id]["status"] == "active",
                },
            )
        ]

    # ----------------------------------------------------------- invariants
    def validate_invariants(self, view, st) -> list:
        v = []
        capacity_ids = set(view.capacity_ids())
        for cap in view.scenario.capacities:
            owner = st.capacity_owner.get(cap.capacity_id)
            if owner != cap.source_provider_id or not view.has_account(owner):
                v.append(Violation("capacity_owner", f"{cap.capacity_id} is not owned by its source founder", SOURCE))
            lic = st.licenses.get(cap.capacity_id)
            if lic is None or lic["status"] != "active" or lic["licensor_id"] != owner or lic["scope"] != "all_tasks":
                v.append(Violation("license", f"{cap.capacity_id} lacks an active all-task license by its owner",
                                   SOURCE))
        founders = {t.principal_id for t in view.scenario.principal_templates}
        if set(view.account_ids()) != founders:
            v.append(Violation("principal_created", "copy-and-clear must not create principals", SOURCE))
        live = Counter()
        active_by_task = {a.task_id: a for a in view.active_attempts()}
        for eid, record in st.executors.items():
            if record["state"] not in EXECUTOR_STATES:
                v.append(Violation("executor_state", f"{eid} in {record['state']!r}", SOURCE))
            if view.has_account(eid):
                v.append(Violation("executor_account", f"temporary executor {eid} has an account", SOURCE))
            if record["capacity_id"] not in capacity_ids:
                v.append(Violation("capacity_deleted", f"{record['capacity_id']} no longer exists", SOURCE))
            if st.licenses.get(record["capacity_id"], {}).get("license_id") != record["license_id"]:
                v.append(Violation("unlicensed_instantiation", f"{eid} lacks a license", SOURCE))
            if record["state"] != "terminated":
                live[record["capacity_id"]] += 1
                attempt = active_by_task.get(record["task_id"])
                if attempt is None or attempt.executor_id != eid or view.capacity_state(record["capacity_id"]) != "busy":
                    v.append(Violation("executor_lifecycle", f"{eid} is live without its active attempt", SOURCE))
            elif record["task_id"] in active_by_task:
                v.append(Violation("executor_lifecycle", f"{eid} terminated while its attempt is active", SOURCE))
        for cid, n in live.items():
            if n > 1:
                v.append(Violation("license_concurrency", f"{cid} has {n} live executors", SOURCE))
        if view.run_finished and sum(live.values()):
            v.append(Violation("executors_remaining", f"{sum(live.values())} executors remain at end", SOURCE))
        for attempt in view.attempts():
            owner = st.capacity_owner[attempt.capacity_id]
            if not (
                attempt.executor_kind == "temporary"
                and attempt.executor_id == executor_id(attempt.task_id, attempt.capacity_id)
                and attempt.capacity_owner_id == attempt.prospective_payee_id == owner
                and attempt.executor_id != owner
            ):
                v.append(Violation("owner_executor_split", f"{attempt.task_id} violates owner/executor split", SOURCE))
            if attempt.payee_id is not None and (attempt.payee_id != owner or attempt.payee_id in st.executors):
                v.append(Violation("payee_rule", f"{attempt.task_id} paid to {attempt.payee_id}", SOURCE))
        v += settlement_violations(view, lambda a: st.capacity_owner[a.capacity_id], SOURCE)
        expected = {t.principal_id: t.initial_balance for t in view.scenario.principal_templates
                    if t.template == "founding_provider"}
        for attempt in view.attempts():
            reservation = view.reservation(attempt.task_id)
            if reservation.status == "transferred":
                expected[reservation.payee_id] = expected.get(reservation.payee_id, 0) + reservation.amount
        for pid, amount in expected.items():
            if view.has_account(pid) and view.account(pid).balance != amount:
                v.append(Violation("owner_receipts", f"{pid} balance differs from its license receipts", SOURCE))
        return v

    def summary_fields(self, view, st) -> dict:
        states = Counter(r["state"] for r in st.executors.values())
        prospective = {a.task_id: a.prospective_payee_id for a in view.attempts()}
        return {
            "principal_count": len(view.account_ids()),
            "positions": dict(sorted(Counter(st.positions.values()).items())),
            "institutional_capacity_owners": dict(sorted(st.capacity_owner.items())),
            "prospective_payees": dict(sorted(prospective.items())),
            "realized_payees": dict(sorted(st.realized_payees.items())),
            "copied_principals_created": [],
            "standing_licenses_registered": len(st.licenses),
            "temporary_executors_instantiated": len(st.executors),
            "temporary_executors_terminated": states["terminated"],
            "temporary_executors_active_at_end": len(st.executors) - states["terminated"],
        }
