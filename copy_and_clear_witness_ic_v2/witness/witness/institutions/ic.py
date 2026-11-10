"""Institution C: fission-and-succession task market.

A technical copy is read as evidence of an exogenous, institutionally
recognized fission of its source provider.  Each copy yields a new persistent
successor-principal ``FSkkk`` that inherits the source lineage's standing
performance commitment but not its balance or post-fission history.

When a task is matched to any capacity in a lineage, every lineage member is
recorded as a co-obligor on one jointly dischargeable duty.  The selected
capacity's owner executes; verified success discharges that one duty for all
members and transfers the one reserved credit to the executor alone.  No
sibling is paid for membership.

Bounded build (addendum Section 2).  The kernel requires every account to
exist before supply registration, so successor accounts are created at
initialization as zero-balance shells marked ``dormant_until_fission``; the
plugin's ``principal_fissioned`` and ``successor_commitment_inherited`` events
then make each succession institutionally effective.  Those administrative
``principal_created`` events therefore precede the normative fission events in
the trace.  No principal is created after initialization, no account is
deleted or merged, no balance is partitioned, and no credit moves except
through the kernel's reservation-backed settlement.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..records import AttemptAuthorization, EventDraft, InstitutionDescriptor, PluginInit, PrincipalRequest, Violation
from .base import (
    common_positions,
    common_trace_requirements,
    fifo_round_robin_match,
    settlement_violations,
    standard_settlement,
)

INSTITUTION_ID = "IC"
SOURCE = f"plugin:{INSTITUTION_ID}"
POSITION = "provider_successor"
SUCCESSOR_CLASS = "fission_successor"
INHERITED = ["planning_module", "provider_position", "standing_performance_commitment"]
NOT_INHERITED = ["source_balance", "post_fission_history"]
EVENT_VOCABULARY = [
    "standing_commitment_registered",
    "principal_fissioned",
    "successor_commitment_inherited",
    "successor_offer_registered",
    "successor_set_bound",
    "joint_duty_resolved",
]
BINDING_STATUS_FOR_TASK_STATE = {
    "matched": "bound",
    "executing": "bound",
    "succeeded": "discharged",
    "failed": "failed",
}


def successor_id(draw_index: int) -> str:
    return f"FS{draw_index:03d}"


def lineage_id(root_id: str) -> str:
    return f"LIN-{root_id}"


def commitment_id(root_id: str) -> str:
    return f"SC-{root_id}"


def duty_id(task_id: str) -> str:
    return f"JD-{task_id}"


def offer_id(capacity_id: str) -> str:
    return f"O-{capacity_id}"


def expected_lineages(scenario) -> dict:
    """Root -> sorted members (root plus FS successors of its copy draws); independent of iteration order."""
    members = {
        t.principal_id: [t.principal_id] for t in scenario.principal_templates if t.template == "founding_provider"
    }
    for cap in scenario.capacities:
        if cap.origin == "copy":
            members[cap.source_provider_id].append(successor_id(cap.copy_draw_index))
    return {root: sorted(ids) for root, ids in members.items()}


# =============================================================== descriptor
SUCCESSOR_CREATED = {"event_type": "principal_created", "where": {"details.principal_class": SUCCESSOR_CLASS}}
COPY_BINDING = {"event_type": "successor_set_bound", "where": {"details.capacity_origin": "copy"}}


def build_descriptor() -> InstitutionDescriptor:
    return InstitutionDescriptor(
        institution_id=INSTITUTION_ID,
        label="Fission-and-succession task market",
        positions_and_assignments={
            "positions": common_positions()
            + [
                {
                    "position": POSITION,
                    "holders": "founding providers P### (source successors) and fission successors FS###",
                    "is_economic_principal": True,
                    "bundled_roles_when_selected": ["capacity_owner", "executor", "prospective_payee"],
                    "additional_relations": [
                        "member of its lineage-level successor set",
                        "co-obligor on every jointly dischargeable task duty bound to that set",
                    ],
                    "powers_or_liberties": ["offer its owned capacity for every task in the batch",
                                            "execute through its owned capacity when selected"],
                    "duties": ["bear the lineage's standing performance commitment",
                               "as co-obligor, share the one duty of every task bound to its lineage"],
                    "claims_or_liabilities": ["when selected as executor: the single success-contingent "
                                              "payment claim on the task reservation"],
                    "lifetime": "persistent for the whole batch",
                }
            ],
            "assignments": {
                "lineage": "LIN-<founding provider>: the founder plus every FS successor created from its copy draws",
                "baseline_capacity": "owned by its source successor (the founding provider)",
                "copied_capacity": "owned by the fission successor FSkkk created for copy draw k",
                "executor": "the selected capacity's owner (a persistent principal)",
                "prospective_payee": "the selected executor",
                "co_obligors": "every member of the selected capacity owner's lineage",
            },
            "executor_kinds": ["principal"],
            "created_principals": {
                "principal_class": SUCCESSOR_CLASS,
                "position": POSITION,
                "count": {"ref": "copied_capacity_count"},
                "initial_balance": 0,
                "inherits": INHERITED,
                "does_not_inherit": NOT_INHERITED,
                "account_creation": "zero-balance account shells created by the kernel at initialization and "
                                    "marked dormant_until_fission; principal_fissioned makes each succession "
                                    "institutionally effective",
            },
            "identity_equalities": [["capacity_owner", "executor", "prospective_payee", "payee"]],
            "set_relations": [["co_obligor_set", "equals", "lineage_members_of_executor"],
                              ["executor", "member_of", "co_obligor_set"]],
        },
        relations={
            "relations": [
                {"id": "source_standing_commitment", "kind": "standing_commitment",
                 "holder": "founding provider", "content": "commit the lineage's capacities to the batch",
                 "scope": "all_tasks", "validity": "batch", "revocable_during_batch": False},
                {"id": "fission_succession_power", "kind": "power", "holder": "institution",
                 "content": "recognize a recorded technical copy as a fission that creates a successor-principal "
                            "and extends occupancy of the provider position", "reversible_within_batch": False},
                {"id": "inherited_standing_commitment", "kind": "standing_commitment", "holder": "fission successor",
                 "content": "become a bearer of the source lineage's standing commitment",
                 "balance_transferred": False},
                {"id": "capacity_use_liberty", "kind": "liberty", "holder": POSITION,
                 "content": "execute through the owned capacity when selected"},
                {"id": "joint_performance_duty", "kind": "duty", "bearer": "every lineage member (co-obligors)",
                 "correlative_claim_holder": "requester", "content": "one performance of the matched task",
                 "created_by": "successor_set_bound", "discharged_by": "verified success by the selected executor",
                 "multiplicity": "one duty and one performance per task, jointly dischargeable"},
                {"id": "discharger_payment_claim", "kind": "claim", "holder": "selected executor",
                 "against": "requester reservation", "content": "task reward", "condition": "verified success",
                 "siblings_hold_claim": False},
                {"id": "mechanism_matching_power", "kind": "power", "holder": "mechanism",
                 "content": "bind a lineage's successor set to an open task and select one owned capacity "
                            "for execution"},
            ]
        },
        allocation={
            "task_rule": "FIFO: lowest-ID open task",
            "capacity_rule": "round-robin in ascending capacity-ID order over available offered capacities",
            "cursor_start": "first capacity (C000)",
            "helper": "fifo_round_robin_match",
            "slot_rule": "lowest-ID empty slot",
            "eligibility": {"kind": "registered_supply", "registration_event": "successor_offer_registered",
                            "task_scope": "all"},
            "max_active_attempts_per_capacity": 1,
        },
        information={
            "event_vocabulary": list(EVENT_VOCABULARY),
            "required_events": list(EVENT_VOCABULARY),
            "observables": ["standing commitment", "fission", "commitment inheritance", "offer", "task post",
                            "successor-set binding", "result", "verification", "settlement",
                            "joint duty resolution"],
        },
        temporal={
            "initialization_precedence": [
                "create successor account shells",
                "register source standing commitment",
                "recognize each fission and inherited commitment",
                "register successor offers",
                "post and reserve tasks",
            ],
            "attempt_order": ["bind successor set", "execute", "verify", "transfer or release",
                              "resolve joint duty", "release capacity"],
            "attempt_duration": "max(1, planner expansions) simulated time units",
            "concurrency": "up to one attempt per compute slot, attempts run concurrently",
            "fission_timing": "exogenous; all fissions are recognized at initialization",
            "fission_reversibility": "irreversible within the witness",
            "successor_lifetime": "persistent for the whole batch (source and successors)",
            "commitment_validity": "whole batch, irrevocable",
        },
        state={
            "registries": ["principal positions and classes", "lineage roots and member sets",
                           "fission records", "standing commitments and bearers", "capacity ownership",
                           "successor offers", "joint duty bindings", "prospective and realized payees",
                           "allocation cursor"],
            "kernel_state_used": ["tasks", "accounts", "attempts", "outcomes"],
            "executor_states": [],
            "binding_states": ["bound", "discharged", "failed"],
        },
        process={
            "sequence": ["create_successor_account_shells", "register_source_standing_commitment",
                         "recognize_fission_and_inherited_commitment", "register_successor_offers",
                         "post_and_reserve", "bind_successor_set", "execute", "verify", "transfer_or_release",
                         "resolve_joint_duty", "release_capacity"],
            "hooks": {
                "initialize": "lineages, FS successors (zero-balance shells), standing_commitment_registered, "
                              "principal_fissioned and successor_commitment_inherited",
                "register_supply": "one successor_offer_registered per capacity",
                "authorize_attempt": "successor_set_bound: lineage members become co-obligors on JD-<task>; "
                                     "the owner is executor and prospective payee",
                "settlement_instruction": "transfer to the executor on success, release on failure; "
                                          "joint_duty_resolved",
                "finalize_attempt": "binding status discharged or failed; no further event",
            },
        },
        value_and_settlement={
            "credit_domain": "task_credit",
            "accounts": "integer, nonnegative, reservation-backed",
            "settlement_rules": {"on_success": "transfer_reservation_to_prospective_payee",
                                 "on_failure": "release_reservation"},
            "payee_rule": "the successor-principal that owns the selected capacity and performs the verified attempt",
            "succession_rule": "selective: standing commitment inherited; balance and post-fission history not",
            "balance_partition": "none: the source successor retains its balance and every FS successor starts at 0",
            "linkage": "verified success by the selected executor discharges the one task duty for every "
                       "co-obligor and activates only the executor's payment claim against the requester's "
                       "reservation",
        },
        external_interfaces={"interfaces": [], "note": "closed witness"},
        trace_requirements=common_trace_requirements()
        + [
            {
                "id": "IC.fission_successors_created",
                "description": "One zero-balance successor account shell is created per copied capacity, "
                               "distinct from its source.",
                "select": SUCCESSOR_CREATED,
                "quantifier": "exactly",
                "count": {"ref": "copied_capacity_count"},
                "distinct": "principal_id",
                "constraints": [
                    {"op": "field_equals", "field": "amount", "value": 0},
                    {"op": "has_account", "field": "principal_id"},
                    {"op": "fields_distinct", "fields": ["principal_id", "details.source_provider_id"]},
                    {"op": "field_equals", "field": "details.succession_status", "value": "dormant_until_fission"},
                ],
            },
            {
                "id": "IC.standing_commitments_registered",
                "description": "Every founding provider registers one batch-wide standing commitment for its lineage.",
                "select": {"event_type": "standing_commitment_registered"},
                "quantifier": "exactly",
                "count": {"ref": "founding_provider_count"},
                "distinct": "principal_id",
                "constraints": [
                    {"op": "fields_equal", "fields": ["principal_id", "details.lineage_root_id"]},
                    {"op": "has_account", "field": "principal_id"},
                    {"op": "field_equals", "field": "details.scope", "value": "all_tasks"},
                ],
            },
            {
                "id": "IC.every_copy_recognized_as_fission",
                "description": "Every copied capacity is recognized as a fission of its source into a successor.",
                "select": {"event_type": "principal_fissioned"},
                "quantifier": "exactly",
                "count": {"ref": "copied_capacity_count"},
                "distinct": "capacity_id",
                "constraints": [
                    {"op": "fields_equal",
                     "fields": ["principal_id", "details.lineage_root_id", "details.source_provider_id"]},
                    {"op": "fields_distinct", "fields": ["principal_id", "details.successor_id"]},
                    {"op": "preceded_by", "select": SUCCESSOR_CREATED,
                     "join": {"details.successor_id": "principal_id", "capacity_id": "details.assigned_capacity_id"}},
                    {"op": "preceded_by", "select": {"event_type": "standing_commitment_registered"},
                     "join": {"principal_id": "principal_id"}},
                ],
            },
            {
                "id": "IC.every_successor_inherits",
                "description": "Each successor inherits its lineage's standing commitment after its fission, "
                               "without inheriting balance.",
                "select": {"event_type": "successor_commitment_inherited"},
                "quantifier": "exactly",
                "count": {"ref": "copied_capacity_count"},
                "distinct": "capacity_id",
                "constraints": [
                    {"op": "preceded_by", "select": {"event_type": "principal_fissioned"},
                     "join": {"capacity_id": "capacity_id", "principal_id": "details.successor_id",
                              "details.lineage_root_id": "details.lineage_root_id"}},
                    {"op": "field_in_events", "field": "principal_id", "select": SUCCESSOR_CREATED,
                     "source_field": "principal_id"},
                    {"op": "field_equals", "field": "details.balance_inherited", "value": False},
                ],
            },
            {
                "id": "IC.every_capacity_offered",
                "description": "Every capacity carries one irrevocable batch-wide offer by its owner under its "
                               "lineage's commitment.",
                "select": {"event_type": "successor_offer_registered"},
                "quantifier": "exactly",
                "count": {"ref": "capacity_count"},
                "distinct": "capacity_id",
                "constraints": [
                    {"op": "has_account", "field": "principal_id"},
                    {"op": "fields_equal", "fields": ["principal_id", "details.capacity_owner_id"]},
                    {"op": "field_equals", "field": "details.scope", "value": "all_tasks"},
                    {"op": "field_equals", "field": "details.revocable_during_batch", "value": False},
                    {"op": "preceded_by", "select": {"event_type": "standing_commitment_registered"},
                     "join": {"details.lineage_root_id": "principal_id"}},
                ],
            },
            {
                "id": "IC.every_task_binds_successor_set",
                "description": "Every task binds a successor set; its executor, capacity owner and prospective "
                               "payee are one principal with an account.",
                "select": {"event_type": "successor_set_bound"},
                "quantifier": "exactly",
                "count": {"ref": "task_count"},
                "distinct": "task_id",
                "constraints": [
                    {"op": "fields_equal", "fields": ["principal_id", "executor_id", "details.capacity_owner_id",
                                                      "details.prospective_payee_id"]},
                    {"op": "has_account", "field": "executor_id"},
                    {"op": "preceded_by", "select": {"event_type": "successor_offer_registered"},
                     "join": {"capacity_id": "capacity_id", "principal_id": "principal_id"}},
                ],
            },
            {
                "id": "IC.every_task_duty_resolved",
                "description": "Every task's joint duty is resolved once, after its binding and verification.",
                "select": {"event_type": "joint_duty_resolved"},
                "quantifier": "exactly",
                "count": {"ref": "task_count"},
                "distinct": "task_id",
                "constraints": [
                    {"op": "preceded_by", "select": {"event_type": "successor_set_bound"},
                     "join": {"task_id": "task_id", "executor_id": "executor_id",
                              "details.duty_id": "details.duty_id"}},
                    {"op": "preceded_by",
                     "select": {"event_type": ["verification_succeeded", "verification_failed"]},
                     "join": {"task_id": "task_id"}},
                ],
            },
            {
                "id": "IC.copied_successor_exercised",
                "description": "A copied successor (not the source) is bound, paid after verified success, and "
                               "discharges the joint duty.",
                "select": COPY_BINDING,
                "quantifier": "at_least",
                "count": 1,
                "constraints": [
                    {"op": "fields_distinct", "fields": ["executor_id", "details.source_provider_id"]},
                    {"op": "field_in_events", "field": "executor_id",
                     "select": {"event_type": "principal_fissioned"}, "source_field": "details.successor_id"},
                    {"op": "followed_by", "select": {"event_type": "credit_transferred"},
                     "join": {"task_id": "task_id", "principal_id": "principal_id"}},
                    {"op": "followed_by",
                     "select": {"event_type": "joint_duty_resolved", "where": {"details.duty_status": "discharged"}},
                     "join": {"task_id": "task_id", "executor_id": "executor_id",
                              "details.duty_id": "details.duty_id"}},
                ],
            },
            {
                "id": "IC.copied_binding_lineage_scoped",
                "description": "Every copied-capacity binding is scoped to the source's lineage: its root bears "
                               "the standing commitment and its executor inherited it.  Membership of both in "
                               "co_obligor_ids is enforced by plugin invariants.",
                "select": COPY_BINDING,
                "quantifier": "all",
                "count": 1,
                "constraints": [
                    {"op": "fields_equal", "fields": ["details.lineage_root_id", "details.source_provider_id"]},
                    {"op": "fields_distinct", "fields": ["executor_id", "details.lineage_root_id"]},
                    {"op": "field_in_events", "field": "details.lineage_root_id",
                     "select": {"event_type": "standing_commitment_registered"}, "source_field": "principal_id"},
                    {"op": "field_in_events", "field": "executor_id",
                     "select": {"event_type": "successor_commitment_inherited"}, "source_field": "principal_id"},
                ],
            },
            {
                "id": "IC.payee_is_bound_executor",
                "description": "Every payment goes to the executor bound for that task, never to a co-obligor "
                               "for membership alone.",
                "select": {"event_type": "credit_transferred"},
                "quantifier": "all",
                "count": 1,
                "constraints": [
                    {"op": "preceded_by", "select": {"event_type": "successor_set_bound"},
                     "join": {"task_id": "task_id", "principal_id": "executor_id"}},
                ],
            },
        ],
    )


# ==================================================================== state
@dataclass
class ICState:
    positions: dict = field(default_factory=dict)
    principal_class: dict = field(default_factory=dict)
    capacity_owner: dict = field(default_factory=dict)
    lineage_root_of: dict = field(default_factory=dict)
    members_by_root: dict = field(default_factory=dict)
    successor_for_capacity: dict = field(default_factory=dict)
    fissions: dict = field(default_factory=dict)
    standing_commitments: dict = field(default_factory=dict)
    offers: dict = field(default_factory=dict)
    cursor: int = 0
    bindings: dict = field(default_factory=dict)
    realized_payees: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        def sorted_map(mapping):
            return dict(sorted(mapping.items()))

        return {
            "positions": sorted_map(self.positions),
            "principal_class": sorted_map(self.principal_class),
            "capacity_owner": sorted_map(self.capacity_owner),
            "lineage_root_of": sorted_map(self.lineage_root_of),
            "members_by_root": {root: sorted(m) for root, m in sorted(self.members_by_root.items())},
            "successor_for_capacity": sorted_map(self.successor_for_capacity),
            "fissions": sorted_map(self.fissions),
            "standing_commitments": {
                root: {**c, "members": sorted(c["members"])} for root, c in sorted(self.standing_commitments.items())
            },
            "offers": sorted_map(self.offers),
            "cursor": self.cursor,
            "bindings": {
                tid: {**b, "co_obligor_ids": sorted(b["co_obligor_ids"])} for tid, b in sorted(self.bindings.items())
            },
            "realized_payees": sorted_map(self.realized_payees),
        }


# =================================================================== plugin
class FissionSuccessionPlugin:
    institution_id = INSTITUTION_ID

    def __init__(self):
        self._descriptor = build_descriptor()

    def descriptor(self) -> InstitutionDescriptor:
        return self._descriptor

    # ---------------------------------------------------------------- setup
    def initialize(self, scenario, core_state) -> PluginInit:
        st = ICState()
        founders = []
        for tmpl in scenario.principal_templates:
            pid = tmpl.principal_id
            st.principal_class[pid] = tmpl.template
            if tmpl.template == "requester":
                st.positions[pid] = "requester"
                continue
            founders.append(pid)
            st.positions[pid] = POSITION
            st.lineage_root_of[pid] = pid
            st.members_by_root[pid] = [pid]
        for cap in scenario.capacities:
            if cap.origin == "baseline":
                st.capacity_owner[cap.capacity_id] = cap.source_provider_id
        copies = sorted((c for c in scenario.capacities if c.origin == "copy"), key=lambda c: c.copy_draw_index)

        requests = []
        for cap in copies:
            root, sid = cap.source_provider_id, successor_id(cap.copy_draw_index)
            st.capacity_owner[cap.capacity_id] = sid
            st.successor_for_capacity[cap.capacity_id] = sid
            st.lineage_root_of[sid] = root
            st.members_by_root[root].append(sid)
            st.positions[sid] = POSITION
            st.principal_class[sid] = SUCCESSOR_CLASS
            requests.append(
                PrincipalRequest(
                    sid,
                    POSITION,
                    {
                        "principal_class": SUCCESSOR_CLASS,
                        "succession_status": "dormant_until_fission",
                        "assigned_capacity_id": cap.capacity_id,
                        "source_provider_id": root,
                        "lineage_root_id": root,
                        "lineage_id": lineage_id(root),
                        "copy_draw_index": cap.copy_draw_index,
                    },
                )
            )
        for root in founders:
            st.members_by_root[root].sort()

        events = []
        for root in founders:
            st.standing_commitments[root] = {
                "commitment_id": commitment_id(root),
                "lineage_id": lineage_id(root),
                "lineage_root_id": root,
                "members": [root],
                "scope": "all_tasks",
                "status": "active",
            }
            events.append(
                EventDraft(
                    "standing_commitment_registered",
                    principal_id=root,
                    details={
                        "commitment_id": commitment_id(root),
                        "lineage_id": lineage_id(root),
                        "lineage_root_id": root,
                        "initial_bearer_ids": [root],
                        "scope": "all_tasks",
                        "validity": "batch",
                        "revocable_during_batch": False,
                    },
                )
            )
        for cap in copies:
            root, sid = cap.source_provider_id, successor_id(cap.copy_draw_index)
            commitment = st.standing_commitments[root]
            st.fissions[sid] = {
                "successor_id": sid,
                "capacity_id": cap.capacity_id,
                "copy_draw_index": cap.copy_draw_index,
                "lineage_root_id": root,
                "lineage_id": lineage_id(root),
                "status": "effective",
                "inherited_commitment_id": commitment["commitment_id"],
            }
            events.append(
                EventDraft(
                    "principal_fissioned",
                    capacity_id=cap.capacity_id,
                    principal_id=root,
                    details={
                        "successor_id": sid,
                        "lineage_root_id": root,
                        "lineage_id": lineage_id(root),
                        "source_provider_id": root,
                        "copy_draw_index": cap.copy_draw_index,
                        "module": cap.module,
                        "fission_basis": "exogenous technical copy recorded in the common scenario",
                        "successor_succession_status": "effective",
                        "successor_initial_balance": 0,
                        "source_balance_retained": True,
                        "inherited": INHERITED,
                        "not_inherited": NOT_INHERITED,
                    },
                )
            )
            commitment["members"] = sorted(commitment["members"] + [sid])
            events.append(
                EventDraft(
                    "successor_commitment_inherited",
                    capacity_id=cap.capacity_id,
                    principal_id=sid,
                    details={
                        "commitment_id": commitment["commitment_id"],
                        "lineage_id": lineage_id(root),
                        "lineage_root_id": root,
                        "source_provider_id": root,
                        "bearer_ids": list(commitment["members"]),
                        "balance_inherited": False,
                        "post_fission_history_inherited": False,
                    },
                )
            )
        return PluginInit(state=st, principals=tuple(requests), events=tuple(events))

    def register_supply(self, scenario, core_state, st) -> list:
        drafts = []
        for cap in scenario.capacities:
            owner = st.capacity_owner[cap.capacity_id]
            root = st.lineage_root_of[owner]
            st.offers[cap.capacity_id] = {
                "offer_id": offer_id(cap.capacity_id),
                "provider_id": owner,
                "capacity_id": cap.capacity_id,
                "commitment_id": commitment_id(root),
                "scope": "all_tasks",
                "validity": "batch",
                "revocable_during_batch": False,
                "status": "active",
            }
            drafts.append(
                EventDraft(
                    "successor_offer_registered",
                    capacity_id=cap.capacity_id,
                    principal_id=owner,
                    details={
                        "offer_id": offer_id(cap.capacity_id),
                        "capacity_owner_id": owner,
                        "capacity_origin": cap.origin,
                        "source_provider_id": cap.source_provider_id,
                        "lineage_root_id": root,
                        "lineage_id": lineage_id(root),
                        "module": cap.module,
                        "commitment_id": commitment_id(root),
                        "scope": "all_tasks",
                        "validity": "batch",
                        "revocable_during_batch": False,
                    },
                )
            )
        return drafts

    # ------------------------------------------------------------ execution
    def choose_match(self, view, st):
        decision, st.cursor = fifo_round_robin_match(view, st.cursor, lambda cid, tid: cid in st.offers)
        return decision

    def authorize_attempt(self, view, match, st) -> AttemptAuthorization:
        cap = view.capacity(match.capacity_id)
        owner = st.capacity_owner[match.capacity_id]
        root = st.lineage_root_of[owner]
        members = list(st.members_by_root[root])
        st.bindings[match.task_id] = {
            "duty_id": duty_id(match.task_id),
            "lineage_root_id": root,
            "co_obligor_ids": members,
            "executor_id": owner,
            "capacity_id": match.capacity_id,
            "status": "bound",
        }
        bound = EventDraft(
            "successor_set_bound",
            task_id=match.task_id,
            capacity_id=match.capacity_id,
            principal_id=owner,
            executor_id=owner,
            details={
                "duty_id": duty_id(match.task_id),
                "duty": "jointly_dischargeable_performance",
                "lineage_root_id": root,
                "lineage_id": lineage_id(root),
                "co_obligor_ids": list(members),
                "co_obligor_count": len(members),
                "capacity_owner_id": owner,
                "prospective_payee_id": owner,
                "source_provider_id": cap.source_provider_id,
                "capacity_origin": cap.origin,
                "commitment_id": commitment_id(root),
                "offer_id": offer_id(match.capacity_id),
            },
        )
        return AttemptAuthorization(
            executor_id=owner,
            executor_kind="principal",
            capacity_owner_id=owner,
            prospective_payee_id=owner,
            events=(bound,),
        )

    def settlement_instruction(self, view, attempt, verification, st):
        binding = st.bindings[attempt.task_id]
        succeeded = verification.outcome == "succeeded"
        if succeeded:
            st.realized_payees[attempt.task_id] = attempt.executor_id
        resolved = EventDraft(
            "joint_duty_resolved",
            task_id=attempt.task_id,
            capacity_id=attempt.capacity_id,
            principal_id=attempt.executor_id,
            executor_id=attempt.executor_id,
            details={
                "duty_id": binding["duty_id"],
                "lineage_root_id": binding["lineage_root_id"],
                "co_obligor_ids": list(binding["co_obligor_ids"]),
                "performer_id": attempt.executor_id,
                "discharger_id": attempt.executor_id if succeeded else None,
                "verified_outcome": verification.outcome,
                "duty_status": "discharged" if succeeded else "failed",
                "payment_claim_holder_id": attempt.executor_id,
                "payment_claim_status": "satisfied" if succeeded else "lapsed",
                "requester_id": attempt.requester_id,
            },
        )
        return standard_settlement(view, attempt, verification, attempt.executor_id, events=(resolved,))

    def finalize_attempt(self, view, attempt, verification, st) -> list:
        st.bindings[attempt.task_id]["status"] = "discharged" if verification.outcome == "succeeded" else "failed"
        return []

    # ----------------------------------------------------------- invariants
    def validate_invariants(self, view, st) -> list:
        v = []

        def bad(code, message):
            v.append(Violation(code, message, SOURCE))

        scenario = view.scenario
        templates = scenario.principal_templates
        founders = {t.principal_id for t in templates if t.template == "founding_provider"}
        expected = expected_lineages(scenario)

        # (4) lineage member sets and (5) inherited standing commitments
        if set(st.members_by_root) != founders:
            bad("lineage_roots", "lineage roots differ from the founding providers")
        for root, members in expected.items():
            if st.members_by_root.get(root) != members:
                bad("lineage_members", f"{lineage_id(root)} is not its root plus exactly its copy successors")
            commitment = st.standing_commitments.get(root)
            if (
                commitment is None
                or commitment["status"] != "active"
                or commitment["commitment_id"] != commitment_id(root)
                or sorted(commitment["members"]) != members
            ):
                bad("standing_commitment", f"not every member of {lineage_id(root)} bears its standing commitment")
            for member in members:
                if st.lineage_root_of.get(member) != root:
                    bad("lineage_root", f"{member} is not recorded under {lineage_id(root)}")

        # (1) (3) copied ownership, (6) baseline ownership, (7) offers
        copy_owners = Counter()
        for cap in scenario.capacities:
            cid = cap.capacity_id
            owner = st.capacity_owner.get(cid)
            if cap.origin == "copy":
                sid = successor_id(cap.copy_draw_index)
                copy_owners[owner] += 1
                if owner != sid or st.successor_for_capacity.get(cid) != sid:
                    bad("copied_capacity_owner", f"{cid} is not owned by its fission successor {sid}")
                if st.lineage_root_of.get(sid) != cap.source_provider_id:
                    bad("successor_lineage", f"{sid} lineage root is not {cap.source_provider_id}")
                fission = st.fissions.get(sid)
                if (
                    fission is None
                    or fission["status"] != "effective"
                    or fission["capacity_id"] != cid
                    or fission["inherited_commitment_id"] != commitment_id(cap.source_provider_id)
                ):
                    bad("fission_record", f"{sid} lacks an effective fission with its inherited commitment")
            elif owner != cap.source_provider_id:
                bad("baseline_capacity_owner", f"{cid} is not owned by its source successor")
            offer = st.offers.get(cid)
            if (
                offer is None
                or offer["provider_id"] != owner
                or offer["status"] != "active"
                or offer["scope"] != "all_tasks"
            ):
                bad("offer", f"{cid} lacks one active batch-wide offer by its owner")

        # (2) successor identity
        successors = sorted(st.successor_for_capacity.values())
        if len(set(successors)) != len(successors) or len(successors) != sum(
            1 for c in scenario.capacities if c.origin == "copy"
        ):
            bad("successor_identity", "copied capacities and FS successors are not one-to-one")
        for sid in successors:
            if sid in founders or not view.has_account(sid) or copy_owners[sid] != 1:
                bad("successor_identity", f"{sid} is not a distinct principal owning exactly one copied capacity")
        expected_accounts = {t.principal_id for t in templates} | set(successors)
        if set(view.account_ids()) != expected_accounts:
            bad("principal_set", "accounts differ from scenario principals plus FS successors")

        # (8) (9) (10) (11) (12) bindings, executors, payees
        attempts = {a.task_id: a for a in view.attempts()}
        if set(st.bindings) != set(attempts):
            bad("binding_attempt_mismatch", "joint duty bindings and attempts differ")
        if not set(st.realized_payees) <= set(st.bindings):
            bad("realized_payee", "a realized payee has no binding")
        group_scope_seen = False
        for tid, binding in st.bindings.items():
            attempt = attempts.get(tid)
            if attempt is None:
                continue
            executor, root, members = binding["executor_id"], binding["lineage_root_id"], binding["co_obligor_ids"]
            if st.lineage_root_of.get(executor) != root or sorted(members) != st.members_by_root.get(root):
                bad("co_obligor_set", f"{binding['duty_id']} co-obligors are not the executor's complete lineage")
            if not (
                executor in members
                and executor
                == attempt.executor_id
                == attempt.capacity_owner_id
                == attempt.prospective_payee_id
                == st.capacity_owner.get(attempt.capacity_id)
                and binding["capacity_id"] == attempt.capacity_id
                and attempt.executor_kind == "principal"
                and view.has_account(executor)
            ):
                bad("successor_bundle", f"{tid}: executor is not the member owning the capacity and payee")
            task_state = view.task_state(tid)
            if binding["status"] != BINDING_STATUS_FOR_TASK_STATE.get(task_state):
                bad("joint_duty_status", f"{binding['duty_id']} is {binding['status']} for a {task_state} task")
            if attempt.payee_id is not None and attempt.payee_id != executor:
                bad("co_obligor_paid", f"{tid} paid {attempt.payee_id}, not its executor")
            if task_state == "failed" and attempt.payee_id is not None:
                bad("failure_payee", f"{tid} failed but names a payee")
            if (task_state == "succeeded") != (tid in st.realized_payees) or st.realized_payees.get(
                tid, executor
            ) != executor:
                bad("realized_payee", f"{tid} realized payee does not match its verified outcome")
            if attempt.capacity_origin == "copy" and executor != root and {root, executor} <= set(members):
                group_scope_seen = True
        if view.run_finished and st.bindings and not group_scope_seen:
            bad("group_scope_unexercised", "no copied-capacity duty bound both its source and copied successor")
        v.extend(settlement_violations(view, lambda attempt: attempt.executor_id, SOURCE))

        # (13) (14) balances are each successor's own receipts; nothing inherited
        expected_balance = {t.principal_id: t.initial_balance for t in templates if t.template == "founding_provider"}
        expected_balance.update({sid: 0 for sid in successors})
        for attempt in attempts.values():
            reservation = view.reservation(attempt.task_id)
            if reservation is not None and reservation.status == "transferred":
                expected_balance[reservation.payee_id] = expected_balance.get(reservation.payee_id, 0) + reservation.amount
        for pid, amount in expected_balance.items():
            if view.has_account(pid) and view.account(pid).balance != amount:
                code = "successor_balance" if pid in st.successor_for_capacity.values() else "source_balance"
                bad(code, f"{pid} balance {view.account(pid).balance} is not its own receipts {amount}")
        return v

    # -------------------------------------------------------------- summary
    def summary_fields(self, view, st) -> dict:
        statuses = Counter(b["status"] for b in st.bindings.values())
        successors = sorted(st.successor_for_capacity.values())
        realized = dict(sorted(st.realized_payees.items()))
        return {
            "principal_count": len(view.account_ids()),
            "positions": dict(sorted(Counter(st.positions.values()).items())),
            "institutional_capacity_owners": dict(sorted(st.capacity_owner.items())),
            "lineage_members": {root: sorted(m) for root, m in sorted(st.members_by_root.items())},
            "fission_successors_created": successors,
            "fission_successor_count": len(successors),
            "fission_events": sum(1 for f in st.fissions.values() if f["status"] == "effective"),
            "standing_commitments_registered": len(st.standing_commitments),
            "inherited_commitments": sum(1 for f in st.fissions.values() if f.get("inherited_commitment_id")),
            "joint_duties_bound": len(st.bindings),
            "joint_duties_discharged": statuses["discharged"],
            "joint_duties_failed": statuses["failed"],
            "joint_duties_resolved": statuses["discharged"] + statuses["failed"],
            "multi_member_duties_bound": sum(1 for b in st.bindings.values() if len(b["co_obligor_ids"]) >= 2),
            "copied_successor_payments": sum(1 for payee in realized.values() if payee in set(successors)),
            "prospective_payees": dict(sorted((a.task_id, a.prospective_payee_id) for a in view.attempts())),
            "realized_payees": realized,
            "standing_offers_registered": len(st.offers),
        }
