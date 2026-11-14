import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sessions.workspaces import native, terminal


class NativeWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.exe = self.root / "WindowsTerminal.exe"
        self.exe.touch()
        (self.root / ".portable").touch()
        self.store = self.root / "state.workspaces.sqlite3"
        self.state = self.root / "state.json"
        self.workspace = dict(name="demo", layout=[dict(action="newTab", cwd=str(self.root))])
        with patch.object(native, "terminal_version", return_value=(1, 26)):
            native.configure(self.store, self.exe)

    def open(self):
        return native.open_argv(self.workspace, self.store, self.state)

    def save(self, command=None):
        launch = terminal.pane_argv("demo", "pane-0", self.store, self.state)
        self.saved_launch = launch
        saved = {
            "persistedWorkspaces": {
                native.window_name("demo", self.store): {
                    "tabLayout": [
                        dict(
                            action="newTab", commandline=command or subprocess.list2cmdline(launch)
                        )
                    ]
                }
            }
        }
        target = self.root / "settings" / "state.json"
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(saved))
        return target

    def test_first_open_contains_durable_pane_launcher(self):
        args = self.open()
        self.assertEqual(args[:3], [str(self.exe), "-w", native.window_name("demo", self.store)])
        self.assertIn("-File", args)
        self.assertIn("pane-0", args)
        self.assertIn(str(self.store), args)

    def test_reopen_uses_native_geometry_without_extra_tabs(self):
        target = self.save()
        before = target.read_bytes()
        with patch.object(native, "split_commandline", return_value=self.saved_launch):
            self.assertEqual(
                self.open(),
                [
                    str(self.exe),
                    "-w",
                    native.window_name("demo", self.store),
                    "focus-tab",
                    "-t",
                    "0",
                ],
            )
        self.assertEqual(target.read_bytes(), before)

    def test_reopen_refuses_foreign_pane_command(self):
        self.save("unrelated-command")
        with patch.object(native, "split_commandline", return_value=["unrelated-command"]):
            with self.assertRaisesRegex(ValueError, "launcher"):
                self.open()

    def test_reopen_refuses_changed_state_or_store(self):
        self.save()
        changed = [
            str(self.root / "other.json") if a == str(self.state) else a for a in self.saved_launch
        ]
        with patch.object(native, "split_commandline", return_value=changed):
            with self.assertRaisesRegex(ValueError, "launcher"):
                self.open()

    def test_unsupported_version_does_not_overwrite_preference(self):
        with patch.object(native, "terminal_version", return_value=(1, 24)):
            with self.assertRaisesRegex(ValueError, "1.26"):
                native.configure(self.store, self.exe)

    def test_repeated_open_before_first_close_is_refused(self):
        native.mark_created(self.store, "demo")
        with self.assertRaisesRegex(ValueError, "not saved"):
            self.open()

    def test_missing_configuration_has_actionable_message(self):
        with self.assertRaisesRegex(ValueError, "workspace terminal"):
            native.open_argv(self.workspace, self.root / "other.sqlite3", self.state)

    def test_missing_saved_slot_is_refused(self):
        self.workspace["layout"].append(dict(action="splitPane", split="right", size=0.5))
        self.save()
        # Only the first pane survives in the native snapshot.
        with patch.object(native, "split_commandline", return_value=self.saved_launch):
            with self.assertRaisesRegex(ValueError, "panes"):
                self.open()

    def test_names_are_isolated_by_database_and_fit_terminal_limit(self):
        self.assertNotEqual(
            native.window_name("demo", self.store),
            native.window_name("demo", self.root / "other.sqlite3"),
        )
        self.assertLessEqual(len(native.window_name("x" * 80, self.store)), 80)
        self.assertNotEqual(
            native.window_name("x" * 79 + "a", self.store),
            native.window_name("x" * 79 + "b", self.store),
        )

    def test_recovery_uses_a_fresh_window_instead_of_appending_to_pending_window(self):
        native.mark_created(self.store, "demo")
        args = native.open_argv(self.workspace, self.store, self.state, recover=True)
        self.assertNotEqual(args[2], native.window_name("demo", self.store))
        self.assertIn("new-tab", args)

    def test_title_named_pwsh_does_not_change_launcher_detection(self):
        self.workspace["layout"][0]["title"] = "pwsh"
        self.workspace["layout"][0]["profile"] = "pwsh"
        self.save()
        with patch.object(native, "split_commandline", return_value=self.saved_launch):
            self.assertIn("focus-tab", self.open())

    def test_failed_spawn_does_not_mark_window_created(self):
        with patch.object(native.subprocess, "Popen", side_effect=OSError("spawn failed")):
            with self.assertRaises(OSError):
                native.launch(self.workspace, self.store, self.state)
        self.assertIn("new-tab", self.open())

    def test_successful_spawn_blocks_duplicate_creation_until_saved(self):
        with patch.object(native.subprocess, "Popen") as popen:
            native.launch(self.workspace, self.store, self.state)
            with self.assertRaisesRegex(ValueError, "not saved"):
                native.launch(self.workspace, self.store, self.state)
            popen.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "Windows argument parser")
    def test_real_windows_parser_preserves_quoted_paths_and_semicolons(self):
        argv = terminal.pane_argv(
            "demo",
            "pane-0",
            self.root / "O'Neil; project" / "db",
            self.root / 'a "quoted" file.json',
        )
        self.assertEqual(native.split_commandline(subprocess.list2cmdline(argv)), argv)

    def test_invalid_terminal_json_is_left_unchanged(self):
        target = self.save()
        target.write_text("{broken")
        with self.assertRaises(ValueError):
            self.open()
        self.assertEqual(target.read_text(), "{broken")


if __name__ == "__main__":
    unittest.main()
