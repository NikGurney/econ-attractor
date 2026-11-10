"""Descriptors, ledger, institutional semantics and kernel enforcement (spec Section 14.4)."""

import dataclasses
import json
import unittest
from collections import Counter, defaultdict

from witness.app import execute_witness
from witness.config import ScenarioConfig
from witness.institutions.base import fifo_round_robin_match
from witness.institutions.registry import InstitutionRegistry, RegistryError, default_registry
from witness.kernel import Execution, KernelConfigurationError, run_institution
from witness.ledger import Ledger, LedgerError
from witness.planners import PLANNERS, PlanResult
from witness.records import (
    DESCRIPTOR_FIELDS,
    KERNEL_EVENT_TYPES,
    EventDraft,
    InstitutionDescriptor,
    LedgerOp,
    MatchDecision,
    SettlementInstruction,
)
from witness.scenario import assemble_scenario
from witness.validation import validate_descriptor, validate_run

from .helpers import make_task, primary_run, primary_scenario

REGISTRY = default_registry()


class Delegating:
    """Test wrapper: forwards every hook to a real plugin unless overridden."""

    def __init__(self, inner):
        self.inner = inner
        self.institution_id = inner.institution_id

    def __getattr__(self, name):
        return getattr(self.inner, name)


# ================================================================ descriptors
class DescriptorTest(unittest.TestCase):
    def test_primary_descriptors_complete_and_serializable(self):
        for iid in ("IA", "IB"):
            with self.subTest(iid=iid):
                desc = REGISTRY.create(iid).descriptor()
                self.assertEqual(desc.institution_id, iid)
                data = desc.to_dict()
                self.assertEqual(list(data), list(DESCRIPTOR_FIELDS))
                self.assertEqual(json.loads(json.dumps(data)), data)
                self.assertEqual(validate_descriptor(desc), [])
                self.assertEqual(desc.external_interfaces["interfaces"], ())

    def test_descriptors_are_immutable(self):
        desc = REGISTRY.create("IA").descriptor()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            desc.label = "changed"
        with self.assertRaises(TypeError):
            desc.allocation["task_rule"] = "changed"

    def test_declared_event_vocabularies(self):
        expected = {"IA": {"offer_registered", "provider_bound"},
                    "IB": {"license_registered", "executor_instantiated", "executor_terminated"}}
        for iid, required in expected.items():
            desc = REGISTRY.create(iid).descriptor()
            self.assertTrue(required <= set(desc.event_vocabulary))
            self.assertEqual(set(desc.information["required_events"]), required)
            self.assertFalse(set(desc.event_vocabulary) & set(KERNEL_EVENT_TYPES))

    def test_primary_trace_requirements_present(self):
        ids = {iid: {r["id"] for r in REGISTRY.create(iid).descriptor().trace_requirements} for iid in ("IA", "IB")}
        self.assertTrue({"IA.copied_principals_created", "IA.copied_capacities_owned_by_copied_principals",
                         "IA.copied_provider_bound_and_paid", "IA.copied_principal_paid"} <= ids["IA"])
        self.assertTrue({"IB.licenses_registered", "IB.copied_executor_lifecycle",
                         "IB.termination_retains_capacity"} <= ids["IB"])
        for iid in ("IA", "IB"):
            self.assertTrue({"common.tasks_posted", "common.tasks_prefunded", "common.tasks_matched",
                             "common.tasks_verified", "common.tasks_settled"} <= ids[iid])

    def test_incomplete_descriptor_rejected(self):
        data = REGISTRY.create("IB").descriptor().to_dict()
        for coordinate in ("relations", "temporal", "external_interfaces"):
            broken = dict(data, **{coordinate: {}})
            self.assertTrue(validate_descriptor(InstitutionDescriptor(**broken)), coordinate)
        bad_id = dict(data, institution_id="ib")
        self.assertTrue(validate_descriptor(InstitutionDescriptor(**bad_id)))
        kernel_type = json.loads(json.dumps(data))
        kernel_type["information"]["event_vocabulary"].append("credit_transferred")
        self.assertTrue(validate_descriptor(InstitutionDescriptor(**kernel_type)))

        class Broken(Delegating):
            def descriptor(self):
                return InstitutionDescriptor(**dict(data, trace_requirements=[]))

        with self.assertRaises(KernelConfigurationError):
            Execution(primary_scenario(), Broken(REGISTRY.create("IB")))

    def test_registry_ids_unique_and_well_formed(self):
        registry = InstitutionRegistry()
        registry.register("IX", lambda: None)
        with self.assertRaises(RegistryError):
            registry.register("IX", lambda: None)
        with self.assertRaises(RegistryError):
            registry.register("ix", lambda: None)
        with self.assertRaises(RegistryError):
            REGISTRY.create("NOPE")
        self.assertEqual(REGISTRY.ids(), ("IA", "IB", "IC"))

    def test_duplicate_requested_ids_rejected(self):
        from witness.app import WitnessConfigurationError

        with self.assertRaises(WitnessConfigurationError):
            execute_witness(institution_ids=("IA", "IA"))


# ===================================================================== ledger
class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()
        self.ledger.create_account("R", 2)
        self.ledger.create_account("P", 0)
        self.ledger.freeze_supply()
        self.ledger.reserve("T1", "R", 1)
        self.ledger.reserve("T2", "R", 1)

    def test_posting_requires_available_credit(self):
        with self.assertRaises(LedgerError):
            self.ledger.reserve("T3", "R", 1)
        with self.assertRaises(LedgerError):
            self.ledger.create_account("late", 0)

    def test_success_transfer(self):
        self.ledger.apply_ops((LedgerOp("transfer_reserved", "T1", 1, "P"),))
        self.assertEqual(self.ledger.snapshot(), {"P": {"balance": 1, "reserved": 0},
                                                  "R": {"balance": 1, "reserved": 1}})
        self.assertEqual(self.ledger.check(), [])

    def test_failure_release(self):
        self.ledger.apply_ops((LedgerOp("release_reservation", "T2", 1),))
        self.assertEqual(self.ledger.snapshot()["R"], {"balance": 2, "reserved": 1})
        self.assertEqual(self.ledger.total(), 2)
        self.assertEqual(self.ledger.check(), [])

    def test_single_settlement_per_task(self):
        self.ledger.apply_ops((LedgerOp("transfer_reserved", "T1", 1, "P"),))
        for op in (LedgerOp("transfer_reserved", "T1", 1, "P"), LedgerOp("release_reservation", "T1", 1)):
            with self.assertRaises(LedgerError):
                self.ledger.apply_ops((op,))

    def test_invalid_ops_rejected_atomically(self):
        before = self.ledger.snapshot()
        bad_batches = [
            (LedgerOp("transfer_reserved", "T1", 1, "P"), LedgerOp("transfer_reserved", "T2", 2, "P")),
            (LedgerOp("transfer_reserved", "T1", 1, "nobody"),),
            (LedgerOp("transfer_reserved", "T1", 1, "R"),),
            (LedgerOp("mint", "T1", 1, "P"),),
            (LedgerOp("transfer_reserved", "T9", 1, "P"),),
        ]
        for batch in bad_batches:
            with self.assertRaises(LedgerError):
                self.ledger.apply_ops(batch)
            self.assertEqual(self.ledger.snapshot(), before)

    def test_conservation_and_nonnegativity_detected(self):
        self.ledger.accounts["P"].balance = 5
        codes = {v.code for v in self.ledger.check()}
        self.assertIn("credit_conservation", codes)
        self.ledger.accounts["P"].balance = 0
        self.ledger.accounts["R"].reserved = 3
        codes = {v.code for v in self.ledger.check()}
        self.assertTrue({"negative_balance", "reservation_exceeds_balance"} <= codes)


# ====================================================== primary institutions
class PrimaryInstitutionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run = primary_run()
        cls.ia, cls.ib = run.results["IA"], run.results["IB"]
        cls.scenario = run.scenario

    def events(self, result, event_type):
        return [e for e in result.events if e["event_type"] == event_type]

    def test_round_robin_uses_every_capacity_in_first_32_assignments(self):
        for result in (self.ia, self.ib):
            first = [e["capacity_id"] for e in self.events(result, "task_matched")[:32]]
            self.assertEqual(first, [f"C{i:03d}" for i in range(32)])
            self.assertEqual(result.summary["distinct_capacities_used"], 32)
            self.assertEqual(result.max_slot_occupancy, 4)

    def test_fifo_round_robin_helper_skips_busy_and_ineligible(self):
        class View:
            def open_task_ids(self):
                return ("T005", "T009")

            def capacity_ids(self):
                return ("C000", "C001", "C002", "C003")

            def capacity_state(self, cid):
                return "busy" if cid == "C002" else "available"

        decision, cursor = fifo_round_robin_match(View(), 2, lambda cid, tid: cid != "C003")
        self.assertEqual((decision, cursor), (MatchDecision("T005", "C000"), 1))

    def test_success_settlement_and_conservation(self):
        for result in (self.ia, self.ib):
            s = result.summary
            self.assertEqual((s["initial_total_credits"], s["final_total_credits"], s["final_reserved_credits"]),
                             (64, 64, 0))
            for pid, acct in result.accounts.items():
                self.assertGreaterEqual(acct["balance"] - acct["reserved"], 0)
            transfers = self.events(result, "credit_transferred")
            self.assertEqual(len(transfers) + len(self.events(result, "credit_released")), 64)
            for e in transfers:
                self.assertEqual(e["principal_id"], result.attempts[[a.task_id for a in result.attempts]
                                                                    .index(e["task_id"])].prospective_payee_id)

    def test_ia_copied_principals_created_and_paid(self):
        s = self.ia.summary
        cps = [f"CP{k:03d}" for k in range(16)]
        self.assertEqual(s["copied_principals_created"], cps)
        self.assertEqual(s["principal_count"], 40)
        created = {e["principal_id"]: e for e in self.events(self.ia, "principal_created")}
        paid = Counter()
        for k, cp in enumerate(cps):
            cid = f"C{16 + k:03d}"
            self.assertEqual(created[cp]["amount"], 0)
            self.assertEqual(created[cp]["details"]["assigned_capacity_id"], cid)
            self.assertEqual(s["institutional_capacity_owners"][cid], cp)
        for a in self.ia.attempts:
            if a.capacity_origin == "copy":
                owner = f"CP{int(a.capacity_id[1:]) - 16:03d}"
                self.assertEqual((a.executor_id, a.capacity_owner_id, a.payee_id), (owner, owner, owner))
                self.assertNotEqual(a.payee_id, a.source_provider_id)
                paid[owner] += 1
        for cp in cps:
            self.assertEqual(self.ia.accounts[cp]["balance"], paid[cp])

    def test_ib_source_owner_licensing_and_payment(self):
        s = self.ib.summary
        self.assertEqual(s["principal_count"], 24)
        self.assertEqual(s["copied_principals_created"], [])
        licenses = self.events(self.ib, "license_registered")
        self.assertEqual(len(licenses), 32)
        for lic in licenses:
            cap = self.scenario.capacity(lic["capacity_id"])
            self.assertEqual(lic["principal_id"], cap.source_provider_id)
        received = Counter()
        for a in self.ib.attempts:
            self.assertEqual(a.capacity_owner_id, a.source_provider_id)
            self.assertEqual(a.payee_id, a.source_provider_id)
            received[a.payee_id] += 1
        for pid, n in received.items():
            self.assertEqual(self.ib.accounts[pid]["balance"], n)
        # A founder whose competence was copied receives the copies' payments in IB but not in IA.
        p013_ib = self.ib.accounts["P013"]["balance"]
        p013_ia = self.ia.accounts["P013"]["balance"]
        self.assertGreater(p013_ib, p013_ia)

    def test_ib_executor_lifecycle(self):
        s = self.ib.summary
        self.assertEqual((s["temporary_executors_instantiated"], s["temporary_executors_terminated"],
                          s["temporary_executors_active_at_end"]), (64, 64, 0))
        by_executor = defaultdict(list)
        for e in self.ib.events:
            if e["executor_id"]:
                by_executor[e["executor_id"]].append(e)
        self.assertEqual(len(by_executor), 64)
        for eid, evs in by_executor.items():
            self.assertNotIn(eid, self.ib.accounts)
            types = [e["event_type"] for e in evs]
            self.assertEqual(types[0], "executor_instantiated")
            self.assertLess(types.index("executor_terminated"), types.index("capacity_released"))
            self.assertEqual(types[-1], "capacity_released")
            self.assertNotIn(eid, [e["principal_id"] for e in self.ib.events])
        # Termination releases but never deletes: each capacity is used again after its first termination.
        uses = Counter(a.capacity_id for a in self.ib.attempts)
        self.assertTrue(all(n >= 2 for n in uses.values()))
        self.assertEqual(set(self.ib.capacity_states.values()), {"available"})

    def test_primary_runs_have_no_failures(self):
        # Documented property of this seed: every module finds a route on every primary grid,
        # so the failure-release path is exercised by the dedicated fixture below instead.
        for result in (self.ia, self.ib):
            self.assertEqual(result.summary["tasks_failed"], 0)


# ============================================================ kernel control
def batching_scenario():
    config = ScenarioConfig(master_seed=1, requester_count=1, founding_provider_count=4, copy_count=1,
                            tasks_per_requester=3, slot_count=2, requester_initial_balance=3, grid_size=5,
                            min_reference_hops=1, max_reference_hops=20)
    corridor = lambda r: ["....." if i == r else "#####" for i in range(5)]
    tasks = tuple(make_task(f"T{i:03d}", "R000", corridor(i), (i, 0), (i, 4)) for i in range(3))
    return assemble_scenario(config, ("greedy", "bfs", "weighted", "beam"), ("P000",), tasks,
                             {"modules": 0, "copies": 0, "tasks": 0})


class KernelBehaviourTest(unittest.TestCase):
    def test_simultaneous_completion_batching(self):
        scenario = batching_scenario()
        result = run_institution(scenario, REGISTRY.create("IA"))
        self.assertEqual(result.status, "completed")
        first, second = result.attempts[0], result.attempts[1]
        self.assertEqual((first.capacity_id, second.capacity_id), ("C000", "C001"))
        self.assertEqual(first.completion_theta, second.completion_theta, "fixture must tie")
        tie = first.completion_theta
        at_tie = [e for e in result.events if e["theta"] == tie]
        caps = [e["capacity_id"] for e in at_tie if e["capacity_id"] in ("C000", "C001")]
        self.assertEqual(caps, sorted(caps), "batch processed in ascending capacity order")
        released = [i for i, e in enumerate(at_tie) if e["event_type"] == "capacity_released"]
        matched = [i for i, e in enumerate(at_tie) if e["event_type"] == "task_matched"]
        self.assertEqual(len(released), 2)
        self.assertTrue(matched and min(matched) > max(released), "refill only after the whole batch")
        third = result.attempts[2]
        self.assertEqual((third.task_id, third.capacity_id, third.slot_id, third.start_theta),
                         ("T002", "C002", "S0", tie))

    def test_blocked_run_detected(self):
        class NeverMatches(Delegating):
            def choose_match(self, view, st):
                return None

        result = run_institution(primary_scenario(), NeverMatches(REGISTRY.create("IB")))
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.events[-1]["event_type"], "run_blocked")
        self.assertEqual(len(result.events[-1]["details"]["open_task_ids"]), 64)
        self.assertFalse(validate_run(result, primary_scenario())["passed"])

    def test_invalid_non_null_path_is_fatal(self):
        jump = lambda p: PlanResult("found", (p.start, p.goal), 1)
        planners = {m: jump for m in PLANNERS}
        result = run_institution(primary_scenario(), REGISTRY.create("IA"), planners)
        self.assertEqual(result.status, "violated")
        self.assertEqual(result.violations[0].code, "invalid_planner_path")
        self.assertEqual(result.events[-1]["event_type"], "invariant_violation")
        self.assertEqual(result.summary["tasks_succeeded"] + result.summary["tasks_failed"], 0)
        run = execute_witness(planners=planners)
        self.assertFalse(run.valid)  # the runner maps this to a nonzero exit code

    def test_explicit_failure_releases_reservation(self):
        planners = dict(PLANNERS, beam=lambda p: PlanResult("failed", None, 7))
        run = execute_witness(planners=planners)
        self.assertTrue(run.valid)
        for iid, result in run.results.items():
            failed = [a for a in result.attempts if a.module == "beam"]
            self.assertTrue(failed)
            self.assertEqual(result.summary["tasks_failed"], len(failed))
            released = {e["task_id"] for e in result.events if e["event_type"] == "credit_released"}
            self.assertEqual(released, {a.task_id for a in failed})
            for a in failed:
                self.assertEqual((a.verification_outcome, a.settlement_kind, a.payee_id), ("failed", "released", None))
            per_requester = Counter(a.requester_id for a in failed)
            for rid in (f"R{i:03d}" for i in range(8)):
                self.assertEqual(result.accounts[rid], {"balance": per_requester[rid], "reserved": 0})
            self.assertEqual(result.summary["final_total_credits"], 64)
        self.assertTrue(run.comparisons["IA_IB"]["passed"])

    def assertFatal(self, plugin, code):
        result = run_institution(primary_scenario(), plugin)
        self.assertEqual(result.status, "violated")
        self.assertIn(code, [v.code for v in result.violations])
        self.assertIn("invariant_violation", [e["event_type"] for e in result.events])

    def test_payment_to_unauthorized_payee_is_fatal(self):
        class WrongPayee(Delegating):
            def settlement_instruction(self, view, attempt, verification, st):
                other = "P015" if attempt.prospective_payee_id != "P015" else "P014"
                return SettlementInstruction((LedgerOp("transfer_reserved", attempt.task_id, 1, other),))

        self.assertFatal(WrongPayee(REGISTRY.create("IB")), "unauthorized_payment")

    def test_payment_to_temporary_executor_is_fatal(self):
        class PayExecutor(Delegating):
            def settlement_instruction(self, view, attempt, verification, st):
                return SettlementInstruction(
                    (LedgerOp("transfer_reserved", attempt.task_id, 1, attempt.executor_id),))

        self.assertFatal(PayExecutor(REGISTRY.create("IB")), "unauthorized_payment")

    def test_unregistered_event_type_is_fatal(self):
        class Undeclared(Delegating):
            def finalize_attempt(self, view, attempt, verification, st):
                return [EventDraft("credit_transferred", task_id=attempt.task_id)]

        self.assertFatal(Undeclared(REGISTRY.create("IA")), "unregistered_event_type")

    def test_busy_capacity_match_is_fatal(self):
        class Greedy(Delegating):
            def choose_match(self, view, st):
                return MatchDecision(view.open_task_ids()[0], "C000") if view.open_task_ids() else None

        self.assertFatal(Greedy(REGISTRY.create("IA")), "illegal_match")

    def test_temporary_executor_with_account_is_fatal(self):
        class OwnerAsTemp(Delegating):
            def authorize_attempt(self, view, match, st):
                auth = self.inner.authorize_attempt(view, match, st)
                return dataclasses.replace(auth, executor_id=auth.capacity_owner_id)

        self.assertFatal(OwnerAsTemp(REGISTRY.create("IB")), "temporary_executor_has_account")

    def test_plugin_invariant_violation_is_fatal(self):
        class Unbundled(Delegating):
            def authorize_attempt(self, view, match, st):
                auth = self.inner.authorize_attempt(view, match, st)
                return dataclasses.replace(auth, prospective_payee_id="P015", capacity_owner_id="P015")

        self.assertFatal(Unbundled(REGISTRY.create("IA")), "provider_bundle")


class ValidatorSensitivityTest(unittest.TestCase):
    """The post-run validators must reject traces that do not carry the declared evidence."""

    def test_tampered_payee_detected_by_trace_audit(self):
        scenario = primary_scenario()
        result = run_institution(scenario, REGISTRY.create("IA"))
        self.assertTrue(validate_run(result, scenario)["passed"])
        events = [dict(e, details=dict(e["details"])) for e in result.events]
        victim = next(e for e in events if e["event_type"] == "credit_transferred"
                      and e["details"]["capacity_origin"] == "copy")
        victim["principal_id"] = victim["details"]["source_provider_id"]
        victim["details"]["to_principal_id"] = victim["principal_id"]
        report = validate_run(dataclasses.replace(result, events=events), scenario)
        self.assertFalse(report["passed"])
        codes = {v["code"] for v in report["trace_audit"]["violations"]}
        self.assertIn("unauthorized_payee", codes)

    def test_other_institutions_requirements_fail_on_a_trace(self):
        # IB's declared evidence (licenses, temporary executors) is absent from IA's trace, and vice versa.
        scenario = primary_scenario()
        ia = run_institution(scenario, REGISTRY.create("IA"))
        ib_desc = REGISTRY.create("IB").descriptor()
        from witness.validation import evaluate_trace_requirements

        results = evaluate_trace_requirements(ib_desc, ia.events, ia.accounts.keys(), ia.capacity_states,
                                              scenario.reference_counts())
        failed = {r["id"] for r in results if not r["passed"]}
        self.assertTrue({"IB.licenses_registered", "IB.copied_executor_lifecycle",
                         "IB.no_principals_created"} <= failed)
        self.assertFalse({r["id"] for r in results if r["id"].startswith("common.")} & failed)

    def test_dropped_termination_event_fails_requirements(self):
        scenario = primary_scenario()
        ib = run_institution(scenario, REGISTRY.create("IB"))
        events = [e for e in ib.events if e["event_type"] != "executor_terminated"]
        report = validate_run(dataclasses.replace(ib, events=events), scenario)
        self.assertFalse(report["passed"])
        failed = {r["id"] for r in report["trace_requirements"] if not r["passed"]}
        self.assertIn("IB.copied_executor_lifecycle", failed)


if __name__ == "__main__":
    unittest.main()
