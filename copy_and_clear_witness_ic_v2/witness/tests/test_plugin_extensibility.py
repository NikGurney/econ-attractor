"""Plugin extensibility (spec Section 14.5).

``ITEST`` is a minimal pooled clearing-house institution defined and
registered only here.  All capacities are enrolled in one pool principal that
is created through the kernel; a temporary executor performs each task and the
pool is paid.  It runs through the public registry, kernel, validators and
output writer with no change to any shared module.
"""

import ast
import json
import tempfile
import unittest
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import witness
from witness.app import execute_witness
from witness.institutions.base import (
    common_positions,
    common_trace_requirements,
    fifo_round_robin_match,
    settlement_violations,
    standard_settlement,
)
from witness.institutions.registry import default_registry
from witness.kernel import run_institution
from witness.outputs import render_files, write_files
from witness.records import (
    AttemptAuthorization,
    EventDraft,
    InstitutionDescriptor,
    PluginInit,
    PrincipalRequest,
    Violation,
)
from witness.scenario import generate_scenario, scenario_to_dict
from witness.validation import validate_descriptor, validate_run

from .helpers import SMALL_CONFIG

POOL = "POOL000"


@dataclass
class ITestState:
    enrolled: list = field(default_factory=list)
    cursor: int = 0
    executors: dict = field(default_factory=dict)

    def to_dict(self):
        return {"enrolled": list(self.enrolled), "cursor": self.cursor, "executors": dict(self.executors)}


class PooledClearingHousePlugin:
    institution_id = "ITEST"

    def descriptor(self):
        return InstitutionDescriptor(
            institution_id="ITEST",
            label="Pooled clearing house (test-only)",
            positions_and_assignments={
                "positions": common_positions() + [
                    {"position": "pool", "holders": POOL, "bundled_roles": ["capacity_owner", "payee"]},
                    {"position": "pool_executor", "holders": "E-{task_id}", "is_economic_principal": False},
                ],
                "assignments": {"every_capacity": "owned by the pool", "payee": "the pool"},
                "executor_kinds": ["temporary"],
            },
            relations={"relations": [{"id": "enrollment", "kind": "license", "grantor": "pool",
                                      "holder": "mechanism", "scope": "all_tasks"}]},
            allocation={"task_rule": "FIFO", "capacity_rule": "round-robin",
                        "eligibility": {"kind": "registered_supply", "registration_event": "pool_enrolled",
                                        "task_scope": "all"}},
            information={"event_vocabulary": ["pool_enrolled", "pool_executor_done"],
                         "required_events": ["pool_enrolled", "pool_executor_done"],
                         "observables": ["enrollment", "match", "verification", "settlement"]},
            temporal={"executor_lifetime": "task-bounded", "enrollment_validity": "batch"},
            state={"registries": ["enrollment", "executors"], "executor_states": ["live", "done"]},
            process={"sequence": ["enroll", "match", "execute", "verify", "settle", "release"]},
            value_and_settlement={
                "credit_domain": "task_credit",
                "settlement_rules": {"on_success": "transfer_reservation_to_prospective_payee",
                                     "on_failure": "release_reservation"},
                "payee_rule": "the pool principal",
            },
            external_interfaces={"interfaces": []},
            trace_requirements=common_trace_requirements() + [
                {"id": "ITEST.pool_created", "description": "One pool principal is created with zero balance.",
                 "select": {"event_type": "principal_created", "where": {"details.position": "pool"}},
                 "quantifier": "exactly", "count": 1,
                 "constraints": [{"op": "field_equals", "field": "amount", "value": 0}]},
                {"id": "ITEST.all_enrolled", "description": "Every capacity is enrolled in the pool.",
                 "select": {"event_type": "pool_enrolled"}, "quantifier": "exactly",
                 "count": {"ref": "capacity_count"}, "distinct": "capacity_id",
                 "constraints": [{"op": "field_equals", "field": "principal_id", "value": POOL}]},
                {"id": "ITEST.pool_paid", "description": "The pool receives at least one payment.",
                 "select": {"event_type": "credit_transferred"}, "quantifier": "at_least", "count": 1,
                 "constraints": [{"op": "field_equals", "field": "principal_id", "value": POOL},
                                 {"op": "no_account", "field": "executor_id"}]},
            ],
        )

    def initialize(self, scenario, core_state):
        return PluginInit(state=ITestState(), principals=(PrincipalRequest(POOL, "pool"),))

    def register_supply(self, scenario, core_state, st):
        st.enrolled = [c.capacity_id for c in scenario.capacities]
        return [EventDraft("pool_enrolled", capacity_id=c, principal_id=POOL) for c in st.enrolled]

    def choose_match(self, view, st):
        decision, st.cursor = fifo_round_robin_match(view, st.cursor, lambda cid, tid: cid in st.enrolled)
        return decision

    def authorize_attempt(self, view, match, st):
        eid = f"E-{match.task_id}"
        st.executors[eid] = "live"
        return AttemptAuthorization(eid, "temporary", POOL, POOL)

    def settlement_instruction(self, view, attempt, verification, st):
        return standard_settlement(view, attempt, verification, POOL)

    def finalize_attempt(self, view, attempt, verification, st):
        st.executors[attempt.executor_id] = "done"
        return [EventDraft("pool_executor_done", task_id=attempt.task_id, capacity_id=attempt.capacity_id,
                           principal_id=POOL, executor_id=attempt.executor_id)]

    def validate_invariants(self, view, st):
        v = settlement_violations(view, lambda a: POOL, "plugin:ITEST")
        for eid in st.executors:
            if view.has_account(eid):
                v.append(Violation("executor_account", f"{eid} has an account", "plugin:ITEST"))
        if view.run_finished and "live" in st.executors.values():
            v.append(Violation("executor_live", "executors remain live", "plugin:ITEST"))
        return v

    def summary_fields(self, view, st):
        return {"principal_count": len(view.account_ids()),
                "pool_balance": view.account(POOL).balance,
                "executors": dict(Counter(st.executors.values()))}


class PluginExtensibilityTest(unittest.TestCase):
    def test_itest_runs_through_public_interfaces(self):
        registry = default_registry().copy()
        registry.register("ITEST", PooledClearingHousePlugin)
        self.assertNotIn("ITEST", default_registry())  # registered only inside this test
        plugin = registry.create("ITEST")
        descriptor = plugin.descriptor()
        self.assertEqual(validate_descriptor(descriptor), [])

        scenario = generate_scenario(SMALL_CONFIG)
        result = run_institution(scenario, plugin)
        self.assertEqual(result.status, "completed", result.summary["invariant_violations"])
        report = validate_run(result, scenario)
        self.assertTrue(report["passed"], json.dumps(report, indent=1)[:2000])
        self.assertTrue(all(r["passed"] for r in report["trace_requirements"]))
        self.assertEqual(result.summary["principal_count"], 2 + 4 + 1)
        self.assertEqual(result.summary["pool_balance"], result.summary["tasks_succeeded"])

        with tempfile.TemporaryDirectory() as tmp:
            files = render_files(scenario_to_dict(scenario), {"ITEST": descriptor},
                                 {"ITEST": result.events}, {"ITEST": result.summary})
            write_files(files, tmp)
            path = Path(tmp) / "ITEST_events.jsonl"
            self.assertTrue(path.exists())
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), len(result.events))
            self.assertTrue(all(json.loads(line)["institution"] == "ITEST" for line in lines))
            specs = json.loads((Path(tmp) / "institution_specs.json").read_text(encoding="utf-8"))
            self.assertEqual(list(specs), ["ITEST"])

    def test_itest_through_the_runner_alongside_primary_institutions(self):
        registry = default_registry().copy()
        registry.register("ITEST", PooledClearingHousePlugin)
        run = execute_witness(SMALL_CONFIG, ("IA", "IB", "ITEST"), registry=registry)
        self.assertEqual(set(run.results), {"IA", "IB", "ITEST"})
        self.assertTrue(run.summaries["ITEST"]["valid"])
        self.assertFalse(run.primary)
        # The IA_IB profile still applies and the technical projection is shared by all three.
        self.assertTrue(run.comparisons["IA_IB"]["passed"])
        projections = {iid: [(a.task_id, a.capacity_id, a.expansions, a.completion_theta)
                             for a in r.attempts] for iid, r in run.results.items()}
        self.assertEqual(projections["IA"], projections["ITEST"])


class KernelIsolationTest(unittest.TestCase):
    """Shared modules neither import IA/IB/IC nor compare IDs against their literals."""

    EXEMPT = {"app.py", "registry.py", "ia.py", "ib.py", "ic.py"}
    CONCRETE_MODULES = ("ia", "ib", "ic", "registry")
    INSTITUTION_LITERALS = ("IA", "IB", "IC")

    def shared_modules(self):
        root = Path(witness.__file__).parent
        return [p for p in sorted(root.rglob("*.py")) if p.name not in self.EXEMPT]

    def test_no_institution_imports_or_literals(self):
        modules = self.shared_modules()
        names = {p.name for p in modules}
        for required in ("kernel.py", "ledger.py", "validation.py", "comparisons.py", "outputs.py",
                         "scenario.py", "planners.py", "grids.py", "base.py"):
            self.assertIn(required, names)
        for path in modules:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(alias.name.split(".")[-1] in self.CONCRETE_MODULES, (path, alias.name))
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    self.assertFalse(module.split(".")[-1] in self.CONCRETE_MODULES, (path, module))
                    for alias in node.names:
                        self.assertNotIn(alias.name, self.CONCRETE_MODULES, (path, module))
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotIn(node.value, self.INSTITUTION_LITERALS,
                                     f"{path.name} contains an institution literal")

    def test_kernel_does_not_import_institutions_package(self):
        source = (Path(witness.__file__).parent / "kernel.py").read_text(encoding="utf-8")
        self.assertNotIn("institutions", source)


if __name__ == "__main__":
    unittest.main()
