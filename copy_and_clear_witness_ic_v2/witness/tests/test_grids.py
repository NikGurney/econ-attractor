"""Grid primitives, hashing, reference solver and mechanical verifier (spec Sections 7-8.3)."""

import unittest

from witness.config import MASTER_SEED, derive_stream_seed, make_rng
from witness.grids import canonical_grid_hash, entered_cost, verify_path

from .helpers import grid, pinput

OPEN_3X3 = ["...", "...", "..."]


class VerifierTest(unittest.TestCase):
    def setUp(self):
        self.p = pinput(["...", ".#.", "..."], (0, 0), (2, 2))

    def assertRejected(self, path, reason):
        check = verify_path(self.p, path)
        self.assertFalse(check.valid)
        self.assertEqual(check.reason, reason)

    def test_accepts_valid_path(self):
        check = verify_path(self.p, [(0, 0), (0, 1), (0, 2), (1, 2), (2, 2)])
        self.assertTrue(check.valid)
        self.assertEqual((check.hops, check.cost), (4, 4))

    def test_rejects_wall_crossing(self):
        self.assertRejected([(0, 0), (0, 1), (1, 1), (2, 1), (2, 2)], "enters_blocked_cell")

    def test_rejects_leaving_grid(self):
        self.assertRejected([(0, 0), (-1, 0), (0, 0), (0, 1), (0, 2), (1, 2), (2, 2)], "out_of_bounds")
        self.assertRejected([(0, 0), (0, 1), (0, 2), (0, 3), (1, 2), (2, 2)], "out_of_bounds")

    def test_rejects_jumps_and_diagonals(self):
        self.assertRejected([(0, 0), (0, 2), (1, 2), (2, 2)], "non_cardinal_step")
        self.assertRejected([(0, 0), (1, 0), (2, 1), (2, 2)], "non_cardinal_step")
        self.assertRejected([(0, 0), (0, 0), (0, 1), (0, 2), (1, 2), (2, 2)], "non_cardinal_step")

    def test_rejects_wrong_endpoints_and_missing_goal(self):
        self.assertRejected([(0, 1), (0, 2), (1, 2), (2, 2)], "wrong_start")
        self.assertRejected([(0, 0), (0, 1), (0, 2), (1, 2)], "does_not_end_at_goal")
        self.assertRejected([(0, 0)], "does_not_end_at_goal")

    def test_rejects_malformed(self):
        self.assertRejected([], "empty_or_malformed_path")
        self.assertRejected([(0, 0, 0)], "malformed_cell")
        self.assertRejected([(True, 0)], "malformed_cell")

    def test_null_path_is_not_verified(self):
        with self.assertRaises(ValueError):
            verify_path(self.p, None)


class CostAndHashTest(unittest.TestCase):
    def test_entered_cell_cost(self):
        _, cost = grid(["1242", "4441"])
        # Start cost is excluded; every entered cell including the goal is included.
        self.assertEqual(entered_cost(cost, [(0, 0), (0, 1), (0, 2), (0, 3)]), 2 + 4 + 2)
        self.assertEqual(entered_cost(cost, [(0, 3), (1, 3)]), 1)
        self.assertEqual(entered_cost(cost, [(0, 0)]), 0)

    def test_hash_covers_only_environment(self):
        blocked, cost = grid(OPEN_3X3)
        base = canonical_grid_hash(3, (0, 0), (2, 2), blocked, cost)
        self.assertEqual(base, canonical_grid_hash(3, (0, 0), (2, 2), blocked, cost))
        self.assertNotEqual(base, canonical_grid_hash(3, (0, 0), (2, 1), blocked, cost))
        _, cost2 = grid(["..2", "...", "..."])
        self.assertNotEqual(base, canonical_grid_hash(3, (0, 0), (2, 2), blocked, cost2))
        self.assertEqual(len(base), 64)


class StreamSeedTest(unittest.TestCase):
    def test_named_stream_derivation(self):
        import hashlib

        for label in ("modules", "copies", "tasks"):
            digest = hashlib.sha256(f"{MASTER_SEED}:{label}".encode("ascii")).digest()
            self.assertEqual(derive_stream_seed(label), int.from_bytes(digest[:8], "big"))
        self.assertEqual(make_rng("tasks").random(), make_rng("tasks").random())
        self.assertNotEqual(derive_stream_seed("modules"), derive_stream_seed("copies"))


if __name__ == "__main__":
    unittest.main()
