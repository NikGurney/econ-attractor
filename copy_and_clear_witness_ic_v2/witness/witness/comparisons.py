"""Data-driven cross-institution comparison profiles (spec Section 12.3).

Profiles are declared as data in the application composition root.  This
module evaluates any profile generically: it never names an institution.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from .records import ComparisonProfile, cell_list, thaw

TECHNICAL_FIELDS = (
    "task_id",
    "capacity_id",
    "slot_id",
    "module",
    "start_theta",
    "duration",
    "completion_theta",
    "planner_status",
    "path",
    "expansions",
    "verification_outcome",
    "realized_cost",
    "hops",
)

PAIRED_EVENT_FIELDS = ("seq", "theta", "event_type", "principal_id", "executor_id", "slot_id", "amount")


def technical_projection(result) -> list:
    """Institution-free projection: matches, planner results, verification, timing."""
    rows = []
    for attempt in result.attempts:
        row = {}
        for name in TECHNICAL_FIELDS:
            value = getattr(attempt, name)
            row[name] = cell_list(value) if name == "path" else value
        rows.append(row)
    return rows


def projection_digest(projection) -> str:
    text = json.dumps(projection, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def paired_record(result, task_id: str) -> dict:
    attempt = next(a for a in result.attempts if a.task_id == task_id)
    involved = {attempt.capacity_owner_id, attempt.payee_id, attempt.prospective_payee_id, attempt.executor_id}
    chosen = {}
    for event in result.events:
        if (
            event["task_id"] == task_id
            or (event["task_id"] is None and event["capacity_id"] == attempt.capacity_id)
            or (event["event_type"] == "principal_created" and event["principal_id"] in involved)
        ):
            chosen[event["seq"]] = event
    events = [chosen[s] for s in sorted(chosen)]
    delta = defaultdict(int)
    for event in events:
        if event["event_type"] == "credit_transferred":
            delta[event["details"]["from_principal_id"]] -= event["amount"]
            delta[event["details"]["to_principal_id"]] += event["amount"]
    return {
        "task_id": attempt.task_id,
        "requester_id": attempt.requester_id,
        "capacity_id": attempt.capacity_id,
        "capacity_origin": attempt.capacity_origin,
        "source_provider_id": attempt.source_provider_id,
        "module": attempt.module,
        "slot_id": attempt.slot_id,
        "executor_id": attempt.executor_id,
        "executor_kind": attempt.executor_kind,
        "capacity_owner_id": attempt.capacity_owner_id,
        "prospective_payee_id": attempt.prospective_payee_id,
        "payee_id": attempt.payee_id,
        "planner_status": attempt.planner_status,
        "path": cell_list(attempt.path),
        "expansions": attempt.expansions,
        "verification_outcome": attempt.verification_outcome,
        "realized_cost": attempt.realized_cost,
        "hops": attempt.hops,
        "start_theta": attempt.start_theta,
        "completion_theta": attempt.completion_theta,
        "account_delta": dict(sorted(delta.items())),
        "event_types": [e["event_type"] for e in events],
        "events": [{name: e[name] for name in PAIRED_EVENT_FIELDS} for e in events],
    }


def select_paired_task(selector, members: list, runs: dict):
    if selector.get("kind") != "lowest_task_id":
        raise ValueError(f"unsupported paired_trace_selector kind {selector.get('kind')!r}")
    require = selector.get("require", {})
    same_capacity = selector.get("same_capacity_across_members", True)
    by_member = {m: {a.task_id: a for a in runs[m].attempts} for m in members}
    for task_id in sorted(set.intersection(*(set(v) for v in by_member.values()))):
        attempts = [by_member[m][task_id] for m in members]
        if not all(all(getattr(a, k) == v for k, v in require.items()) for a in attempts):
            continue
        if same_capacity and len({a.capacity_id for a in attempts}) != 1:
            continue
        return task_id
    return None


def _check(inv, passed: bool, detail) -> dict:
    return {"id": inv["id"], "description": inv["description"], "passed": bool(passed), "detail": detail}


def _evaluate_invariant(inv, records: dict, members: list, summaries: dict) -> dict:
    kind = inv["kind"]
    if kind == "same_across_members":
        diffs = {f: {m: records[m][f] for m in members} for f in inv["fields"]
                 if len({json.dumps(records[m][f], sort_keys=True) for m in members}) != 1}
        return _check(inv, not diffs, diffs or "identical")
    if kind == "differ_across_members":
        values = {f: {m: records[m][f] for m in members} for f in inv["fields"]}
        ok = all(len({json.dumps(v, sort_keys=True) for v in vals.values()}) == len(members) for vals in values.values())
        return _check(inv, ok, values)
    member = inv["member"]
    record = records[member]
    if kind == "member_fields_equal":
        values = {f: record[f] for f in inv["fields"]}
        return _check(inv, None not in values.values() and len(set(values.values())) == 1, values)
    if kind == "member_fields_distinct":
        values = {f: record[f] for f in inv["fields"]}
        return _check(inv, None not in values.values() and len(set(values.values())) == len(values), values)
    if kind == "member_field_equals":
        return _check(inv, record[inv["field"]] == inv["value"], {inv["field"]: record[inv["field"]]})
    if kind in ("member_field_in_summary", "member_field_not_in_summary"):
        pool = summaries[member].get(inv["summary_key"], [])
        present = record[inv["field"]] in pool
        ok = present if kind == "member_field_in_summary" else not present
        return _check(inv, ok, {inv["field"]: record[inv["field"]], "summary_key": inv["summary_key"]})
    if kind == "member_no_account":
        accounts = summaries[member].get("final_account_balances", {})
        return _check(inv, record[inv["field"]] not in accounts, {inv["field"]: record[inv["field"]]})
    if kind == "member_event_subsequence":
        wanted, position = list(inv["event_types"]), 0
        for event_type in record["event_types"]:
            if position < len(wanted) and event_type == wanted[position]:
                position += 1
        return _check(inv, position == len(wanted), {"required_order": wanted, "matched": position})
    raise ValueError(f"unsupported comparison invariant kind {kind!r}")


def evaluate_profile(profile: ComparisonProfile, runs: dict, summaries: dict) -> dict:
    members = list(profile.institution_ids)
    report = {"profile": profile.to_dict(), "checks": [], "paired_trace": None, "passed": False}
    missing = [m for m in members if m not in runs]
    if missing:
        report["checks"].append({"id": "members_executed", "description": "Every member was executed.",
                                 "passed": False, "detail": {"missing": missing}})
        return report
    checks = report["checks"]
    hashes = {m: runs[m].scenario_hash for m in members}
    checks.append({"id": "identical_scenario_input", "description": "Every member ran the identical immutable scenario.",
                   "passed": len(set(hashes.values())) == 1, "detail": hashes})
    completed = {m: runs[m].status for m in members}
    checks.append({"id": "members_completed", "description": "Every member run completed.",
                   "passed": all(s == "completed" for s in completed.values()), "detail": completed})
    if profile.require_same_technical_projection:
        digests = {m: projection_digest(technical_projection(runs[m])) for m in members}
        checks.append({"id": "identical_technical_projection",
                       "description": "Task-capacity matches, planner results, expansions, verification outcomes "
                                      "and completion times are identical across members.",
                       "passed": len(set(digests.values())) == 1, "detail": digests})
    task_id = select_paired_task(thaw(profile.paired_trace_selector), members, runs)
    checks.append({"id": "paired_task_selected", "description": "A qualifying paired task exists.",
                   "passed": task_id is not None, "detail": {"task_id": task_id}})
    if task_id is not None:
        records = {m: paired_record(runs[m], task_id) for m in members}
        report["paired_trace"] = {"task_id": task_id, "capacity_id": records[members[0]]["capacity_id"],
                                  "members": records}
        for inv in thaw(profile.comparison_invariants):
            checks.append(_evaluate_invariant(inv, records, members, summaries))
    report["passed"] = all(c["passed"] for c in checks)
    return report
