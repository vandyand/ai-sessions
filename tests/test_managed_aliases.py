import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell required")
class AliasTests(unittest.TestCase):
    def test_only_alias_launchers_change_and_native_arguments_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "profile.ps1"
            profile.write_text(
                """# preserve this comment
function global:codex { 'raw native launcher' }
function global:cdba {
    codex --dangerously-bypass-approvals-and-sandbox @args
}
function global:cdsp {
    claude --dangerously-skip-permissions @args
}
""",
                encoding="utf-8",
            )
            installer = Path(__file__).resolve().parents[1] / "tools/Set-ManagedSessionAliases.ps1"
            for _ in range(2):
                result = subprocess.run(
                    ["pwsh", "-NoProfile", "-File", str(installer), "-ProfilePath", str(profile)],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            text = profile.read_text(encoding="utf-8")
            self.assertIn("function global:codex { 'raw native launcher' }", text)
            self.assertIn("# preserve this comment", text)
            driver = Path(temp) / "test.ps1"
            driver.write_text(
                'param($ProfilePath)\n. $ProfilePath\nfunction global:sessions { ConvertTo-Json -InputObject @($args) -Compress }\ncdba --model example "a prompt with spaces"\ncdsp --model example "another prompt"\n',
                encoding="utf-8",
            )
            result = subprocess.run(
                ["pwsh", "-NoProfile", "-File", str(driver), "-ProfilePath", str(profile)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(
                rows[0],
                [
                    "new",
                    "codex",
                    "--",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--model",
                    "example",
                    "a prompt with spaces",
                ],
            )
            self.assertEqual(
                rows[1],
                [
                    "new",
                    "claude",
                    "--",
                    "--dangerously-skip-permissions",
                    "--model",
                    "example",
                    "another prompt",
                ],
            )
