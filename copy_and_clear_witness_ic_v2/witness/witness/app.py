"""Application composition root.

Defines the primary institution set, the primary ``IA_IB`` and ``IA_IC``
comparison profiles, and the structural acceptance criteria (spec Section
13.2 plus addendum Section 11), all as data.  It is the only module besides
the institution registry that may name concrete institutions.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from .comparisons import evaluate_profile
from .config import PRIMARY_CONFIG, ConfigurationError, ScenarioConfig
from .institutions.registry import InstitutionRegistry, RegistryError, default_registry
from .kernel import KernelConfigurationError, run_institution
from .outputs import json_bytes, render_descriptors, render_files, write_files
from .records import ComparisonProfile, thaw
from .scenario import ScenarioGenerationError, generate_scenario, scenario_to_dict
from .validation import validate_descriptor, validate_run

PRIMARY_INSTITUTIONS = ("IA", "IB", "IC")

IA_IB_PROFILE = ComparisonProfile(
    profile_id="IA_IB",
    institution_ids=("IA", "IB"),
    require_same_technical_projection=True,
    paired_trace_selector={
        "kind": "lowest_task_id",
        "require": {
            "capacity_origin": "copy",
            "verification_outcome": "succeeded",
            "settlement_kind": "transferred",
        },
        "same_capacity_across_members": True,
    },
    comparison_invariants=[
        {
            "id": "same_task_capacity_and_technical_result",
            "description": "Both members run the same copied task-capacity pair with the same technical result.",
            "kind": "same_across_members",
            "fields": ["task_id", "requester_id", "capacity_id", "capacity_origin", "source_provider_id", "module",
                       "slot_id", "planner_status", "path", "expansions", "verification_outcome",
                       "realized_cost", "hops", "start_theta", "completion_theta"],
        },
        {
            "id": "different_institutional_facts",
            "description": "Executor identity and type, capacity owner and payee differ between members.",
            "kind": "differ_across_members",
            "fields": ["executor_id", "executor_kind", "capacity_owner_id", "payee_id"],
        },
        {
            "id": "IA_provider_bundle",
            "description": "IA: executor, capacity owner, prospective payee and payee are one principal.",
            "kind": "member_fields_equal",
            "member": "IA",
            "fields": ["executor_id", "capacity_owner_id", "prospective_payee_id", "payee_id"],
        },
        {
            "id": "IA_executor_is_principal",
            "description": "IA: the executor is a persistent economic principal.",
            "kind": "member_field_equals",
            "member": "IA",
            "field": "executor_kind",
            "value": "principal",
        },
        {
            "id": "IA_payee_is_copied_principal",
            "description": "IA: the payee for the copied capacity is its copied principal.",
            "kind": "member_field_in_summary",
            "member": "IA",
            "field": "payee_id",
            "summary_key": "copied_principals_created",
        },
        {
            "id": "IA_payee_is_not_copy_source",
            "description": "IA: the copy source is not paid.",
            "kind": "member_fields_distinct",
            "member": "IA",
            "fields": ["payee_id", "source_provider_id"],
        },
        {
            "id": "IA_institutional_path",
            "description": "IA: offer, post and reserve, bind provider, execute, verify, settle, release.",
            "kind": "member_event_subsequence",
            "member": "IA",
            "event_types": ["offer_registered", "task_posted", "credit_reserved", "provider_bound", "task_matched",
                            "attempt_started", "attempt_completed", "verification_succeeded",
                            "credit_transferred", "capacity_released"],
        },
        {
            "id": "IB_owner_is_licensor_and_payee",
            "description": "IB: capacity owner, prospective payee and payee are the founding source owner.",
            "kind": "member_fields_equal",
            "member": "IB",
            "fields": ["capacity_owner_id", "prospective_payee_id", "payee_id", "source_provider_id"],
        },
        {
            "id": "IB_executor_is_temporary",
            "description": "IB: the executor is a temporary, non-principal position.",
            "kind": "member_field_equals",
            "member": "IB",
            "field": "executor_kind",
            "value": "temporary",
        },
        {
            "id": "IB_executor_is_not_payee",
            "description": "IB: capacity owner differs from the temporary executor.",
            "kind": "member_fields_distinct",
            "member": "IB",
            "fields": ["executor_id", "payee_id"],
        },
        {
            "id": "IB_executor_has_no_account",
            "description": "IB: the temporary executor never has an account.",
            "kind": "member_no_account",
            "member": "IB",
            "field": "executor_id",
        },
        {
            "id": "IB_institutional_path",
            "description": "IB: license, post and reserve, instantiate executor, execute, verify, settle, "
                           "terminate, release.",
            "kind": "member_event_subsequence",
            "member": "IB",
            "event_types": ["license_registered", "task_posted", "credit_reserved", "executor_instantiated",
                            "task_matched", "attempt_started", "attempt_completed", "verification_succeeded",
                            "credit_transferred", "executor_terminated", "capacity_released"],
        },
    ],
)


def _ia_ib_invariants(*ids) -> list:
    """Reuse IA_IB invariants verbatim, so IA's provider-bundle path is defined in one place."""
    by_id = {inv["id"]: inv for inv in thaw(IA_IB_PROFILE.comparison_invariants)}
    return [by_id[i] for i in ids]


IA_IC_PROFILE = ComparisonProfile(
    profile_id="IA_IC",
    institution_ids=("IA", "IC"),
    require_same_technical_projection=True,
    paired_trace_selector=thaw(IA_IB_PROFILE.paired_trace_selector),
    comparison_invariants=_ia_ib_invariants(
        "same_task_capacity_and_technical_result",
        "IA_provider_bundle",
        "IA_executor_is_principal",
        "IA_payee_is_copied_principal",
        "IA_payee_is_not_copy_source",
        "IA_institutional_path",
    )
    + [
        {
            "id": "IC_successor_bundle",
            "description": "IC: executor, capacity owner, prospective payee and payee are one successor-principal.",
            "kind": "member_fields_equal",
            "member": "IC",
            "fields": ["executor_id", "capacity_owner_id", "prospective_payee_id", "payee_id"],
        },
        {
            "id": "IC_executor_is_principal",
            "description": "IC: the copied-capacity executor is a persistent economic principal.",
            "kind": "member_field_equals",
            "member": "IC",
            "field": "executor_kind",
            "value": "principal",
        },
        {
            "id": "IC_payee_is_fission_successor",
            "description": "IC: the payee for the copied capacity is the fission successor created for it.",
            "kind": "member_field_in_summary",
            "member": "IC",
            "field": "payee_id",
            "summary_key": "fission_successors_created",
        },
        {
            "id": "IC_payee_is_not_copy_source",
            "description": "IC: the source successor is a co-obligor but is not paid.",
            "kind": "member_fields_distinct",
            "member": "IC",
            "fields": ["payee_id", "source_provider_id"],
        },
        {
            "id": "IC_institutional_path",
            "description": "IC: fission, inherited commitment, successor offer, successor-set binding, execute, "
                           "verify, settle, resolve joint duty, release.",
            "kind": "member_event_subsequence",
            "member": "IC",
            "event_types": ["principal_fissioned", "successor_commitment_inherited", "successor_offer_registered",
                            "successor_set_bound", "task_matched", "attempt_started", "attempt_completed",
                            "verification_succeeded", "credit_transferred", "joint_duty_resolved",
                            "capacity_released"],
        },
        {
            "id": "different_institutional_paths",
            "description": "The two paired traces follow different institutional event paths.  IC's fission, "
                           "inherited-commitment and successor-set events cannot occur in IA's trace: they are "
                           "outside IA's declared event vocabulary, which the kernel and trace audit enforce.",
            "kind": "differ_across_members",
            "fields": ["event_types"],
        },
    ],
)
PRIMARY_PROFILES = (IA_IB_PROFILE, IA_IC_PROFILE)


def _primary_acceptance() -> tuple:
    rows = [
        ("IA", "principal_count", "==", 40),
        ("IB", "principal_count", "==", 24),
        ("IC", "principal_count", "==", 40),
    ]
    for iid in PRIMARY_INSTITUTIONS:
        rows += [
            (iid, "capacity_count", "==", 32),
            (iid, "tasks_posted", "==", 64),
            (iid, "distinct_capacities_used", "==", 32),
            (iid, "max_simultaneous_slot_occupancy", "==", 4),
            (iid, "tasks_terminal", "==", 64),
            (iid, "final_reserved_credits", "==", 0),
            (iid, "final_occupied_slots", "==", 0),
            (iid, "initial_total_credits", "==", 64),
            (iid, "final_total_credits", "==", 64),
            (iid, "invariant_violation_count", "==", 0),
            (iid, "copied_capacity_successes", ">=", 1),
        ]
    rows.append(("IB", "temporary_executors_active_at_end", "==", 0))
    rows += [
        ("IC", "fission_successor_count", "==", 16),
        ("IC", "fission_events", "==", 16),
        ("IC", "standing_commitments_registered", "==", 16),
        ("IC", "inherited_commitments", "==", 16),
        ("IC", "joint_duties_bound", "==", 64),
        ("IC", "joint_duties_resolved", "==", 64),
        ("IC", "multi_member_duties_bound", ">=", 1),
        ("IC", "copied_successor_payments", ">=", 1),
    ]
    return tuple({"institution": i, "field": f, "op": op, "expected": x} for i, f, op, x in rows)


PRIMARY_ACCEPTANCE = _primary_acceptance()


class WitnessConfigurationError(ValueError):
    pass


@dataclass
class WitnessRun:
    config: ScenarioConfig
    scenario: object
    institution_ids: tuple
    descriptors: dict
    descriptor_bytes: bytes
    results: dict
    summaries: dict
    comparisons: dict
    acceptance: list
    primary: bool
    nonprimary_reasons: list
    valid: bool
    validity_statement: dict = field(default_factory=dict)


def _compare(observed, op, expected) -> bool:
    if op == "==":
        return observed == expected
    if op == ">=":
        return observed is not None and observed >= expected
    raise ValueError(f"unsupported acceptance operator {op!r}")


def evaluate_acceptance(criteria, summaries: dict) -> list:
    results = []
    for row in criteria:
        if row["institution"] not in summaries:
            continue
        observed = summaries[row["institution"]].get(row["field"])
        results.append({**row, "observed": observed, "passed": _compare(observed, row["op"], row["expected"])})
    return results


def _validity_statement(summaries: dict, comparisons: dict) -> dict:
    s = list(summaries.values())
    return {
        "all_tasks_terminal": all(x["tasks_terminal"] == x["tasks_posted"] for x in s),
        "copied_capacity_produced_settled_exchange": all(x["copied_capacity_successes"] >= 1 for x in s),
        "credits_conserved": all(x["initial_total_credits"] == x["final_total_credits"] for x in s),
        "no_invariant_violations": all(x["invariant_violation_count"] == 0 for x in s),
        "no_reservations_remaining": all(x["final_reserved_credits"] == 0 for x in s),
        "no_slots_occupied": all(x["final_occupied_slots"] == 0 for x in s),
        "no_temporary_executors_remaining": all(x.get("temporary_executors_active_at_end", 0) == 0 for x in s),
        "comparison_profiles_passed": all(c["passed"] for c in comparisons.values()),
    }


def execute_witness(
    config: ScenarioConfig = PRIMARY_CONFIG,
    institution_ids=PRIMARY_INSTITUTIONS,
    *,
    registry: Optional[InstitutionRegistry] = None,
    profiles=PRIMARY_PROFILES,
    acceptance=PRIMARY_ACCEPTANCE,
    planners=None,
) -> WitnessRun:
    registry = registry or default_registry()
    ids = tuple(institution_ids)
    if not ids:
        raise WitnessConfigurationError("at least one institution is required")
    if len(set(ids)) != len(ids):
        raise WitnessConfigurationError(f"duplicate institution IDs requested: {list(ids)}")
    plugins = {iid: registry.create(iid) for iid in ids}

    descriptors = {}
    for iid, plugin in plugins.items():
        descriptor = plugin.descriptor()
        errors = validate_descriptor(descriptor)
        if errors:
            raise WitnessConfigurationError(f"{iid}: incomplete descriptor: " + "; ".join(errors))
        if descriptor.institution_id != iid:
            raise WitnessConfigurationError(f"{iid}: descriptor declares {descriptor.institution_id!r}")
        descriptors[iid] = descriptor
    descriptor_bytes = render_descriptors(descriptors)  # serialized before any execution

    scenario = generate_scenario(config)
    results = {iid: run_institution(scenario, plugins[iid], planners) for iid in ids}
    summaries = {}
    for iid in ids:
        validation = validate_run(results[iid], scenario)
        summaries[iid] = {**results[iid].summary, "validation": validation, "valid": validation["passed"]}

    comparisons = {
        p.profile_id: evaluate_profile(p, results, summaries)
        for p in profiles
        if set(p.institution_ids) <= set(ids)
    }
    reasons = []
    if not config.is_primary():
        reasons.append("scenario configuration differs from the normative primary configuration")
    if ids != PRIMARY_INSTITUTIONS:
        reasons.append(f"institution set {list(ids)} differs from {list(PRIMARY_INSTITUTIONS)}")
    acceptance_results = evaluate_acceptance(acceptance, summaries) if config.is_primary() else []
    statement = _validity_statement(summaries, comparisons)
    valid = (
        all(s["valid"] for s in summaries.values())
        and all(c["passed"] for c in comparisons.values())
        and all(a["passed"] for a in acceptance_results)
        and all(statement.values())
    )
    return WitnessRun(
        config=config,
        scenario=scenario,
        institution_ids=ids,
        descriptors=descriptors,
        descriptor_bytes=descriptor_bytes,
        results=results,
        summaries=summaries,
        comparisons=comparisons,
        acceptance=acceptance_results,
        primary=not reasons,
        nonprimary_reasons=reasons,
        valid=valid,
        validity_statement=statement,
    )


def build_summary(run: WitnessRun) -> dict:
    summary = {iid: run.summaries[iid] for iid in run.institution_ids}
    summary["comparisons"] = run.comparisons
    summary["run"] = {
        "label": "PRIMARY" if run.primary else "NONPRIMARY",
        "primary": run.primary,
        "nonprimary_reasons": run.nonprimary_reasons,
        "master_seed": run.config.master_seed,
        "institutions": list(run.institution_ids),
        "comparison_profiles": sorted(run.comparisons),
        "scenario_hash": run.scenario.scenario_hash,
        "acceptance_criteria": run.acceptance,
        "validity_statement": run.validity_statement,
        "valid": run.valid,
        "interpretation": (
            "Constructive consistency and existence witness under one declared finite scenario. "
            "Differences between institutions are institutional (positions, rights, state, process, payees), "
            "not evidence of superiority; no elapsed-time, success-rate or concentration outcome is primary."
        ),
    }
    return summary


def render_witness(run: WitnessRun) -> dict:
    files = render_files(
        scenario_to_dict(run.scenario),
        run.descriptors,
        {iid: run.results[iid].events for iid in run.institution_ids},
        build_summary(run),
    )
    if files["institution_specs.json"] != run.descriptor_bytes:
        raise RuntimeError("descriptors changed during execution")
    for iid in run.institution_ids:
        files[f"diagnostics/{iid}_final_state.json"] = json_bytes(run.results[iid].plugin_state)
    return files


# ---------------------------------------------------------------- reporting
def _fmt_delta(delta: dict) -> str:
    return ", ".join(f"{pid} {amount:+d}" for pid, amount in delta.items())


def format_report(run: WitnessRun, output_dir, replay_identical: Optional[bool]) -> str:
    sc = run.scenario
    counts = sc.reference_counts()
    label = "PRIMARY" if run.primary else "NONPRIMARY"
    lines = []
    if not run.primary:
        lines += ["=" * 72, "  NONPRIMARY RUN: " + "; ".join(run.nonprimary_reasons), "=" * 72]
    lines.append(f"Copy-and-clear witness [{label}]  seed={run.config.master_seed}  "
                 f"institutions={','.join(run.institution_ids)}")
    lines.append(f"scenario {sc.scenario_hash[:16]}  {counts['task_count']} tasks | {counts['capacity_count']} "
                 f"capacities ({counts['copied_capacity_count']} copied) | {counts['slot_count']} slots")
    lines.append("")
    for iid in run.institution_ids:
        s = run.summaries[iid]
        reqs = s["validation"]["trace_requirements"]
        line = (
            f"  {iid:<6} {s['run_status']:<10} principals={s.get('principal_count', '?')}  "
            f"terminal={s['tasks_terminal']}/{s['tasks_posted']}  succeeded={s['tasks_succeeded']} "
            f"failed={s['tasks_failed']}  copied successes={s['copied_capacity_successes']}  "
            f"credits {s['initial_total_credits']}->{s['final_total_credits']}  "
            f"violations={s['invariant_violation_count']}  "
            f"trace reqs {sum(r['passed'] for r in reqs)}/{len(reqs)}  {'PASS' if s['valid'] else 'FAIL'}"
        )
        lines.append(line)
        if "temporary_executors_instantiated" in s:
            lines.append(f"         executors instantiated={s['temporary_executors_instantiated']} "
                         f"terminated={s['temporary_executors_terminated']} "
                         f"active at end={s['temporary_executors_active_at_end']}")
        if "fission_events" in s:
            lines.append(f"         fissions={s['fission_events']} inherited commitments={s['inherited_commitments']} "
                         f"joint duties bound={s['joint_duties_bound']} discharged={s['joint_duties_discharged']} "
                         f"failed={s['joint_duties_failed']} multi-member={s['multi_member_duties_bound']}")
        for msg in s["invariant_violations"][:5]:
            lines.append(f"         ! {msg}")
        for r in reqs:
            if not r["passed"]:
                lines.append(f"         ! trace requirement failed: {r['id']}")
        for v in s["validation"]["trace_audit"]["violations"][:5]:
            lines.append(f"         ! audit: {v['code']}: {v['message']}")
    for pid, report in run.comparisons.items():
        lines.append("")
        lines.append(f"  profile {pid}: {'PASS' if report['passed'] else 'FAIL'}")
        for check in report["checks"]:
            if not check["passed"]:
                lines.append(f"         ! {check['id']}: {json.dumps(check['detail'], sort_keys=True)[:200]}")
        pt = report.get("paired_trace")
        if pt:
            first = next(iter(pt["members"].values()))
            lines.append(f"    paired task {pt['task_id']} on copied capacity {pt['capacity_id']} "
                         f"(source {first['source_provider_id']}, module {first['module']}, "
                         f"{first['hops']} moves, cost {first['realized_cost']}, completes at theta "
                         f"{first['completion_theta']})")
            for mid, rec in pt["members"].items():
                lines.append(f"      {mid:<4} executor {rec['executor_id']} ({rec['executor_kind']})  owner "
                             f"{rec['capacity_owner_id']}  payee {rec['payee_id']}  [{_fmt_delta(rec['account_delta'])}]")
    if run.acceptance:
        met = sum(a["passed"] for a in run.acceptance)
        lines.append("")
        lines.append(f"  Structural acceptance (spec 13.2 + IC addendum 11): {met}/{len(run.acceptance)} criteria met")
        for a in run.acceptance:
            if not a["passed"]:
                lines.append(f"         ! {a['institution']}.{a['field']} = {a['observed']!r}, "
                             f"expected {a['op']} {a['expected']!r}")
    if replay_identical is not None:
        lines.append(f"  replay: {'byte-identical' if replay_identical else 'OUTPUTS DIFFER'}")
    lines.append("")
    ok = run.valid and replay_identical is not False
    lines.append(f"{'WITNESS VALID' if ok else 'WITNESS INVALID'}  [{label}]  outputs -> {output_dir}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the copy-and-clear institutional witness.")
    parser.add_argument("--institutions", nargs="+", metavar="ID",
                        help=f"registered institution IDs to execute (default: {' '.join(PRIMARY_INSTITUTIONS)})")
    parser.add_argument("--seed", type=int, help="development only: nonprimary master seed")
    parser.add_argument("--output-dir", help="output directory (default: outputs/ for the primary run)")
    parser.add_argument("--force", action="store_true",
                        help="allow a nonprimary run to overwrite a directory holding primary results")
    parser.add_argument("--skip-replay-check", action="store_true",
                        help="do not re-execute the witness to confirm byte-identical outputs")
    return parser


def _holds_primary_results(directory: Path) -> bool:
    path = directory / "summary.json"
    if not path.exists():
        return False
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("run", {}).get("primary"))
    except (ValueError, OSError):
        return False


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    ids = tuple(args.institutions) if args.institutions else PRIMARY_INSTITUTIONS
    config = PRIMARY_CONFIG if args.seed is None else replace(PRIMARY_CONFIG, master_seed=args.seed)
    primary = config.is_primary() and ids == PRIMARY_INSTITUTIONS
    if args.output_dir:
        output_dir = Path(args.output_dir)
    elif primary:
        output_dir = Path("outputs")
    else:
        output_dir = Path("outputs_nonprimary") / f"seed{config.master_seed}_{'-'.join(ids)}"
    if not primary and _holds_primary_results(output_dir) and not args.force:
        print(f"refusing to overwrite primary results in {output_dir} with a NONPRIMARY run "
              f"(choose another --output-dir or pass --force)", file=sys.stderr)
        return 2
    try:
        run = execute_witness(config, ids)
        files = render_witness(run)
        write_files(files, output_dir)
        replay_identical = None
        if not args.skip_replay_check:
            replay_identical = render_witness(execute_witness(config, ids)) == files
    except (ConfigurationError, RegistryError, WitnessConfigurationError, KernelConfigurationError,
            ScenarioGenerationError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    print(format_report(run, output_dir, replay_identical))
    return 0 if run.valid and replay_identical is not False else 1
