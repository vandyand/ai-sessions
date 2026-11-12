import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sessions.workspaces import terminal


class TerminalTests(unittest.TestCase):
    def tabs(self, actions):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            p.write_text(json.dumps({"persistedWindowLayouts": [{"tabLayout": actions}]}))
            before = p.read_bytes()
            result = terminal.saved_tabs(p)
            self.assertEqual(before, p.read_bytes())
            return result

    def test_import_sanitizes_and_groups(self):
        tabs = self.tabs(
            [
                {
                    "action": "newTab",
                    "startingDirectory": "C:/a",
                    "commandline": "NEVER",
                    "tabTitle": "Shell",
                },
                {
                    "action": "splitPane",
                    "startingDirectory": "C:/b",
                    "split": "right",
                    "size": 0.75,
                },
                {"action": "moveFocus", "direction": "previousInOrder"},
                {"action": "splitPane", "startingDirectory": "C:/a", "split": "down", "size": 0.5},
                {"action": "focusPane", "id": 1},
                {"action": "newTab", "startingDirectory": "C:/c"},
                {"action": "switchToTab", "index": 0},
            ]
        )
        self.assertEqual([t["pane_count"] for t in tabs], [3, 1])
        self.assertEqual(
            tabs[0]["layout"][1],
            {
                "action": "splitPane",
                "slot": "pane-1",
                "cwd": "C:/b",
                "split": "right",
                "size": 0.75,
            },
        )
        self.assertNotIn("NEVER", str(tabs))
        self.assertEqual(tabs[0]["layout"][-1], {"action": "focusPane", "id": 1})

    def test_reject_bad_sequences(self):
        base = {"action": "newTab", "startingDirectory": "C:/a"}
        for action in [
            {"action": "splitPane", "split": "auto", "size": 0.5},
            {"action": "splitPane", "split": "down", "size": 1},
            {"action": "focusPane", "id": 1},
            {"action": "moveFocus", "direction": "garbage"},
            {"action": "closePane"},
        ]:
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.tabs([base, action])
        with self.assertRaises(ValueError):
            self.tabs([{"action": "splitPane", "split": "right", "size": 0.5}])

    def test_malformed_actions_raise_value_error(self):
        for action in [{"action": []}, {"action": "moveFocus", "direction": []}]:
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.tabs([{"action": "newTab"}, action])

    def test_separate_window_and_nonsequential_slots(self):
        layout = [
            {"action": "newTab", "slot": "pane-8", "cwd": "C:/a"},
            {"action": "splitPane", "slot": "pane-2", "cwd": "C:/b", "split": "right", "size": 0.5},
        ]
        args = terminal.restore_argv(
            {"name": "demo", "layout": layout}, window="-1", store_path="db", state_path="state"
        )
        self.assertEqual(args[:3], ["wt", "-w", "-1"])
        self.assertIn("pane-8", args)
        self.assertIn("pane-2", args)
        with self.assertRaises(ValueError):
            terminal.restore_argv(
                {"name": "demo", "layout": [layout[0], {**layout[1], "slot": "pane-8"}]}
            )

    def test_restore_managed_argv_and_rejects_injection(self):
        w = {
            "name": "demo",
            "layout": [
                {"action": "newTab", "slot": "pane-0", "cwd": "C:/O'Neil; project", "title": "a;b"},
                {
                    "action": "splitPane",
                    "slot": "pane-1",
                    "cwd": "C:/b",
                    "split": "down",
                    "size": 0.4,
                },
                {"action": "focusPane", "id": 0},
            ],
        }
        args = terminal.restore_argv(w, store_path="C:/a;b/db", state_path="C:/a b/state")
        self.assertEqual(args[:4], ["wt", "-w", "0", "new-tab"])
        self.assertEqual(args.count(";"), 2)
        self.assertIn("pwsh", args)
        self.assertIn("-NoExit", args)
        self.assertIn("-File", args)
        self.assertEqual(args[-3:], ["focus-pane", "-t", "0"])
        self.assertIn("-H", args)
        self.assertIn("0.4", args)
        self.assertFalse(any(";" in a.replace("\\;", "") for a in args if a != ";"))
        for name in ["bad;name", "-bad", "a b", "../x", "x" * 81]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                terminal.restore_argv({**w, "name": name})

    def test_default_location_and_ten_panes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Packages/Microsoft.WindowsTerminal_8wekyb3d8bbwe/LocalState/state.json"
            p.parent.mkdir(parents=True)
            actions = [{"action": "newTab", "startingDirectory": d, "tabTitle": "Shell"}]
            for n in range(9):
                actions += [
                    {
                        "action": "splitPane",
                        "startingDirectory": d,
                        "split": "right" if n % 2 else "down",
                        "size": 0.75 if n == 0 else 0.5,
                    },
                    {
                        "action": "moveFocus",
                        "direction": "previousInOrder" if n % 2 else "nextInOrder",
                    },
                ]
            actions.append({"action": "focusPane", "id": 4})
            p.write_text(
                json.dumps(
                    {
                        "persistedWindowLayouts": [
                            {"tabLayout": actions},
                            {"tabLayout": [{"action": "newTab", "startingDirectory": d}]},
                        ]
                    }
                )
            )
            with patch.dict(os.environ, {"LOCALAPPDATA": d}):
                tabs = terminal.saved_tabs()
            self.assertEqual(
                [(t["window"], t["tab"], t["pane_count"]) for t in tabs], [(0, 0, 10), (1, 0, 1)]
            )
            args = terminal.restore_argv(
                {"name": "demo", "layout": tabs[0]["layout"]},
                store_path=Path(d) / "db",
                state_path=Path(d) / "app.json",
            )
            self.assertEqual(args.count("split-pane"), 9)
            self.assertEqual(args[-3:], ["focus-pane", "-t", "4"])

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell required")
    def test_bootstrap_sets_child_context_and_returns_after_failure(self):
        with tempfile.TemporaryDirectory(prefix="workspace O'Neil; ") as d:
            root = Path(d)
            package = root / "ai_sessions"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "__main__.py").write_text(
                "import json,os,sys; from pathlib import Path; Path(os.environ['CAPTURE']).write_text(json.dumps({'argv':sys.argv[1:],'env':{k:v for k,v in os.environ.items() if k.startswith('AI_SESSIONS_')}})); sys.exit(7)"
            )
            driver = root / "driver.ps1"
            driver.write_text(
                "param($Bootstrap,$Python,$Root)\n& $Bootstrap -Workspace demo -Pane pane-2 -WorkspaceFile (Join-Path $Root 'db.sqlite3') -StateFile (Join-Path $Root 'state.json') -Python $Python\nWrite-Output ('RETURNED:' + $env:AI_SESSIONS_WORKSPACE_PANE)\nexit 0\n"
            )
            capture = root / "capture.json"
            env = {**os.environ, "PYTHONPATH": d, "CAPTURE": str(capture)}
            result = subprocess.run(
                [
                    shutil.which("pwsh"),
                    "-NoProfile",
                    "-File",
                    str(driver),
                    "-Bootstrap",
                    str(Path(terminal.__file__).with_name("launch.ps1")),
                    "-Python",
                    sys.executable,
                    "-Root",
                    d,
                ],
                env=env,
                cwd=d,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("RETURNED:pane-2", result.stdout)
            data = json.loads(capture.read_text())
            self.assertEqual(data["argv"], ["workspace", "run", "demo", "pane-2"])
            self.assertEqual(data["env"]["AI_SESSIONS_WORKSPACE"], "demo")
            self.assertEqual(data["env"]["AI_SESSIONS_WORKSPACE_FILE"], str(root / "db.sqlite3"))
            self.assertEqual(data["env"]["AI_SESSIONS_STATE_FILE"], str(root / "state.json"))


if __name__ == "__main__":
    unittest.main()
