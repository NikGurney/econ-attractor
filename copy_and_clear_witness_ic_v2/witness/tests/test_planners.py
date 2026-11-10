"""Planner fixtures (spec Sections 14.1-14.2)."""

import unittest

from witness.grids import reference_dijkstra, verify_path
from witness.planners import PLANNERS

from .helpers import pinput, primary_scenario

OPEN_3X3 = ["...", "...", "..."]


class PlannerMicrofixtureTest(unittest.TestCase):
    """Open 3x3 unit grid, start (0,0), goal (0,2): exact paths and expansion counts."""

    EXPECTED = {"greedy": 3, "bfs": 4, "weighted": 3, "beam": 4}

    def test_top_row_path_and_expansions(self):
        p = pinput(OPEN_3X3, (0, 0), (0, 2))
        for module, expansions in self.EXPECTED.items():
            with self.subTest(module=module):
                result = PLANNERS[module](p)
                self.assertEqual(result.status, "found")
                self.assertEqual(result.path, ((0, 0), (0, 1), (0, 2)))
                self.assertEqual(result.expansions, expansions)


class PlannerFixtureTest(unittest.TestCase):
    def test_forced_detour_single_route(self):
        rows = [".#.", ".#.", "..."]
        p = pinput(rows, (0, 0), (0, 2))
        only = ((0, 0), (1, 0), (2, 0), (2, 1), (2, 2), (1, 2), (0, 2))
        self.assertEqual(reference_dijkstra(p).path, only)
        for module, planner in PLANNERS.items():
            with self.subTest(module=module):
                result = planner(p)
                self.assertEqual(result.status, "found")
                self.assertEqual(result.path, only)
                self.assertTrue(verify_path(p, result.path).valid)

    def test_weighted_fork(self):
        # 2x4: direct bottom route enters costs 4,4,1 (=9, 3 moves); upper route five entered costs of 1.
        p = pinput(["....", ".441"], (1, 0), (1, 3))
        ref = reference_dijkstra(p)
        self.assertEqual(ref.path, ((1, 0), (0, 0), (0, 1), (0, 2), (0, 3), (1, 3)))
        self.assertEqual((ref.hops, ref.cost), (5, 5))
        bfs = PLANNERS["bfs"](p)
        self.assertEqual(bfs.path, ((1, 0), (1, 1), (1, 2), (1, 3)))
        self.assertEqual(verify_path(p, bfs.path).cost, 9)

    def test_impossible_grid_explicit_failure(self):
        p = pinput(["...", "###", "..."], (0, 0), (2, 2))
        ref = reference_dijkstra(p)
        self.assertIsNone(ref.path)
        for module, planner in PLANNERS.items():
            with self.subTest(module=module):
                result = planner(p)
                self.assertEqual(result.status, "failed")
                self.assertIsNone(result.path)
                self.assertGreaterEqual(result.expansions, 1)

    def test_deterministic_repeated_output(self):
        tasks = primary_scenario().tasks[:12]
        for module, planner in PLANNERS.items():
            first = [planner(t.planner_input()) for t in tasks]
            second = [planner(t.planner_input()) for t in reversed(tasks)][::-1]
            self.assertEqual(first, second, module)

    def test_planner_paths_verify_on_primary_tasks(self):
        for task in primary_scenario().tasks:
            for module, planner in PLANNERS.items():
                result = planner(task.planner_input())
                if result.status == "found":
                    check = verify_path(task.planner_input(), result.path)
                    self.assertTrue(check.valid, (task.task_id, module, check.reason))
                    self.assertGreaterEqual(check.cost, task.reference_cost)

    def test_beam_can_fail_where_route_exists(self):
        # Bounded beam search is incomplete: pruned candidates are marked seen, so on this
        # unit-cost grid width-4 beam exhausts its frontier although a route exists.
        # This is a legitimate task failure, not an invalid path.
        p = pinput(["....#", ".....", ".##..", "...#.", "....#"], (1, 2), (3, 2))
        self.assertIsNotNone(reference_dijkstra(p).path)
        for module in ("greedy", "bfs", "weighted"):
            self.assertEqual(PLANNERS[module](p).status, "found", module)
        result = PLANNERS["beam"](p)
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.path)


if __name__ == "__main__":
    unittest.main()
