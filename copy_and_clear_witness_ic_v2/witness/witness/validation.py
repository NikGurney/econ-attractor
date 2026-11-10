"""Common, institution-agnostic validators.

* ``validate_descriptor`` - structural completeness of an InstitutionDescriptor.
* ``evaluate_trace_requirements`` - evaluates a descriptor's declarative
  ``trace_requirements`` against a completed trace and final state.
* ``validate_scenario`` - immutable-scenario invariants (e.g. no copied
  capacity is ever a copy source).
* ``audit_trace`` - independent replay of the event trace: schema, sequence,
  ledger conservation, reservation/settlement pairing, slot and capacity
  exclusivity, single attempt per task.
* ``validate_run`` - combines the above into a per-execution report.

Nothing in this module knows which institutions exist.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Mapping, Sequence

from .config import MODULE_LABELS
from .grids import canonical_grid_hash
from .records import (
    DESCRIPTOR_FIELDS,
    EVENT_FIELDS,
    EVENT_TYPE_PATTERN,
    EXECUTOR_KINDS,
    INSTITUTION_ID_PATTERN,
    KERNEL_EVENT_TYPES,
    SUPPORTED_ELIGIBILITY_KINDS,
    SUPPORTED_SETTLEMENT_RULES,
    TERMINAL_TASK_STATES,
    InstitutionDescriptor,
    Violation,
    thaw,
)

MISSING = object()

QUANTIFIERS = ("at_least", "exactly", "all")
REFERENCE_NAMES = (
    "task_count",
    "capacity_count",
    "baseline_capacity_count",
    "copied_capacity_count",
    "requester_count",
    "founding_provider_count",
    "slot_count",
)
CONSTRAINT_KEYS = {
    "fields_equal": ("fields",),
    "fields_distinct": ("fields",),
    "field_equals": ("field", "value"),
    "field_in_events": ("field", "select", "source_field"),
    "field_not_in_events": ("field", "select", "source_field"),
    "followed_by": ("select", "join"),
    "preceded_by": ("select", "join"),
    "has_account": ("field",),
    "no_account": ("field",),
    "capacity_available_at_end": ("field",),
}


# =================================================================== helpers
def get_path(obj, path: str):
    current = obj
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return MISSING
    return current


def _json_equal(a, b) -> bool:
    if a is MISSING or b is MISSING:
        return False
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return thaw(a) == thaw(b)


def _event_types(selector) -> list:
    value = selector.get("event_type")
    return [value] if isinstance(value, str) else list(value or ())


def _where_ok(event, where) -> bool:
    for path, expected in (where or {}).items():
        value = get_path(event, path)
        if isinstance(expected, Mapping) and "in" in expected:
            if value is MISSING or not any(_json_equal(value, option) for option in expected["in"]):
                return False
        elif not _json_equal(value, expected):
            return False
    return True


# ====================================================== descriptor validation
def _validate_selector(selector, allowed_types, where_label) -> list:
    errors = []
    if not isinstance(selector, Mapping):
        return [f"{where_label}: selector must be an object"]
    types = _event_types(selector)
    if not types:
        errors.append(f"{where_label}: selector needs event_type")
    for t in types:
        if t not in allowed_types:
            errors.append(f"{where_label}: event type {t!r} is neither kernel-owned nor declared")
    where = selector.get("where", {})
    if not isinstance(where, Mapping):
        errors.append(f"{where_label}: where must be an object")
    unknown = set(selector) - {"event_type", "where"}
    if unknown:
        errors.append(f"{where_label}: unknown selector keys {sorted(unknown)}")
    return errors


def validate_trace_requirement(req, allowed_types) -> list:
    if not isinstance(req, Mapping):
        return ["trace requirement must be an object"]
    rid = req.get("id")
    label = f"trace requirement {rid!r}"
    errors = []
    if not isinstance(rid, str) or not rid:
        errors.append("trace requirement without id")
    if not isinstance(req.get("description"), str) or not req.get("description"):
        errors.append(f"{label}: missing description")
    errors += _validate_selector(req.get("select"), allowed_types, label)
    if req.get("quantifier") not in QUANTIFIERS:
        errors.append(f"{label}: quantifier must be one of {QUANTIFIERS}")
    count = req.get("count")
    if isinstance(count, Mapping):
        if count.get("ref") not in REFERENCE_NAMES:
            errors.append(f"{label}: unknown count reference {count.get('ref')!r}")
    elif isinstance(count, bool) or not isinstance(count, int) or count < 0:
        errors.append(f"{label}: count must be a nonnegative integer or a reference")
    if "distinct" in req and not isinstance(req["distinct"], str):
        errors.append(f"{label}: distinct must be a field path")
    for constraint in req.get("constraints", ()):
        if not isinstance(constraint, Mapping) or constraint.get("op") not in CONSTRAINT_KEYS:
            errors.append(f"{label}: unknown constraint {constraint!r}")
            continue
        for key in CONSTRAINT_KEYS[constraint["op"]]:
            if key not in constraint:
                errors.append(f"{label}: constraint {constraint['op']} missing {key!r}")
        if "select" in constraint:
            errors += _validate_selector(constraint["select"], allowed_types, f"{label}/{constraint['op']}")
        if "join" in constraint and (not isinstance(constraint["join"], Mapping) or not constraint["join"]):
            errors.append(f"{label}: join must be a non-empty object")
    unknown = set(req) - {"id", "description", "select", "quantifier", "count", "distinct", "constraints"}
    if unknown:
        errors.append(f"{label}: unknown keys {sorted(unknown)}")
    return errors


def validate_descriptor(desc) -> list:
    """Return a list of human-readable errors; empty means complete and well formed."""
    if not isinstance(desc, InstitutionDescriptor):
        return ["descriptor is not an InstitutionDescriptor"]
    errors = []
    iid = desc.institution_id
    if not isinstance(iid, str) or not INSTITUTION_ID_PATTERN.match(iid):
        errors.append(f"institution_id {iid!r} does not match [A-Z][A-Z0-9_]*")
    if not isinstance(desc.label, str) or not desc.label.strip():
        errors.append("label must be a non-empty string")
    for name in DESCRIPTOR_FIELDS[2:-1]:
        value = getattr(desc, name)
        if not isinstance(value, Mapping) or not value:
            errors.append(f"coordinate {name} is missing or empty")
    if errors:
        return errors
    try:
        json.dumps(desc.to_dict(), sort_keys=True)
    except (TypeError, ValueError) as exc:
        errors.append(f"descriptor is not JSON-serializable: {exc}")

    pa = desc.positions_and_assignments
    if not isinstance(pa.get("positions"), Sequence) or not pa.get("positions"):
        errors.append("positions_and_assignments.positions must be a non-empty list")
    kinds = pa.get("executor_kinds")
    if not isinstance(kinds, Sequence) or not kinds or any(k not in EXECUTOR_KINDS for k in kinds):
        errors.append(f"positions_and_assignments.executor_kinds must be a non-empty subset of {EXECUTOR_KINDS}")
    if not pa.get("assignments"):
        errors.append("positions_and_assignments.assignments is required")

    if not isinstance(desc.relations.get("relations"), Sequence) or not desc.relations.get("relations"):
        errors.append("relations.relations must be a non-empty list")

    info = desc.information
    vocab = info.get("event_vocabulary")
    if not isinstance(vocab, Sequence) or isinstance(vocab, str):
        errors.append("information.event_vocabulary must be a list")
        vocab = ()
    if len(set(vocab)) != len(vocab):
        errors.append("information.event_vocabulary contains duplicates")
    for event_type in vocab:
        if not isinstance(event_type, str) or not EVENT_TYPE_PATTERN.match(event_type):
            errors.append(f"event type {event_type!r} does not match [a-z][a-z0-9_]*")
        elif event_type in KERNEL_EVENT_TYPES:
            errors.append(f"plugin vocabulary may not redeclare kernel event type {event_type!r}")
    required = info.get("required_events", ())
    for event_type in required:
        if event_type not in vocab:
            errors.append(f"required event {event_type!r} is not in the declared vocabulary")
    if not info.get("observables"):
        errors.append("information.observables is required")
    allowed_types = set(KERNEL_EVENT_TYPES) | set(vocab)

    eligibility = desc.allocation.get("eligibility")
    if not isinstance(eligibility, Mapping):
        errors.append("allocation.eligibility is required")
    else:
        if eligibility.get("kind") not in SUPPORTED_ELIGIBILITY_KINDS:
            errors.append(f"allocation.eligibility.kind must be one of {SUPPORTED_ELIGIBILITY_KINDS}")
        if eligibility.get("registration_event") not in vocab:
            errors.append("allocation.eligibility.registration_event must be a declared event type")
        if eligibility.get("task_scope") != "all":
            errors.append("allocation.eligibility.task_scope must be 'all' (bounded interface)")

    if not isinstance(desc.state.get("executor_states"), Sequence):
        errors.append("state.executor_states must be a list (possibly empty)")
    sequence = desc.process.get("sequence")
    if not isinstance(sequence, Sequence) or not sequence:
        errors.append("process.sequence must be a non-empty list")

    vs = desc.value_and_settlement
    if vs.get("credit_domain") != "task_credit":
        errors.append("value_and_settlement.credit_domain must be 'task_credit'")
    rules = vs.get("settlement_rules")
    if not isinstance(rules, Mapping):
        errors.append("value_and_settlement.settlement_rules is required")
    else:
        for key, supported in SUPPORTED_SETTLEMENT_RULES.items():
            if rules.get(key) not in supported:
                errors.append(f"settlement rule {key} must be one of {supported}")
    if not vs.get("payee_rule"):
        errors.append("value_and_settlement.payee_rule is required")

    interfaces = desc.external_interfaces.get("interfaces")
    if not isinstance(interfaces, Sequence) or isinstance(interfaces, str):
        errors.append("external_interfaces.interfaces must be a list (possibly empty)")

    reqs = desc.trace_requirements
    if not isinstance(reqs, Sequence) or not reqs:
        errors.append("trace_requirements must be a non-empty list")
    else:
        ids = [r.get("id") for r in reqs if isinstance(r, Mapping)]
        if len(ids) != len(set(ids)):
            errors.append("trace requirement ids must be unique")
        for req in reqs:
            errors += validate_trace_requirement(req, allowed_types)
    return errors


# ================================================ trace requirement evaluator
class TraceIndex:
    def __init__(self, events, accounts, capacity_states):
        self.events = events
        self.accounts = set(accounts)
        self.capacity_states = dict(capacity_states)
        self.by_type = defaultdict(list)
        for event in events:
            self.by_type[event["event_type"]].append(event)
        self._cache = {}

    def select(self, selector) -> list:
        key = ("select", json.dumps(thaw(selector), sort_keys=True))
        if key not in self._cache:
            chosen = []
            for event_type in _event_types(selector):
                chosen.extend(self.by_type.get(event_type, ()))
            chosen.sort(key=lambda e: e["seq"])
            where = selector.get("where", {})
            self._cache[key] = [e for e in chosen if _where_ok(e, where)]
        return self._cache[key]

    def value_set(self, selector, source_field) -> list:
        return [get_path(e, source_field) for e in self.select(selector)]

    def _joined(self, event, selector, join, later: bool) -> bool:
        for other in self.select(selector):
            if later and other["seq"] <= event["seq"]:
                continue
            if not later and other["seq"] >= event["seq"]:
                continue
            if all(_json_equal(get_path(event, a), get_path(other, b)) for a, b in join.items()):
                return True
        return False

    def satisfies(self, constraint, event) -> bool:
        op = constraint["op"]
        if op == "fields_equal":
            values = [get_path(event, f) for f in constraint["fields"]]
            if any(v is MISSING or v is None for v in values):
                return False
            return all(_json_equal(values[0], v) for v in values[1:])
        if op == "fields_distinct":
            values = [get_path(event, f) for f in constraint["fields"]]
            if any(v is MISSING or v is None for v in values):
                return False
            return all(
                not _json_equal(values[i], values[j])
                for i in range(len(values))
                for j in range(i + 1, len(values))
            )
        if op == "field_equals":
            return _json_equal(get_path(event, constraint["field"]), constraint["value"])
        if op in ("field_in_events", "field_not_in_events"):
            value = get_path(event, constraint["field"])
            if value is MISSING:
                return False
            present = any(
                _json_equal(value, v) for v in self.value_set(constraint["select"], constraint["source_field"])
            )
            return present if op == "field_in_events" else not present
        if op == "followed_by":
            return self._joined(event, constraint["select"], constraint["join"], later=True)
        if op == "preceded_by":
            return self._joined(event, constraint["select"], constraint["join"], later=False)
        if op == "has_account":
            return get_path(event, constraint["field"]) in self.accounts
        if op == "no_account":
            value = get_path(event, constraint["field"])
            return value is not MISSING and value is not None and value not in self.accounts
        if op == "capacity_available_at_end":
            return self.capacity_states.get(get_path(event, constraint["field"])) == "available"
        raise ValueError(f"unknown constraint op {op!r}")


def resolve_count(count, reference_counts) -> int:
    if isinstance(count, Mapping):
        return int(reference_counts[count["ref"]])
    return int(count)


def evaluate_trace_requirements(descriptor, events, accounts, capacity_states, reference_counts) -> list:
    index = TraceIndex(events, accounts, capacity_states)
    results = []
    for req in descriptor.trace_requirements:
        required = resolve_count(req["count"], reference_counts)
        selected = index.select(req["select"])
        constraints = req.get("constraints", ())
        satisfying, failing = [], []
        for event in selected:
            (satisfying if all(index.satisfies(c, event) for c in constraints) else failing).append(event)
        distinct = req.get("distinct")

        def count(evs):
            if distinct:
                return len({json.dumps(thaw(get_path(e, distinct)), sort_keys=True, default=str) for e in evs})
            return len(evs)

        quantifier = req["quantifier"]
        if quantifier == "at_least":
            passed = count(satisfying) >= required
        elif quantifier == "exactly":
            passed = count(satisfying) == required and count(selected) == required
        else:  # all
            passed = not failing and count(selected) >= required
        results.append(
            {
                "id": req["id"],
                "description": req["description"],
                "quantifier": quantifier,
                "required_count": required,
                "selected": count(selected),
                "satisfying": count(satisfying),
                "passed": passed,
                "evidence_seq": satisfying[0]["seq"] if satisfying else None,
                "first_failing_seq": failing[0]["seq"] if failing else None,
            }
        )
    return results


# ======================================================= scenario invariants
def validate_scenario(scenario) -> list:
    v = []
    config = scenario.config
    founders = [t.principal_id for t in scenario.principal_templates if t.template == "founding_provider"]
    requesters = [t.principal_id for t in scenario.principal_templates if t.template == "requester"]
    capacity_ids = {c.capacity_id for c in scenario.capacities}
    modules = dict(scenario.module_assignment)
    if len(scenario.tasks) != config.task_count:
        v.append(Violation("scenario_task_count", f"{len(scenario.tasks)} tasks, expected {config.task_count}"))
    if len(scenario.capacities) != config.capacity_count:
        v.append(Violation("scenario_capacity_count", "capacity count differs from configuration"))
    if len(capacity_ids) != len(scenario.capacities):
        v.append(Violation("duplicate_capacity_id", "capacity ids are not unique"))
    if set(modules.values()) != set(MODULE_LABELS):
        v.append(Violation("module_label_absent", "not every module label occurs among founders"))
    for cap in scenario.capacities:
        if cap.origin not in ("baseline", "copy"):
            v.append(Violation("capacity_origin", f"{cap.capacity_id} has origin {cap.origin!r}"))
        if cap.source_provider_id not in founders:
            v.append(
                Violation(
                    "copied_capacity_used_as_source",
                    f"{cap.capacity_id} has source {cap.source_provider_id!r}, which is not a founding provider",
                )
            )
        elif cap.module != modules.get(cap.source_provider_id):
            v.append(Violation("copy_fidelity", f"{cap.capacity_id} module differs from its source"))
        if cap.max_concurrency != 1:
            v.append(Violation("max_concurrency", f"{cap.capacity_id} max_concurrency != 1"))
    per_requester = Counter(t.requester_id for t in scenario.tasks)
    if any(per_requester.get(r, 0) != config.tasks_per_requester for r in requesters):
        v.append(Violation("unbalanced_requesters", "task/requester assignment is not balanced"))
    hashes = set()
    for index, task in enumerate(scenario.tasks):
        if task.requester_id != requesters[index % len(requesters)]:
            v.append(Violation("requester_assignment", f"{task.task_id} has requester {task.requester_id}"))
        if task.reward != config.reward:
            v.append(Violation("reward", f"{task.task_id} reward {task.reward}"))
        if not (config.min_reference_hops <= task.reference_hops <= config.max_reference_hops):
            v.append(Violation("route_threshold", f"{task.task_id} reference hops {task.reference_hops}"))
        recomputed = canonical_grid_hash(task.size, task.start, task.goal, task.blocked, task.cost)
        if recomputed != task.grid_hash:
            v.append(Violation("grid_hash", f"{task.task_id} grid hash does not match its grid"))
        if task.grid_hash in hashes:
            v.append(Violation("duplicate_grid", f"{task.task_id} duplicates an earlier grid"))
        hashes.add(task.grid_hash)
    return v


# ============================================================== trace audit
def audit_trace(result, scenario) -> list:
    """Replay the completed trace independently of the kernel's own bookkeeping."""
    v = []
    events = result.events
    allowed = set(KERNEL_EVENT_TYPES) | set(result.descriptor.event_vocabulary)
    previous_theta = 0
    for index, event in enumerate(events):
        if set(event) != set(EVENT_FIELDS):
            v.append(Violation("event_schema", f"event {index} fields {sorted(event)}"))
            continue
        if event["schema_version"] != "1.0":
            v.append(Violation("event_schema", f"event {index} schema_version"))
        if event["seq"] != index:
            v.append(Violation("event_sequence", f"event {index} has seq {event['seq']}"))
        theta = event["theta"]
        if isinstance(theta, bool) or not isinstance(theta, int) or theta < previous_theta:
            v.append(Violation("event_theta", f"event {index} theta {theta!r} after {previous_theta}"))
        else:
            previous_theta = theta
        if event["institution"] != result.institution_id:
            v.append(Violation("event_institution", f"event {index} institution {event['institution']!r}"))
        if event["event_type"] not in allowed:
            v.append(Violation("unregistered_event_type", f"event {index} type {event['event_type']!r}"))
        for name in ("task_id", "capacity_id", "principal_id", "executor_id", "slot_id"):
            if event[name] is not None and not isinstance(event[name], str):
                v.append(Violation("event_schema", f"event {index} {name} is not a string or null"))
        amount = event["amount"]
        if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
            v.append(Violation("event_schema", f"event {index} amount is not an integer or null"))
        if not isinstance(event["details"], dict):
            v.append(Violation("event_schema", f"event {index} details is not an object"))
    if v:
        return v

    balances, reserved, reservations = {}, {}, {}
    initial_total = 0
    started, verified = {}, {}
    active_caps, active_slots = {}, {}
    attempts_per_task, settlements_per_task = Counter(), Counter()
    slot_count = len(scenario.slot_ids)
    kinds = Counter()
    for event in events:
        kind = event["event_type"]
        kinds[kind] += 1
        tid, cid, pid, sid = event["task_id"], event["capacity_id"], event["principal_id"], event["slot_id"]
        amount = event["amount"]
        if kind == "principal_created":
            if pid in balances:
                v.append(Violation("duplicate_principal", f"{pid} created twice"))
            balances[pid], reserved[pid] = amount, 0
            initial_total += amount
        elif kind == "credit_reserved":
            if tid in reservations:
                v.append(Violation("duplicate_reservation", f"{tid} reserved twice"))
            reservations[tid] = {"principal": pid, "amount": amount, "status": "open"}
            reserved[pid] = reserved.get(pid, 0) + amount
            if balances.get(pid, 0) - reserved[pid] < 0:
                v.append(Violation("negative_available", f"{pid} over-reserved at seq {event['seq']}"))
        elif kind == "attempt_started":
            attempts_per_task[tid] += 1
            if cid in active_caps:
                v.append(Violation("capacity_double_assigned", f"{cid} active twice at seq {event['seq']}"))
            if sid in active_slots:
                v.append(Violation("slot_double_occupied", f"{sid} occupied twice at seq {event['seq']}"))
            active_caps[cid], active_slots[sid] = tid, tid
            started[tid] = event
            if len(active_caps) > slot_count:
                v.append(Violation("slot_capacity_exceeded", f"{len(active_caps)} active at seq {event['seq']}"))
        elif kind in ("verification_succeeded", "verification_failed"):
            if tid in verified:
                v.append(Violation("double_verification", f"{tid} verified twice"))
            if tid not in started:
                v.append(Violation("verification_without_attempt", f"{tid} verified without an attempt"))
            verified[tid] = "succeeded" if kind == "verification_succeeded" else "failed"
        elif kind == "credit_transferred":
            settlements_per_task[tid] += 1
            res = reservations.get(tid)
            if res is None or res["status"] != "open":
                v.append(Violation("payment_without_reservation", f"{tid} paid without an open reservation"))
                continue
            if verified.get(tid) != "succeeded":
                v.append(Violation("payment_without_verified_success", f"{tid} paid without verified success"))
            attempt = started.get(tid)
            if attempt is None or attempt["details"].get("prospective_payee_id") != pid:
                v.append(Violation("unauthorized_payee", f"{tid} paid to {pid}, not the authorized payee"))
            if amount != res["amount"]:
                v.append(Violation("amount_mismatch", f"{tid} transfer amount differs from reservation"))
            payer = res["principal"]
            balances[payer] -= amount
            reserved[payer] -= amount
            if pid not in balances:
                v.append(Violation("payment_to_non_principal", f"{tid} paid to {pid}, which has no account"))
                balances[pid] = 0
            balances[pid] += amount
            res["status"] = "transferred"
        elif kind == "credit_released":
            settlements_per_task[tid] += 1
            res = reservations.get(tid)
            if res is None or res["status"] != "open":
                v.append(Violation("release_without_reservation", f"{tid} released without an open reservation"))
                continue
            if verified.get(tid) != "failed":
                v.append(Violation("release_without_verified_failure", f"{tid} released without verified failure"))
            reserved[res["principal"]] -= amount
            res["status"] = "released"
        elif kind == "capacity_released":
            if active_caps.get(cid) != tid or active_slots.get(sid) != tid:
                v.append(Violation("release_mismatch", f"{cid}/{sid} released for {tid} but not active for it"))
            active_caps.pop(cid, None)
            active_slots.pop(sid, None)
        for principal, balance in balances.items():
            if balance < 0 or reserved.get(principal, 0) < 0 or balance - reserved.get(principal, 0) < 0:
                v.append(Violation("negative_balance", f"{principal} negative at seq {event['seq']}"))
                break
        if kind in ("credit_transferred", "credit_released") and sum(balances.values()) != initial_total:
            v.append(Violation("credit_conservation", f"credit not conserved at seq {event['seq']}"))

    for tid, count in attempts_per_task.items():
        if count > 1:
            v.append(Violation("task_attempted_twice", f"{tid} attempted {count} times"))
    for tid, state in result.task_states.items():
        if state in TERMINAL_TASK_STATES and settlements_per_task[tid] != 1:
            v.append(Violation("settlement_count", f"{tid} has {settlements_per_task[tid]} settlements"))
    replayed = {pid: {"balance": balances[pid], "reserved": reserved.get(pid, 0)} for pid in balances}
    if replayed != result.accounts:
        v.append(Violation("ledger_replay_mismatch", "replayed balances differ from final ledger"))
    ledger_ops = Counter(entry["op"] for entry in result.ledger_log)
    if (
        ledger_ops["reserve"] != kinds["credit_reserved"]
        or ledger_ops["transfer_reserved"] != kinds["credit_transferred"]
        or ledger_ops["release_reservation"] != kinds["credit_released"]
        or ledger_ops["create_account"] != kinds["principal_created"]
    ):
        v.append(Violation("unrecorded_ledger_operation", "ledger operations and ledger events differ"))
    for event_type in result.descriptor.information.get("required_events", ()):
        if kinds[event_type] == 0:
            v.append(Violation("required_event_absent", f"declared required event {event_type!r} never occurred"))
    if kinds["invariant_violation"] != len(result.violations):
        v.append(Violation("violation_record_mismatch", "invariant_violation events differ from recorded violations"))
    if result.status == "completed":
        if kinds["run_completed"] != 1 or events[-1]["event_type"] != "run_completed":
            v.append(Violation("run_completed_missing", "completed run must end with one run_completed event"))
        if set(verified) != {t.task_id for t in scenario.tasks}:
            v.append(Violation("unverified_tasks", "not every task was verified"))
        if active_caps or active_slots:
            v.append(Violation("active_at_end", "capacities or slots remain active at end"))
    return v


# ============================================================ run validation
def validate_run(result, scenario) -> dict:
    descriptor_errors = validate_descriptor(result.descriptor)
    audit = audit_trace(result, scenario) if not descriptor_errors else []
    requirements = (
        evaluate_trace_requirements(
            result.descriptor,
            result.events,
            result.accounts.keys(),
            result.capacity_states,
            scenario.reference_counts(),
        )
        if not descriptor_errors
        else []
    )
    summary = result.summary
    completion = {
        "run_completed": result.status == "completed",
        "all_tasks_terminal": summary["tasks_terminal"] == summary["tasks_posted"] == len(scenario.tasks),
        "no_reservations_remain": summary["final_reserved_credits"] == 0,
        "no_slots_occupied": summary["final_occupied_slots"] == 0,
        "credit_conserved": summary["initial_total_credits"] == summary["final_total_credits"],
        "no_invariant_violations": summary["invariant_violation_count"] == 0,
    }
    passed = (
        not descriptor_errors
        and not audit
        and all(r["passed"] for r in requirements)
        and all(completion.values())
    )
    return {
        "descriptor_errors": descriptor_errors,
        "trace_audit": {"passed": not audit, "violations": [x.to_dict() for x in audit]},
        "trace_requirements": requirements,
        "completion_checks": completion,
        "passed": passed,
    }
