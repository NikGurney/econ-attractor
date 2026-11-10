"""Scenario generation (spec Sections 5-7 and 14.3)."""

import json
import unittest
from collections import Counter
from dataclasses import replace

from witness.config import MODULE_LABELS, PRIMARY_CONFIG
from witness.grids import reference_dijkstra
from witness.institutions.registry import default_registry
from witness.kernel import run_institution
from witness.scenario import generate_scenario, scenario_to_dict
from witness.validation import validate_scenario

from .helpers import primary_scenario

FOUNDER_CHECKPOINT = [
    "beam", "greedy", "weighted", "beam",
    "beam", "bfs", "bfs", "weighted",
    "greedy", "greedy", "greedy", "bfs",
    "bfs", "weighted", "weighted", "greedy",
]
COPY_CHECKPOINT = [
    "P013", "P001", "P004", "P007",
    "P010", "P001", "P012", "P000",
    "P009", "P010", "P000", "P015",
    "P015", "P006", "P011", "P003",
]


class PrimaryCheckpointTest(unittest.TestCase):
    def test_founding_module_assignment(self):
        s = primary_scenario()
        self.assertEqual([m for _, m in s.module_assignment], FOUNDER_CHECKPOINT)
        self.assertEqual([p for p, _ in s.module_assignment], [f"P{i:03d}" for i in range(16)])
        self.assertEqual(Counter(FOUNDER_CHECKPOINT), {"greedy": 5, "bfs": 4, "weighted": 4, "beam": 3})

    def test_copy_source_draws(self):
        s = primary_scenario()
        copies = [c for c in s.capacities if c.origin == "copy"]
        self.assertEqual([c.capacity_id for c in copies], [f"C{16 + k:03d}" for k in range(16)])
        self.assertEqual([c.source_provider_id for c in copies], COPY_CHECKPOINT)
        self.assertEqual(Counter(c.module for c in copies), {"greedy": 7, "bfs": 3, "weighted": 2, "beam": 4})


class ScenarioPropertyTest(unittest.TestCase):
    def setUp(self):
        self.s = primary_scenario()

    def test_counts_and_identifiers(self):
        s = self.s
        self.assertEqual([t.task_id for t in s.tasks], [f"T{i:03d}" for i in range(64)])
        self.assertEqual([c.capacity_id for c in s.capacities], [f"C{i:03d}" for i in range(32)])
        self.assertEqual(s.slot_ids, ("S0", "S1", "S2", "S3"))
        self.assertEqual(len(s.principal_templates), 24)

    def test_all_modules_among_founders(self):
        self.assertEqual({m for _, m in self.s.module_assignment}, set(MODULE_LABELS))

    def test_copy_sources_are_founders_never_copies(self):
        founders = {p for p, _ in self.s.module_assignment}
        copies = [c for c in self.s.capacities if c.origin == "copy"]
        self.assertEqual(len(copies), 16)
        copy_ids = {c.capacity_id for c in copies}
        modules = dict(self.s.module_assignment)
        for c in copies:
            self.assertIn(c.source_provider_id, founders)
            self.assertNotIn(c.source_provider_id, copy_ids)
            self.assertEqual(c.module, modules[c.source_provider_id])
        for c in self.s.capacities[:16]:
            self.assertEqual((c.origin, c.source_provider_id), ("baseline", f"P{c.capacity_id[1:]}"))

    def test_route_threshold_and_reference(self):
        for t in self.s.tasks:
            self.assertTrue(8 <= t.reference_hops <= 32, t.task_id)
            ref = reference_dijkstra(t.planner_input())
            self.assertEqual((ref.path, ref.hops, ref.cost), (t.reference_path, t.reference_hops, t.reference_cost))
            self.assertFalse(t.blocked[t.start[0]][t.start[1]])
            self.assertEqual(t.cost[t.start[0]][t.start[1]], 1)
            for r in range(9):
                for c in range(9):
                    self.assertEqual(t.cost[r][c] is None, t.blocked[r][c])
                    self.assertIn(t.cost[r][c], (None, 1, 2, 4))

    def test_unique_hashes(self):
        hashes = [t.grid_hash for t in self.s.tasks]
        self.assertEqual(len(set(hashes)), 64)

    def test_balanced_requesters(self):
        self.assertEqual(Counter(t.requester_id for t in self.s.tasks), {f"R{i:03d}": 8 for i in range(8)})
        for i, t in enumerate(self.s.tasks):
            self.assertEqual(t.requester_id, f"R{i % 8:03d}")

    def test_common_scenario_invariants(self):
        self.assertEqual(validate_scenario(self.s), [])

    def test_repeated_generation_identical(self):
        again = generate_scenario(PRIMARY_CONFIG)
        self.assertEqual(again, self.s)
        self.assertEqual(scenario_to_dict(again), scenario_to_dict(self.s))
        self.assertEqual(again.scenario_hash, self.s.scenario_hash)

    def test_named_streams_are_independent(self):
        # Changing task generation must not change module assignments or copy sources.
        other = generate_scenario(replace(PRIMARY_CONFIG, max_reference_hops=12))
        self.assertEqual(other.module_assignment, self.s.module_assignment)
        self.assertEqual(other.copy_draws, self.s.copy_draws)
        self.assertNotEqual(other.tasks, self.s.tasks)

    def test_scenario_json_has_no_ownership_or_run_state(self):
        text = json.dumps(scenario_to_dict(self.s))
        for forbidden in ("owner", "license", "offer", "CP0", "payee", "balance_after", "executor"):
            self.assertNotIn(forbidden, text)

    def test_no_scenario_field_depends_on_run_order(self):
        s = generate_scenario(PRIMARY_CONFIG)
        before = json.dumps(scenario_to_dict(s), sort_keys=True)
        registry = default_registry()
        order_ab = {i: run_institution(s, registry.create(i)).events for i in ("IA", "IB")}
        order_ba = {i: run_institution(s, registry.create(i)).events for i in ("IB", "IA")}
        self.assertEqual(json.dumps(scenario_to_dict(s), sort_keys=True), before)
        self.assertEqual(order_ab, order_ba)


if __name__ == "__main__":
    unittest.main()
