"""The supervisor hook runs on every prompt of every hooked session, so it stays light."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src"
HEAVY = ("psutil", "zstandard", "ai_sessions.app", "ai_sessions.registry", "ai_sessions.harnesses")


class HookImportTests(unittest.TestCase):
    def test_the_hook_loads_no_harness_and_no_process_library(self) -> None:
        probe = (
            "import sys, ai_sessions.supervisor.hook\n"
            f"print(','.join(name for name in {HEAVY!r} if name in sys.modules))\n"
        )
        environment = dict(os.environ, PYTHONPATH=str(SOURCE))
        loaded = subprocess.run(
            [sys.executable, "-c", probe],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(loaded, "")

    def test_the_registry_still_has_every_harness_when_asked(self) -> None:
        probe = "from ai_sessions.registry import REGISTRY\nprint(','.join(REGISTRY.names()))\n"
        environment = dict(os.environ, PYTHONPATH=str(SOURCE))
        names = subprocess.run(
            [sys.executable, "-c", probe],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(set(names.split(",")), {"claude", "codex", "opencode"})


if __name__ == "__main__":
    unittest.main()
