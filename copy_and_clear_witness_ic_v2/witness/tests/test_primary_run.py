"""End-to-end primary witness (spec Sections 13 and 14.6)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from witness import app
from witness.comparisons import technical_projection

from .helpers import primary_run, quiet

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_FILES = ("scenario.json", "institution_specs.json", "IA_events.jsonl", "IB_events.jsonl", "summary.json")


def read_tree(directory: Path) -> dict:
    return {str(p.relative_to(directory)): p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file()}


class PrimaryWitnessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls._tmp.name) / "outputs"
        cls.exit_code = quiet(app.main, ["--output-dir", str(cls.out), "--skip-replay-check"])
        cls.summary = json.loads((cls.out / "summary.json").read_text(encoding="utf-8"))
        cls.ia, cls.ib = cls.summary["IA"], cls.summary["IB"]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_exit_code_and_artifacts(self):
        self.assertEqual(self.exit_code, 0)
        for name in REQUIRED_FILES:
            self.assertTrue((self.out / name).is_file(), name)
        self.assertEqual(self.summary["run"]["label"], "PRIMARY")
        self.assertTrue(self.summary["run"]["valid"])

    def test_structural_acceptance_criteria(self):
        ia, ib = self.ia, self.ib
        self.assertEqual(ia["principal_count"], 40)
        self.assertEqual(ib["principal_count"], 24)
        for s in (ia, ib):
            self.assertEqual(s["capacity_count"], 32)
            self.assertEqual((s["baseline_capacity_count"], s["copied_capacity_count"]), (16, 16))
            self.assertEqual(s["tasks_posted"], 64)
            self.assertEqual(s["distinct_capacities_used"], 32)
            self.assertEqual(s["max_simultaneous_slot_occupancy"], 4)
            self.assertEqual(s["tasks_terminal"], 64)
            self.assertEqual(s["tasks_attempted"], 64)
            self.assertEqual(s["final_reserved_credits"], 0)
            self.assertEqual(s["final_occupied_slots"], 0)
            self.assertEqual((s["initial_total_credits"], s["final_total_credits"]), (64, 64))
            self.assertEqual(s["invariant_violation_count"], 0)
            self.assertGreaterEqual(s["copied_capacity_successes"], 1)
            self.assertEqual(s["run_status"], "completed")
            self.assertTrue(s["valid"])
            self.assertTrue(all(r["passed"] for r in s["validation"]["trace_requirements"]))
            self.assertEqual(s["validation"]["trace_audit"]["violations"], [])
        self.assertEqual(ib["temporary_executors_active_at_end"], 0)
        self.assertTrue(all(a["passed"] for a in self.summary["run"]["acceptance_criteria"]))
        self.assertTrue(all(self.summary["run"]["validity_statement"].values()))

    def test_paired_trace_contrast(self):
        profile = self.summary["comparisons"]["IA_IB"]
        self.assertTrue(profile["passed"])
        pt = profile["paired_trace"]
        a, b = pt["members"]["IA"], pt["members"]["IB"]
        self.assertEqual(a["capacity_origin"], "copy")
        self.assertEqual((a["task_id"], a["capacity_id"]), (b["task_id"], b["capacity_id"]))
        for key in ("path", "expansions", "verification_outcome", "completion_theta", "slot_id"):
            self.assertEqual(a[key], b[key], key)
        # IA: the independent copied principal is executor, owner and payee.
        self.assertTrue(a["payee_id"].startswith("CP"))
        self.assertEqual(a["executor_id"], a["payee_id"])
        self.assertIn(a["payee_id"], self.ia["copied_principals_created"])
        self.assertNotEqual(a["payee_id"], a["source_provider_id"])
        # IB: the founding source owner is paid; a temporary executor ran and terminated.
        self.assertEqual(b["payee_id"], b["source_provider_id"])
        self.assertEqual(b["executor_kind"], "temporary")
        self.assertNotIn(b["executor_id"], self.ib["final_account_balances"])
        self.assertIn("executor_terminated", b["event_types"])
        self.assertEqual(a["account_delta"][a["requester_id"]], -1)
        self.assertEqual(b["account_delta"], {a["requester_id"]: -1, b["payee_id"]: 1})

    def test_technical_projection_identical(self):
        run = primary_run()
        self.assertEqual(technical_projection(run.results["IA"]), technical_projection(run.results["IB"]))
        self.assertNotEqual(run.results["IA"].events, run.results["IB"].events)

    def test_event_file_format(self):
        raw = (self.out / "IB_events.jsonl").read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r", raw)
        lines = raw.decode("utf-8").split("\n")[:-1]
        fields = {"schema_version", "seq", "theta", "institution", "event_type", "task_id", "capacity_id",
                  "principal_id", "executor_id", "slot_id", "amount", "details"}
        for i, line in enumerate(lines):
            event = json.loads(line)
            self.assertEqual(set(event), fields)
            self.assertEqual(event["seq"], i)
            self.assertEqual(line, json.dumps(event, sort_keys=True, separators=(",", ":")))

    def test_replay_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            second = Path(tmp) / "outputs"
            self.assertEqual(quiet(app.main, ["--output-dir", str(second), "--skip-replay-check"]), 0)
            first_tree, second_tree = read_tree(self.out), read_tree(second)
            self.assertEqual(sorted(first_tree), sorted(second_tree))
            for name in first_tree:
                self.assertEqual(first_tree[name], second_tree[name], name)


class CommandLineTest(unittest.TestCase):
    def test_primary_command_in_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, str(ROOT / "run_witness.py")],
                cwd=tmp, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT)},
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("WITNESS VALID", proc.stdout)
            self.assertIn("replay: byte-identical", proc.stdout)
            self.assertTrue((Path(tmp) / "outputs" / "summary.json").exists())

    def test_nonprimary_runs_are_labeled_and_never_overwrite_primary(self):
        with tempfile.TemporaryDirectory() as tmp:
            primary_dir = Path(tmp) / "outputs"
            self.assertEqual(quiet(app.main, ["--output-dir", str(primary_dir), "--skip-replay-check"]), 0)
            before = read_tree(primary_dir)
            refused = quiet(app.main, ["--institutions", "IB", "--output-dir", str(primary_dir),
                                       "--skip-replay-check"])
            self.assertEqual(refused, 2)
            self.assertEqual(read_tree(primary_dir), before)

            other = Path(tmp) / "ib_only"
            code = quiet(app.main, ["--institutions", "IB", "--output-dir", str(other), "--skip-replay-check"])
            self.assertEqual(code, 0)
            summary = json.loads((other / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["run"]["label"], "NONPRIMARY")
            self.assertFalse(summary["run"]["primary"])
            self.assertEqual(summary["comparisons"], {})
            self.assertFalse((other / "IA_events.jsonl").exists())

    def test_unknown_institution_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(quiet(app.main, ["--institutions", "IA", "IZ", "--output-dir", tmp]), 2)


if __name__ == "__main__":
    unittest.main()
