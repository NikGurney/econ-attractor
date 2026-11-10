"""Institution IC: fission-and-succession market (IC addendum Section 13)."""

import ast
import dataclasses
import json
import random
import tempfile
import unittest
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

import witness
from witness import app
from witness.app import IA_IB_PROFILE, execute_witness
from witness.comparisons import technical_projection
from witness.config import ScenarioConfig
from witness.institutions.ic import EVENT_VOCABULARY, FissionSuccessionPlugin
from witness.institutions.registry import RegistryError, default_registry
from witness.kernel import run_institution
from witness.outputs import jsonl_bytes
from witness.planners import PLANNERS, PlanResult
from witness.records import DESCRIPTOR_FIELDS, KERNEL_EVENT_TYPES
from witness.scenario import assemble_scenario
from witness.validation import validate_descriptor, validate_run

from .helpers import make_task, primary_run, primary_scenario, quiet
from .test_scenario import COPY_CHECKPOINT

IC_ONLY_EVENTS = {"principal_fissioned", "successor_commitment_inherited", "successor_set_bound"}


def lineage_fixture():
    """Four founders; both copy draws come from P000, so LIN-P000 has three members."""
    config = ScenarioConfig(master_seed=1, requester_count=1, founding_provider_count=4, copy_count=2,
                            tasks_per_requester=6, slot_count=2, requester_initial_balance=6, grid_size=5,
                            min_reference_hops=1, max_reference_hops=20)
    corridor = lambda r: ["....." if i == r else "#####" for i in range(5)]
    tasks = [make_task(f"T{i:03d}", "R000", corridor(i), (i, 0), (i, 4)) for i in range(5)]
    tasks.append(make_task("T005", "R000", corridor(0), (0, 4), (0, 0)))
    return assemble_scenario(config, ("greedy", "bfs", "weighted", "beam"), ("P000", "P000"), tuple(tasks),
                             {"modules": 0, "copies": 0, "tasks": 0})


def events_of(result, event_type):
    return [e for e in result.events if e["event_type"] == event_type]


# ================================================================ registry
class ICDescriptorRegistryTest(unittest.TestCase):
    def test_registered_exactly_once(self):
        registry = default_registry()
        self.assertEqual(registry.ids().count("IC"), 1)
        self.assertIsInstance(registry.create("IC"), FissionSuccessionPlugin)
        with self.assertRaises(RegistryError):
            registry.copy().register("IC", FissionSuccessionPlugin)

    def test_descriptor_validates_and_serializes(self):
        desc = FissionSuccessionPlugin().descriptor()
        self.assertEqual(validate_descriptor(desc), [])
        data = desc.to_dict()
        self.assertEqual(list(data), list(DESCRIPTOR_FIELDS))
        self.assertEqual(json.loads(json.dumps(data)), data)
        self.assertEqual(data["institution_id"], "IC")
        self.assertEqual(data["external_interfaces"]["interfaces"], [])
        self.assertEqual(data["value_and_settlement"]["settlement_rules"],
                         {"on_success": "transfer_reservation_to_prospective_payee",
                          "on_failure": "release_reservation"})
        self.assertEqual(data["positions_and_assignments"]["executor_kinds"], ["principal"])

    def test_event_vocabulary_declared_and_used(self):
        desc = FissionSuccessionPlugin().descriptor()
        vocab = set(desc.event_vocabulary)
        self.assertEqual(list(desc.event_vocabulary), EVENT_VOCABULARY)
        self.assertEqual(len(vocab), 6)
        self.assertTrue(set(desc.information["required_events"]) <= vocab)
        self.assertFalse(vocab & set(KERNEL_EVENT_TYPES))
        used = {e["event_type"] for e in primary_run().results["IC"].events}
        self.assertTrue(used <= set(KERNEL_EVENT_TYPES) | vocab)
        self.assertTrue(vocab <= used)

    def test_descriptor_serialized_before_execution(self):
        specs = json.loads(primary_run().descriptor_bytes)
        self.assertEqual(sorted(specs), ["IA", "IB", "IC"])
        self.assertEqual(specs["IC"], FissionSuccessionPlugin().descriptor().to_dict())


# ================================================================ lineages
class ICLineageTest(unittest.TestCase):
    def test_two_copies_of_one_founder_form_a_three_member_lineage(self):
        scenario = lineage_fixture()
        result = run_institution(scenario, FissionSuccessionPlugin())
        self.assertEqual(result.status, "completed", result.summary["invariant_violations"])
        self.assertTrue(validate_run(result, scenario)["passed"])
        s = result.summary
        self.assertEqual(s["lineage_members"]["P000"], ["FS000", "FS001", "P000"])
        for root in ("P001", "P002", "P003"):
            self.assertEqual(s["lineage_members"][root], [root])  # founders without copies stay singletons
        self.assertEqual((s["institutional_capacity_owners"]["C004"], s["institutional_capacity_owners"]["C005"]),
                         ("FS000", "FS001"))
        by_capacity = {e["capacity_id"]: e["details"]["co_obligor_ids"] for e in events_of(result, "successor_set_bound")}
        for cid in ("C000", "C004", "C005"):
            self.assertEqual(by_capacity[cid], ["FS000", "FS001", "P000"])
        self.assertEqual(by_capacity["C001"], ["P001"])
        self.assertEqual(s["multi_member_duties_bound"], 3)
        self.assertEqual(s["principal_count"], 1 + 4 + 2)

    def test_primary_lineages_follow_copy_draws(self):
        result = primary_run().results["IC"]
        expected = {f"P{i:03d}": [f"P{i:03d}"] for i in range(16)}
        for k, source in enumerate(COPY_CHECKPOINT):
            expected[source].append(f"FS{k:03d}")
        self.assertEqual(result.summary["lineage_members"], {root: sorted(m) for root, m in expected.items()})
        for root in ("P002", "P005", "P008", "P014"):
            self.assertEqual(result.summary["lineage_members"][root], [root])
        self.assertEqual(result.summary["lineage_members"]["P001"], ["FS001", "FS005", "P001"])
        state = result.plugin_state
        for k, source in enumerate(COPY_CHECKPOINT):
            cid, sid = f"C{16 + k:03d}", f"FS{k:03d}"
            self.assertEqual(state["capacity_owner"][cid], sid)
            self.assertEqual(state["successor_for_capacity"][cid], sid)
            self.assertEqual(state["lineage_root_of"][sid], source)
        for i in range(16):
            self.assertEqual(state["capacity_owner"][f"C{i:03d}"], f"P{i:03d}")

    def test_copy_order_does_not_depend_on_iteration(self):
        scenario = primary_scenario()
        plugin = FissionSuccessionPlugin()
        reference = plugin.initialize(scenario, None)
        shuffled_caps = list(scenario.capacities)
        random.Random(3).shuffle(shuffled_caps)
        for capacities in (tuple(reversed(scenario.capacities)), tuple(shuffled_caps)):
            other = plugin.initialize(
                SimpleNamespace(principal_templates=scenario.principal_templates, capacities=capacities), None)
            self.assertEqual(other.state.to_dict(), reference.state.to_dict())
            self.assertEqual(other.principals, reference.principals)
            self.assertEqual(other.events, reference.events)
        self.assertEqual([p.principal_id for p in reference.principals], [f"FS{k:03d}" for k in range(16)])
        text = json.dumps(reference.state.to_dict(), sort_keys=True)  # no sets; sorted lists only
        for members in reference.state.to_dict()["members_by_root"].values():
            self.assertEqual(members, sorted(members))
        self.assertIsInstance(text, str)


# ================================================== institutional behaviour
class ICBehaviourTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ic = primary_run().results["IC"]

    def of(self, event_type):
        return events_of(self.ic, event_type)

    def test_one_standing_commitment_per_founder(self):
        events = self.of("standing_commitment_registered")
        self.assertEqual([e["principal_id"] for e in events], [f"P{i:03d}" for i in range(16)])
        self.assertEqual(len({e["details"]["commitment_id"] for e in events}), 16)

    def test_one_fission_then_one_inheritance_per_copy(self):
        fissions, inherits = self.of("principal_fissioned"), self.of("successor_commitment_inherited")
        copies = [f"C{16 + k:03d}" for k in range(16)]
        self.assertEqual([e["capacity_id"] for e in fissions], copies)
        self.assertEqual([e["capacity_id"] for e in inherits], copies)
        by_seq = {e["seq"]: e for e in self.ic.events}
        for k, fission in enumerate(fissions):
            self.assertEqual((fission["principal_id"], fission["details"]["successor_id"]),
                             (COPY_CHECKPOINT[k], f"FS{k:03d}"))
            following = by_seq[fission["seq"] + 1]
            self.assertEqual(following["event_type"], "successor_commitment_inherited")
            self.assertEqual((following["capacity_id"], following["principal_id"]),
                             (fission["capacity_id"], fission["details"]["successor_id"]))
        # Administrative account shells precede the normative fission events (documented ordering).
        shells = [e["seq"] for e in self.of("principal_created") if e["details"].get("principal_class") == "fission_successor"]
        self.assertLess(max(shells), min(e["seq"] for e in self.of("standing_commitment_registered")))

    def test_successor_inherits_commitment_but_not_balance(self):
        state = self.ic.plugin_state
        created = {e["principal_id"]: e for e in self.of("principal_created")}
        receipts = Counter(e["principal_id"] for e in self.of("credit_transferred"))
        for k, root in enumerate(COPY_CHECKPOINT):
            sid = f"FS{k:03d}"
            self.assertEqual(created[sid]["amount"], 0)
            self.assertEqual(created[sid]["details"]["succession_status"], "dormant_until_fission")
            self.assertIn(sid, state["standing_commitments"][root]["members"])
            self.assertEqual(self.ic.accounts[sid]["balance"], receipts[sid])
        for i in range(16):
            pid = f"P{i:03d}"
            self.assertEqual(self.ic.accounts[pid]["balance"], receipts[pid])
        self.assertTrue(all(e["details"]["balance_inherited"] is False for e in self.of("successor_commitment_inherited")))

    def test_copied_binding_records_source_and_successor_as_co_obligors(self):
        lineages = self.ic.summary["lineage_members"]
        bindings = [e for e in self.of("successor_set_bound") if e["details"]["capacity_origin"] == "copy"]
        self.assertEqual(len(bindings), 32)
        for event in bindings:
            d = event["details"]
            self.assertIn(d["source_provider_id"], d["co_obligor_ids"])
            self.assertIn(event["executor_id"], d["co_obligor_ids"])
            self.assertNotEqual(event["executor_id"], d["source_provider_id"])
            self.assertEqual(d["co_obligor_ids"], lineages[d["source_provider_id"]])

    def test_selected_successor_is_owner_executor_and_payee(self):
        for attempt in self.ic.attempts:
            self.assertEqual(attempt.executor_kind, "principal")
            self.assertEqual(len({attempt.executor_id, attempt.capacity_owner_id, attempt.prospective_payee_id,
                                  attempt.payee_id}), 1)
            if attempt.capacity_origin == "copy":
                self.assertEqual(attempt.executor_id, f"FS{int(attempt.capacity_id[1:]) - 16:03d}")
        self.assertEqual(self.ic.summary["realized_payees"], {a.task_id: a.executor_id for a in self.ic.attempts})

    def test_one_success_resolves_the_one_duty_with_one_payment(self):
        transfers = defaultdict(list)
        for event in self.of("credit_transferred"):
            transfers[event["task_id"]].append(event)
        bound = {e["task_id"]: e for e in self.of("successor_set_bound")}
        resolved = {e["task_id"]: e for e in self.of("joint_duty_resolved")}
        self.assertEqual(len(resolved), 64)
        for tid, event in resolved.items():
            d = event["details"]
            self.assertEqual(d["duty_id"], f"JD-{tid}")
            self.assertEqual(d["co_obligor_ids"], bound[tid]["details"]["co_obligor_ids"])
            self.assertEqual((d["duty_status"], d["payment_claim_status"], d["discharger_id"]),
                             ("discharged", "satisfied", event["executor_id"]))
            self.assertEqual(len(transfers[tid]), 1)
            self.assertEqual((transfers[tid][0]["principal_id"], transfers[tid][0]["amount"]), (event["executor_id"], 1))
            self.assertLess(transfers[tid][0]["seq"], event["seq"])
            self.assertEqual(self.ic.plugin_state["bindings"][tid]["status"], "discharged")

    def test_no_sibling_paid_for_membership(self):
        paid_for, executed, co_obligated = defaultdict(set), defaultdict(set), defaultdict(set)
        for event in self.of("credit_transferred"):
            paid_for[event["principal_id"]].add(event["task_id"])
        for attempt in self.ic.attempts:
            executed[attempt.executor_id].add(attempt.task_id)
        for event in self.of("successor_set_bound"):
            for member in event["details"]["co_obligor_ids"]:
                co_obligated[member].add(event["task_id"])
        for member, tasks in co_obligated.items():
            self.assertEqual(paid_for[member], executed[member])
        membership_only = sum(len(tasks - executed[m]) for m, tasks in co_obligated.items())
        self.assertGreater(membership_only, 0, "fixture must contain co-obligations without execution")


class ICFailureTest(unittest.TestCase):
    def test_explicit_failure_releases_and_fails_the_joint_duty(self):
        scenario = primary_scenario()
        planners = dict(PLANNERS, beam=lambda p: PlanResult("failed", None, 7))
        result = run_institution(scenario, FissionSuccessionPlugin(), planners)
        self.assertEqual(result.status, "completed", result.summary["invariant_violations"])
        self.assertTrue(validate_run(result, scenario)["passed"])
        failed = {a.task_id: a for a in result.attempts if a.module == "beam"}
        self.assertTrue(any(a.capacity_origin == "copy" for a in failed.values()))
        released = {e["task_id"] for e in events_of(result, "credit_released")}
        transferred = {e["task_id"] for e in events_of(result, "credit_transferred")}
        resolved = {e["task_id"]: e["details"] for e in events_of(result, "joint_duty_resolved")}
        self.assertEqual(released, set(failed))
        self.assertFalse(transferred & set(failed))
        for tid, attempt in failed.items():
            d = resolved[tid]
            self.assertEqual((d["verified_outcome"], d["duty_status"], d["payment_claim_status"], d["discharger_id"]),
                             ("failed", "failed", "lapsed", None))
            self.assertEqual(result.plugin_state["bindings"][tid]["status"], "failed")
            self.assertIsNone(attempt.payee_id)
            self.assertNotIn(tid, result.summary["realized_payees"])
        s = result.summary
        self.assertEqual(s["joint_duties_failed"], len(failed))
        self.assertEqual(s["joint_duties_discharged"] + s["joint_duties_failed"], 64)
        self.assertEqual((s["final_total_credits"], s["final_reserved_credits"]), (64, 0))


# ======================================================= invariant enforcement
class ICInvariantEnforcementTest(unittest.TestCase):
    def assertFatal(self, plugin, code):
        result = run_institution(primary_scenario(), plugin)
        self.assertEqual(result.status, "violated")
        self.assertIn(code, [v.code for v in result.violations])

    def test_incomplete_co_obligor_set_is_fatal(self):
        class DropsCoObligors(FissionSuccessionPlugin):
            def authorize_attempt(self, view, match, st):
                auth = super().authorize_attempt(view, match, st)
                st.bindings[match.task_id]["co_obligor_ids"] = [auth.executor_id]
                return auth

        self.assertFatal(DropsCoObligors(), "co_obligor_set")

    def test_sibling_as_payee_is_fatal(self):
        class PaysSource(FissionSuccessionPlugin):
            def authorize_attempt(self, view, match, st):
                auth = super().authorize_attempt(view, match, st)
                root = st.lineage_root_of[auth.executor_id]
                return dataclasses.replace(auth, prospective_payee_id=root) if root != auth.executor_id else auth

        self.assertFatal(PaysSource(), "successor_bundle")

    def test_wrong_lineage_membership_is_fatal(self):
        class WrongLineage(FissionSuccessionPlugin):
            def initialize(self, scenario, core_state):
                init = super().initialize(scenario, core_state)
                init.state.members_by_root["P002"].append("FS000")  # FS000 is a copy of P013
                return init

        self.assertFatal(WrongLineage(), "lineage_members")

    def test_unresolved_joint_duty_is_fatal(self):
        class NeverResolves(FissionSuccessionPlugin):
            def finalize_attempt(self, view, attempt, verification, st):
                return []

        self.assertFatal(NeverResolves(), "joint_duty_status")

    def test_dropped_resolution_events_fail_trace_requirements(self):
        scenario = primary_scenario()
        result = run_institution(scenario, FissionSuccessionPlugin())
        events = [e for e in result.events if e["event_type"] != "joint_duty_resolved"]
        report = validate_run(dataclasses.replace(result, events=events), scenario)
        self.assertFalse(report["passed"])
        failed = {r["id"] for r in report["trace_requirements"] if not r["passed"]}
        self.assertTrue({"IC.every_task_duty_resolved", "IC.copied_successor_exercised"} <= failed)


# ============================================================ compatibility
class ICCompatibilityTest(unittest.TestCase):
    def test_all_three_complete_with_identical_technical_projection(self):
        run = primary_run()
        self.assertEqual(run.institution_ids, ("IA", "IB", "IC"))
        self.assertTrue(run.primary and run.valid)
        projections = {iid: technical_projection(result) for iid, result in run.results.items()}
        self.assertEqual(projections["IA"], projections["IC"])
        self.assertEqual(projections["IB"], projections["IC"])
        for iid, s in run.summaries.items():
            self.assertTrue(s["valid"], iid)
            self.assertEqual((s["run_status"], s["tasks_terminal"], s["final_reserved_credits"],
                              s["final_occupied_slots"], s["final_total_credits"], s["invariant_violation_count"]),
                             ("completed", 64, 0, 0, 64, 0), iid)
            self.assertGreaterEqual(s["copied_capacity_successes"], 1)
            self.assertEqual(set(run.results[iid].capacity_states.values()), {"available"})
        ic = run.summaries["IC"]
        self.assertEqual((ic["principal_count"], len(ic["fission_successors_created"]), ic["fission_events"],
                          ic["inherited_commitments"], ic["joint_duties_bound"]), (40, 16, 16, 16, 64))
        self.assertEqual(ic["joint_duties_discharged"] + ic["joint_duties_failed"], 64)
        self.assertGreaterEqual(ic["multi_member_duties_bound"], 1)
        self.assertGreaterEqual(ic["copied_successor_payments"], 1)
        self.assertTrue(all(a["passed"] for a in run.acceptance))
        self.assertTrue(any(a["institution"] == "IC" for a in run.acceptance))

    def test_ia_ib_profile_unchanged_by_ic(self):
        three, two = primary_run(), execute_witness(institution_ids=("IA", "IB"))
        self.assertFalse(two.primary)
        encode = lambda x: json.dumps(x, sort_keys=True, indent=2)
        self.assertEqual(sorted(two.comparisons), ["IA_IB"])
        self.assertEqual(encode(three.comparisons["IA_IB"]), encode(two.comparisons["IA_IB"]))
        self.assertEqual(three.comparisons["IA_IB"]["profile"], IA_IB_PROFILE.to_dict())
        for iid in ("IA", "IB"):
            self.assertEqual(jsonl_bytes(three.results[iid].events), jsonl_bytes(two.results[iid].events))

    def test_ia_ic_profile_identical_execution_different_paths(self):
        report = primary_run().comparisons["IA_IC"]
        self.assertTrue(report["passed"], [c for c in report["checks"] if not c["passed"]])
        checks = {c["id"] for c in report["checks"]}
        self.assertTrue({"identical_technical_projection", "IA_institutional_path", "IC_institutional_path",
                         "different_institutional_paths", "IC_payee_is_fission_successor"} <= checks)
        pt = report["paired_trace"]
        ia, ic = pt["members"]["IA"], pt["members"]["IC"]
        self.assertEqual((pt["task_id"], pt["capacity_id"], ic["source_provider_id"]), ("T016", "C016", "P013"))
        for key in ("slot_id", "module", "path", "expansions", "verification_outcome", "start_theta",
                    "completion_theta", "realized_cost"):
            self.assertEqual(ia[key], ic[key], key)
        self.assertEqual((ia["executor_kind"], ic["executor_kind"]), ("principal", "principal"))
        self.assertEqual((ia["payee_id"], ic["payee_id"]), ("CP000", "FS000"))
        self.assertEqual(ic["account_delta"], {"FS000": 1, "R000": -1})  # co-obligor P013 is not paid
        self.assertTrue(IC_ONLY_EVENTS <= set(ic["event_types"]))
        self.assertFalse(IC_ONLY_EVENTS & set(ia["event_types"]))
        ia_vocab = set(default_registry().create("IA").descriptor().event_vocabulary)
        self.assertFalse(IC_ONLY_EVENTS & ia_vocab)  # absence in IA is structural, not incidental

    def test_ic_trace_independent_of_execution_order(self):
        scenario = primary_scenario()
        first = run_institution(scenario, FissionSuccessionPlugin()).events
        run_institution(scenario, default_registry().create("IA"))
        self.assertEqual(run_institution(scenario, FissionSuccessionPlugin()).events, first)

    def test_outputs_include_ic_and_replay_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "a", Path(tmp) / "b"
            self.assertEqual(quiet(app.main, ["--output-dir", str(first), "--skip-replay-check"]), 0)
            self.assertEqual(quiet(app.main, ["--output-dir", str(second), "--skip-replay-check"]), 0)
            self.assertTrue((first / "IC_events.jsonl").is_file())
            self.assertTrue((first / "diagnostics" / "IC_final_state.json").is_file())
            specs = json.loads((first / "institution_specs.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(specs), ["IA", "IB", "IC"])
            summary = json.loads((first / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["run"]["institutions"], ["IA", "IB", "IC"])
            self.assertEqual((summary["run"]["label"], sorted(summary["comparisons"])), ("PRIMARY", ["IA_IB", "IA_IC"]))
            self.assertTrue(summary["IC"]["valid"])
            files = sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file())
            self.assertEqual(files, sorted(p.relative_to(second) for p in second.rglob("*") if p.is_file()))
            for name in files:
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), str(name))


# ================================================================= isolation
class ICIsolationTest(unittest.TestCase):
    ALLOWED = {"app.py", "registry.py", "ic.py"}

    def test_only_registry_app_and_plugin_name_ic(self):
        importers = []
        for path in sorted(Path(witness.__file__).parent.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names_ic = any(isinstance(n, ast.Constant) and n.value == "IC" for n in ast.walk(tree))
            imports_ic = any(
                (isinstance(n, ast.ImportFrom) and ((n.module or "").split(".")[-1] == "ic"
                                                    or any(a.name == "ic" for a in n.names)))
                or (isinstance(n, ast.Import) and any(a.name.split(".")[-1] == "ic" for a in n.names))
                for n in ast.walk(tree)
            )
            if imports_ic:
                importers.append(path.name)
            if path.name not in self.ALLOWED:
                self.assertFalse(names_ic or imports_ic, f"{path.name} names or imports IC")
        self.assertEqual(importers, ["registry.py"])


if __name__ == "__main__":
    unittest.main()
