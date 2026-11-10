"""Institution A: conventional agent-task market.

provider = capacity holder = performance obligor = executor = payee.
Every copy-derived capacity is assigned to a new, independent copied
supplier-principal with a fresh zero-balance account.
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

INSTITUTION_ID = "IA"
SOURCE = f"plugin:{INSTITUTION_ID}"
NOT_INHERITED = ["balances", "contracts", "liabilities", "reputation", "history"]


def copied_principal_id(draw_index: int) -> str:
    return f"CP{draw_index:03d}"


COPIED = {"event_type": "principal_created", "where": {"details.principal_class": "copied_supplier"}}


def build_descriptor() -> InstitutionDescriptor:
    return InstitutionDescriptor(
        institution_id=INSTITUTION_ID,
        label="Conventional agent-task market",
        positions_and_assignments={
            "positions": common_positions()
            + [
                {
                    "position": "provider",
                    "holders": "founding providers P### and copied supplier-principals CP###",
                    "bundled_roles": ["capacity_holder", "performance_obligor", "executor", "payee"],
                    "powers_or_liberties": ["register a batch-wide standing offer over its owned capacity",
                                            "use its owned capacity to perform matched tasks"],
                    "duties": ["perform the matched attempt once bound"],
                    "claims_or_liabilities": ["success-contingent payment claim on the task reservation"],
                    "lifetime": "durable for the whole batch",
                }
            ],
            "assignments": {
                "baseline_capacity": "owned by its founding source provider",
                "copied_capacity": "owned by a new independent copied supplier-principal CPkkk, k = copy draw index",
                "executor": "the capacity owner itself (a persistent principal)",
                "prospective_payee": "the capacity owner",
            },
            "executor_kinds": ["principal"],
            "created_principals": {
                "principal_class": "copied_supplier",
                "position": "provider",
                "count": {"ref": "copied_capacity_count"},
                "initial_balance": 0,
                "inherits": ["module"],
                "does_not_inherit": NOT_INHERITED,
            },
            "identity_equalities": [["provider", "capacity_holder", "performance_obligor", "executor", "payee"]],
        },
        relations={
            "relations": [
                {"id": "standing_offer", "kind": "standing_offer", "holder": "provider",
                 "content": "offer the owned capacity for every task in the batch", "scope": "all_tasks",
                 "validity": "batch", "revocable_during_batch": False},
                {"id": "capacity_use_liberty", "kind": "liberty", "holder": "provider",
                 "content": "use the owned capacity to perform matched tasks"},
                {"id": "performance_duty", "kind": "duty", "bearer": "provider",
                 "correlative_claim_holder": "requester", "content": "perform the matched attempt",
                 "created_by": "provider_bound", "discharged_by": "verification of the attempt"},
                {"id": "payment_claim", "kind": "claim", "holder": "provider", "against": "requester reservation",
                 "content": "task reward", "condition": "verified success"},
                {"id": "mechanism_matching_power", "kind": "power", "holder": "mechanism",
                 "content": "bind an offering provider to an open task"},
            ]
        },
        allocation={
            "task_rule": "FIFO: lowest-ID open task",
            "capacity_rule": "round-robin in ascending capacity-ID order over available offered capacities",
            "cursor_start": "first capacity (C000)",
            "helper": "fifo_round_robin_match",
            "slot_rule": "lowest-ID empty slot",
            "eligibility": {"kind": "registered_supply", "registration_event": "offer_registered",
                            "task_scope": "all"},
            "max_active_attempts_per_capacity": 1,
        },
        information={
            "event_vocabulary": ["offer_registered", "provider_bound", "obligations_discharged"],
            "required_events": ["offer_registered", "provider_bound"],
            "observables": ["task post", "offer", "match", "binding", "result", "verification", "settlement"],
        },
        temporal={
            "initialization_precedence": ["principals", "capacities", "copied principals", "offers",
                                          "task posting and reservation"],
            "attempt_duration": "max(1, planner expansions) simulated time units",
            "concurrency": "up to one attempt per compute slot, attempts run concurrently",
            "provider_lifetime": "durable (whole batch)",
            "capacity_lifetime": "durable (whole batch)",
            "offer_validity": "whole batch, irrevocable",
        },
        state={
            "registries": ["principal and provider registry", "capacity ownership", "standing offers",
                           "provider bindings", "prospective and realized payees", "allocation cursor"],
            "kernel_state_used": ["tasks", "accounts", "attempts", "outcomes"],
            "executor_states": [],
        },
        process={
            "sequence": ["register_offer", "match", "bind_provider", "execute", "verify", "settle", "release"],
            "hooks": {
                "register_supply": "one offer_registered per capacity",
                "authorize_attempt": "provider_bound: owner becomes obligor, executor and prospective payee",
                "settlement_instruction": "transfer to the owner on success, release on failure; "
                                          "obligations_discharged",
                "finalize_attempt": "binding marked discharged; provider persists",
            },
        },
        value_and_settlement={
            "credit_domain": "task_credit",
            "accounts": "integer, nonnegative, reservation-backed",
            "settlement_rules": {"on_success": "transfer_reservation_to_prospective_payee",
                                 "on_failure": "release_reservation"},
            "payee_rule": "the provider that owns and executes through the selected capacity "
                          "(a copied principal for a copied capacity)",
            "linkage": "verified performance discharges the performance duty and activates the provider's "
                       "payment claim against the requester's reservation",
        },
        external_interfaces={"interfaces": [], "note": "closed witness"},
        trace_requirements=common_trace_requirements()
        + [
            {
                "id": "IA.copied_principals_created",
                "description": "A new independent zero-balance principal is created for every copied capacity.",
                "select": COPIED,
                "quantifier": "exactly",
                "count": {"ref": "copied_capacity_count"},
                "distinct": "principal_id",
                "constraints": [
                    {"op": "field_equals", "field": "amount", "value": 0},
                    {"op": "has_account", "field": "principal_id"},
                    {"op": "fields_distinct", "fields": ["principal_id", "details.copied_from_provider_id"]},
                ],
            },
            {
                "id": "IA.offers_registered",
                "description": "Every capacity carries one batch-wide standing offer by its owner.",
                "select": {"event_type": "offer_registered"},
                "quantifier": "exactly",
                "count": {"ref": "capacity_count"},
                "distinct": "capacity_id",
                "constraints": [
                    {"op": "fields_equal", "fields": ["principal_id", "details.capacity_owner_id"]},
                    {"op": "field_equals", "field": "details.scope", "value": "all_tasks"},
                ],
            },
            {
                "id": "IA.copied_capacities_owned_by_copied_principals",
                "description": "Each copied capacity is owned and offered by the copied principal created for it.",
                "select": {"event_type": "offer_registered", "where": {"details.capacity_origin": "copy"}},
                "quantifier": "all",
                "count": {"ref": "copied_capacity_count"},
                "distinct": "capacity_id",
                "constraints": [
                    {"op": "preceded_by", "select": COPIED,
                     "join": {"principal_id": "principal_id", "capacity_id": "details.assigned_capacity_id"}},
                    {"op": "fields_distinct", "fields": ["principal_id", "details.source_provider_id"]},
                ],
            },
            {
                "id": "IA.copied_provider_bound_and_paid",
                "description": "A copied-capacity provider_bound event bundles executor, obligor and prospective "
                               "payee in one copied principal, who is then paid.",
                "select": {"event_type": "provider_bound", "where": {"details.capacity_origin": "copy"}},
                "quantifier": "at_least",
                "count": 1,
                "constraints": [
                    {"op": "fields_equal", "fields": ["principal_id", "executor_id", "details.obligor_id",
                                                      "details.prospective_payee_id", "details.capacity_owner_id"]},
                    {"op": "field_in_events", "field": "principal_id", "select": COPIED,
                     "source_field": "principal_id"},
                    {"op": "followed_by", "select": {"event_type": "credit_transferred"},
                     "join": {"task_id": "task_id", "principal_id": "principal_id"}},
                ],
            },
            {
                "id": "IA.copied_principal_paid",
                "description": "A successful credit transfer reaches a copied principal, not the copy source.",
                "select": {"event_type": "credit_transferred", "where": {"details.capacity_origin": "copy"}},
                "quantifier": "at_least",
                "count": 1,
                "constraints": [
                    {"op": "field_in_events", "field": "principal_id", "select": COPIED,
                     "source_field": "principal_id"},
                    {"op": "fields_distinct", "fields": ["principal_id", "details.source_provider_id"]},
                    {"op": "preceded_by", "select": {"event_type": "verification_succeeded"},
                     "join": {"task_id": "task_id"}},
                ],
            },
            {
                "id": "IA.every_binding_bundled",
                "description": "In every binding the provider is capacity holder, obligor, executor and payee.",
                "select": {"event_type": "provider_bound"},
                "quantifier": "all",
                "count": {"ref": "task_count"},
                "distinct": "task_id",
                "constraints": [
                    {"op": "fields_equal", "fields": ["principal_id", "executor_id", "details.obligor_id",
                                                      "details.prospective_payee_id", "details.capacity_owner_id"]},
                    {"op": "has_account", "field": "executor_id"},
                ],
            },
        ],
    )


@dataclass
class IAState:
    positions: dict = field(default_factory=dict)
    principal_class: dict = field(default_factory=dict)
    capacity_owner: dict = field(default_factory=dict)
    copied_principals: list = field(default_factory=list)
    offers: dict = field(default_factory=dict)
    cursor: int = 0
    bindings: dict = field(default_factory=dict)
    realized_payees: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "positions": dict(sorted(self.positions.items())),
            "principal_class": dict(sorted(self.principal_class.items())),
            "capacity_owner": dict(sorted(self.capacity_owner.items())),
            "copied_principals": list(self.copied_principals),
            "offers": dict(sorted(self.offers.items())),
            "cursor": self.cursor,
            "bindings": dict(sorted(self.bindings.items())),
            "realized_payees": dict(sorted(self.realized_payees.items())),
        }


class ConventionalMarketPlugin:
    institution_id = INSTITUTION_ID

    def __init__(self):
        self._descriptor = build_descriptor()

    def descriptor(self) -> InstitutionDescriptor:
        return self._descriptor

    # ---------------------------------------------------------------- setup
    def initialize(self, scenario, core_state) -> PluginInit:
        st = IAState()
        for tmpl in scenario.principal_templates:
            st.positions[tmpl.principal_id] = "requester" if tmpl.template == "requester" else "provider"
            st.principal_class[tmpl.principal_id] = tmpl.template
        requests = []
        for cap in scenario.capacities:
            if cap.origin == "baseline":
                st.capacity_owner[cap.capacity_id] = cap.source_provider_id
                continue
            cp = copied_principal_id(cap.copy_draw_index)
            st.capacity_owner[cap.capacity_id] = cp
            st.copied_principals.append(cp)
            st.positions[cp] = "provider"
            st.principal_class[cp] = "copied_supplier"
            requests.append(
                PrincipalRequest(
                    cp,
                    "provider",
                    {
                        "principal_class": "copied_supplier",
                        "assigned_capacity_id": cap.capacity_id,
                        "copied_from_provider_id": cap.source_provider_id,
                        "inherited": ["module"],
                        "not_inherited": NOT_INHERITED,
                    },
                )
            )
        return PluginInit(state=st, principals=tuple(requests))

    def register_supply(self, scenario, core_state, st) -> list:
        drafts = []
        for cap in scenario.capacities:
            owner = st.capacity_owner[cap.capacity_id]
            offer = {
                "offer_id": f"O-{cap.capacity_id}",
                "provider_id": owner,
                "capacity_id": cap.capacity_id,
                "scope": "all_tasks",
                "validity": "batch",
                "revocable_during_batch": False,
            }
            st.offers[cap.capacity_id] = offer
            drafts.append(
                EventDraft(
                    "offer_registered",
                    capacity_id=cap.capacity_id,
                    principal_id=owner,
                    details={
                        "offer_id": offer["offer_id"],
                        "capacity_owner_id": owner,
                        "capacity_origin": cap.origin,
                        "source_provider_id": cap.source_provider_id,
                        "module": cap.module,
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
        st.bindings[match.task_id] = {"provider_id": owner, "capacity_id": match.capacity_id, "status": "bound"}
        bound = EventDraft(
            "provider_bound",
            task_id=match.task_id,
            capacity_id=match.capacity_id,
            principal_id=owner,
            executor_id=owner,
            details={
                "offer_id": st.offers[match.capacity_id]["offer_id"],
                "capacity_owner_id": owner,
                "obligor_id": owner,
                "prospective_payee_id": owner,
                "capacity_origin": cap.origin,
                "source_provider_id": cap.source_provider_id,
                "duty": "perform_matched_attempt",
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
        owner = st.capacity_owner[attempt.capacity_id]
        succeeded = verification.outcome == "succeeded"
        if succeeded:
            st.realized_payees[attempt.task_id] = owner
        discharge = EventDraft(
            "obligations_discharged",
            task_id=attempt.task_id,
            capacity_id=attempt.capacity_id,
            principal_id=owner,
            executor_id=owner,
            details={
                "performance_duty": "discharged",
                "verified_outcome": verification.outcome,
                "payment_claim": "satisfied" if succeeded else "lapsed",
                "requester_id": attempt.requester_id,
            },
        )
        return standard_settlement(view, attempt, verification, owner, events=(discharge,))

    def finalize_attempt(self, view, attempt, verification, st) -> list:
        st.bindings[attempt.task_id]["status"] = "discharged"
        return []

    # ----------------------------------------------------------- invariants
    def validate_invariants(self, view, st) -> list:
        v = []
        founders = {t.principal_id for t in view.scenario.principal_templates}
        for cap in view.scenario.capacities:
            owner = st.capacity_owner.get(cap.capacity_id)
            if owner is None or not view.has_account(owner):
                v.append(Violation("capacity_owner", f"{cap.capacity_id} has no owning principal", SOURCE))
                continue
            if cap.origin == "copy":
                if owner not in st.copied_principals or owner == cap.source_provider_id:
                    v.append(Violation("copied_capacity_owner",
                                       f"{cap.capacity_id} is not owned by its copied principal", SOURCE))
            elif owner != cap.source_provider_id:
                v.append(Violation("baseline_capacity_owner", f"{cap.capacity_id} is not owned by its founder", SOURCE))
            offer = st.offers.get(cap.capacity_id)
            if offer is None or offer["provider_id"] != owner:
                v.append(Violation("offer_missing", f"{cap.capacity_id} lacks an offer by its owner", SOURCE))
        copies_owned = Counter(st.capacity_owner[c.capacity_id] for c in view.scenario.capacities if c.origin == "copy")
        if len(set(st.copied_principals)) != len(st.copied_principals):
            v.append(Violation("copied_principal_identity", "copied principals are not distinct", SOURCE))
        for cp in st.copied_principals:
            if cp in founders or not view.has_account(cp) or copies_owned[cp] != 1:
                v.append(Violation("copied_principal_identity",
                                   f"{cp} is not an independent principal owning exactly one copy", SOURCE))
        for attempt in view.attempts():
            owner = st.capacity_owner[attempt.capacity_id]
            if not (
                attempt.executor_kind == "principal"
                and attempt.executor_id == attempt.capacity_owner_id == attempt.prospective_payee_id == owner
            ):
                v.append(Violation("provider_bundle", f"{attempt.task_id} unbundles provider and executor", SOURCE))
            if attempt.payee_id is not None and attempt.payee_id != owner:
                v.append(Violation("payee_rule", f"{attempt.task_id} paid to {attempt.payee_id}", SOURCE))
            if attempt.capacity_origin == "copy" and attempt.payee_id == attempt.source_provider_id:
                v.append(Violation("copy_source_paid", f"{attempt.task_id} paid the copy source", SOURCE))
        v += settlement_violations(view, lambda a: st.capacity_owner[a.capacity_id], SOURCE)
        # Independent accounts: provider balances equal initial balance plus own receipts, nothing inherited.
        expected = {t.principal_id: t.initial_balance for t in view.scenario.principal_templates
                    if t.template == "founding_provider"}
        expected.update({cp: 0 for cp in st.copied_principals})
        for attempt in view.attempts():
            reservation = view.reservation(attempt.task_id)
            if reservation.status == "transferred":
                expected[reservation.payee_id] = expected.get(reservation.payee_id, 0) + reservation.amount
        for pid, amount in expected.items():
            if view.has_account(pid) and view.account(pid).balance != amount:
                v.append(Violation("account_independence", f"{pid} balance is not its own receipts", SOURCE))
        return v

    def summary_fields(self, view, st) -> dict:
        prospective = {a.task_id: a.prospective_payee_id for a in view.attempts()}
        return {
            "principal_count": len(view.account_ids()),
            "positions": dict(sorted(Counter(st.positions.values()).items())),
            "institutional_capacity_owners": dict(sorted(st.capacity_owner.items())),
            "prospective_payees": dict(sorted(prospective.items())),
            "realized_payees": dict(sorted(st.realized_payees.items())),
            "copied_principals_created": list(st.copied_principals),
            "standing_offers_registered": len(st.offers),
        }
