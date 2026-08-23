import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ai_sessions.paths import env_path

ROOT = Path(__file__).resolve().parents[1]


def provider_homes(environment: dict[str, str]) -> dict[str, str]:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from ai_sessions.registry import REGISTRY; "
                "print(json.dumps({name: str(REGISTRY.get(name).home) "
                "for name in ('claude', 'codex')}))"
            ),
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


class EnvironmentPathTests(unittest.TestCase):
    def test_unset_and_empty_overrides_use_the_fallback(self) -> None:
        fallback = Path("fallback")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(env_path("HARNESS_HOME", fallback), fallback)
        with patch.dict(os.environ, {"HARNESS_HOME": ""}, clear=True):
            self.assertEqual(env_path("HARNESS_HOME", fallback), fallback)

    def test_nonempty_override_is_expanded(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"HARNESS_HOME": directory}, clear=True):
                self.assertEqual(env_path("HARNESS_HOME", Path("fallback")), Path(directory))
        with patch.dict(os.environ, {"HARNESS_HOME": "~/provider"}):
            self.assertEqual(env_path("HARNESS_HOME", Path("fallback")), Path.home() / "provider")

    def test_fallback_is_expanded(self) -> None:
        with patch.dict(os.environ, {"HARNESS_HOME": ""}):
            self.assertEqual(env_path("HARNESS_HOME", Path("~/fallback")), Path.home() / "fallback")

    def test_registered_provider_homes_use_exact_overrides(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(ROOT / "src")
            environment["CLAUDE_CONFIG_DIR"] = str(root / "claude-override")
            environment["CODEX_HOME"] = str(root / "codex-override")
            self.assertEqual(
                provider_homes(environment),
                {
                    "claude": str(root / "claude-override"),
                    "codex": str(root / "codex-override"),
                },
            )

    def test_registered_provider_homes_use_exact_defaults(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        environment.pop("CLAUDE_CONFIG_DIR", None)
        environment.pop("CODEX_HOME", None)
        self.assertEqual(
            provider_homes(environment),
            {
                "claude": str(Path.home() / ".claude"),
                "codex": str(Path.home() / ".codex"),
            },
        )


if __name__ == "__main__":
    unittest.main()
