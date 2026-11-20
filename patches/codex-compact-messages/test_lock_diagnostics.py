"""Fixture-only checks; no Cargo, dependency downloads, or native messages."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "lock_probe", Path(__file__).with_name("capture_lock_diagnostics.py")
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

BEFORE = b'version = 4\n\n[[package]]\nname = "fixture"\nversion = "1.0.0"\n'
AFTER = BEFORE.replace(b"1.0.0", b"1.0.1")


class LockProbeTests(unittest.TestCase):
    def fixture(self, base):
        source = Path(base) / "source"
        lock = source / "codex-rs" / "Cargo.lock"
        lock.parent.mkdir(parents=True)
        lock.write_bytes(BEFORE)
        return source, lock, Path(base) / "output"

    def test_single_resolution_records_diff_and_restores_even_on_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as base:
            source, lock, output = self.fixture(base)
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                lock.write_bytes(AFTER)
                return SimpleNamespace(returncode=101)

            summary = probe.capture(source, output, runner=run)
            self.assertEqual(calls, [["cargo", "metadata", "--offline", "--format-version", "1"]])
            self.assertEqual(lock.read_bytes(), BEFORE)
            self.assertEqual((output / "Cargo.lock.proposed").read_bytes(), AFTER)
            self.assertIn(
                '+version = "1.0.1"', (output / "Cargo.lock.diff").read_text(encoding="utf-8")
            )
            self.assertTrue(summary["changed"] and summary["restored_original"])
            changes = json.loads((output / "package-changes.json").read_text(encoding="utf-8"))
            self.assertEqual(changes["added"], ["fixture|1.0.1|"])

    def test_timeout_after_partial_write_still_restores_original(self):
        with tempfile.TemporaryDirectory() as base:
            source, lock, output = self.fixture(base)

            def run(command, **kwargs):
                lock.write_bytes(AFTER)
                raise subprocess.TimeoutExpired(command, 600)

            result = probe.capture(source, output, runner=run)
            self.assertEqual(lock.read_bytes(), BEFORE)
            self.assertEqual(result["probe_error"], "TimeoutExpired")
            self.assertTrue((output / "summary.json").exists())


if __name__ == "__main__":
    unittest.main()
