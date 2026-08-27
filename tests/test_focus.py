"""Focusing an open session, including recovering one whose terminal died."""

import subprocess
import unittest
from unittest.mock import patch

from ai_sessions import app
from ai_sessions.app import Session, focus_open_session, terminal_argv, tmux_attach_command
from ai_sessions.config import LaunchConfig

PANE = "%19"
WINDOW = "@7"
SESSION = "2"


def result(stdout: str = "", code: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, stdout, "")


class FocusHarness(unittest.TestCase):
    """Fakes for everything ``focus_open_session`` shells out to or reads."""

    def setUp(self) -> None:
        self.clients = f"{SESSION}\t555"
        self.windows = ""
        self.raise_succeeds = True
        self.spawned: list[list[str]] = []
        self.spawn_succeeds = True

    def run_capture(self, argv, env=None):
        if argv[:2] == ["tmux", "display-message"]:
            return result(f"{SESSION}\t{WINDOW}\t1\t{PANE}")
        if argv[:2] in (["tmux", "select-window"], ["tmux", "select-pane"]):
            return result()
        if argv[:2] == ["tmux", "list-clients"]:
            return result(self.clients)
        if argv[:1] == ["wmctrl"] and argv[1] == "-lpGx":
            return result(self.windows)
        if argv[:1] == ["wmctrl"]:
            return result(code=0 if self.raise_succeeds else 1)
        if argv[:1] == ["xdotool"]:
            return result()
        if argv[:2] == ["tmux", "display-message"]:
            return result()
        return result()

    def focus(self, config: LaunchConfig | None = None, display: str = ":0"):
        item = Session("claude", "s", "", "", 0, 0, "", False, "storage", is_open=True, open_pid=99)
        environment = {"TMUX_PANE": PANE, "DISPLAY": display} if display else {"TMUX_PANE": PANE}

        def spawn(argv, env):
            self.spawned.append(argv)
            return self.spawn_succeeds

        with (
            patch.object(app, "IS_WINDOWS", False),
            patch.object(app, "process_start_token", return_value="token"),
            patch.object(app, "process_environment", return_value=environment),
            patch.object(app, "process_ancestry", return_value=[555]),
            patch.object(app, "process_state", return_value="S"),
            patch.object(app, "run_capture", side_effect=self.run_capture),
            patch.object(app, "spawn_terminal", side_effect=spawn),
            patch.dict("os.environ", {"DISPLAY": display} if display else {}, clear=True),
        ):
            return focus_open_session(item, config)


class ClientlessRecoveryTests(FocusHarness):
    def test_a_session_with_no_client_gets_a_terminal_opened_on_it(self) -> None:
        self.clients = ""
        with patch.object(app.shutil, "which", side_effect=lambda name: name == "kitty"):
            focused, note = self.focus()
        self.assertTrue(focused)
        self.assertEqual(len(self.spawned), 1)
        argv = self.spawned[0]
        self.assertEqual(argv[0], "kitty")
        script = argv[-1]
        # The pane selection travels with the attach so nothing can move the
        # session between the two steps.
        self.assertIn(f"attach-session -t {SESSION}", script)
        self.assertIn(f"select-window -t {WINDOW}", script)
        self.assertIn(f"select-pane -t {PANE}", script)
        self.assertIn("opened kitty", note)

    def test_an_attached_raisable_session_is_focused_and_nothing_is_spawned(self) -> None:
        self.windows = "0x01  0  555  0 0 0 0  host  tmux:2"
        focused, note = self.focus()
        self.assertTrue(focused)
        self.assertEqual(self.spawned, [])
        self.assertIn("Focused", note)

    def test_an_attached_but_unraisable_session_never_spawns_a_second_client(self) -> None:
        # Two clients on one session leave two windows fighting over one pane,
        # and tmux sizes to the smallest.
        self.windows = ""
        self.raise_succeeds = False
        focused, note = self.focus()
        self.assertFalse(focused)
        self.assertEqual(self.spawned, [])
        self.assertIn("no second one was opened", note)
        self.assertIn(f"tmux attach-session -t {SESSION}", note)

    def test_without_a_display_nothing_is_spawned_and_the_command_is_given(self) -> None:
        self.clients = ""
        focused, note = self.focus(display="")
        self.assertFalse(focused)
        self.assertEqual(self.spawned, [])
        self.assertIn(f"tmux attach-session -t {SESSION}", note)

    def test_a_failed_spawn_still_names_the_manual_command(self) -> None:
        self.clients = ""
        self.spawn_succeeds = False
        with patch.object(app.shutil, "which", side_effect=lambda name: name == "kitty"):
            focused, note = self.focus()
        self.assertFalse(focused)
        self.assertIn(f"tmux attach-session -t {SESSION}", note)

    def test_no_terminal_found_points_at_the_config_and_the_command(self) -> None:
        self.clients = ""
        with patch.object(app.shutil, "which", return_value=None):
            focused, note = self.focus()
        self.assertFalse(focused)
        self.assertEqual(self.spawned, [])
        self.assertIn("[terminal] command", note)
        self.assertIn(f"tmux attach-session -t {SESSION}", note)


class TitleFallbackTests(FocusHarness):
    def test_session_2_does_not_focus_a_window_titled_tmux_21(self) -> None:
        # Substring matching against the whole wmctrl line made session 2
        # match tmux:21, tmux:212, and so on.
        self.windows = "0x01  0  4242  0 0 0 0  host  tmux:21"
        focused, note = self.focus()
        self.assertFalse(focused)
        self.assertIn("could not be raised", note)

    def test_the_exact_title_is_still_usable_while_a_client_is_attached(self) -> None:
        self.windows = "0x01  0  4242  0 0 0 0  host  tmux:2"
        focused, _ = self.focus()
        self.assertTrue(focused)

    def test_a_window_whose_client_switched_away_is_not_focused_for_the_old_session(self) -> None:
        # The title is fixed at launch and survives switch-client, so a window
        # still titled tmux:2 may be displaying something else entirely.  With
        # no client on the session, the title proves nothing.
        self.clients = "main\t555"
        self.windows = "0x01  0  4242  0 0 0 0  host  tmux:2"
        with patch.object(app.shutil, "which", side_effect=lambda name: name == "kitty"):
            focused, note = self.focus()
        self.assertTrue(focused)
        self.assertEqual(len(self.spawned), 1, "recovered by attaching, not by a stale title")
        self.assertIn("opened kitty", note)


class TerminalResolutionTests(unittest.TestCase):
    def test_a_configured_command_wins_and_is_substituted(self) -> None:
        config = LaunchConfig(terminal_command=["myterm", "--title", "tmux:{session}", "{script}"])
        argv = terminal_argv(config, "2", "@7", "%19")
        self.assertEqual(argv[:3], ["myterm", "--title", "tmux:2"])
        self.assertIn("attach-session -t 2", argv[3])

    def test_a_configured_command_without_a_script_placeholder_still_attaches(self) -> None:
        config = LaunchConfig(terminal_command=["myterm", "-e"])
        argv = terminal_argv(config, "2", "@7", "%19")
        self.assertEqual(argv[:3], ["myterm", "-e", "bash"])
        self.assertIn("attach-session -t 2", argv[-1])

    def test_the_terminal_environment_variable_is_used_before_probing(self) -> None:
        with (
            patch.dict("os.environ", {"TERMINAL": "myterm"}, clear=True),
            patch.object(app.shutil, "which", side_effect=lambda name: name == "myterm"),
        ):
            argv = terminal_argv(None, "2", "@7", "%19")
        self.assertEqual(argv[0], "myterm")

    def test_probing_falls_through_to_whatever_is_installed(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(app.shutil, "which", side_effect=lambda name: name == "xterm"),
        ):
            argv = terminal_argv(None, "2", "@7", "%19")
        self.assertEqual(argv[0], "xterm")

    def test_no_terminal_installed_resolves_to_nothing(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(app.shutil, "which", return_value=None),
        ):
            self.assertEqual(terminal_argv(None, "2", "@7", "%19"), [])

    def test_session_names_needing_quoting_are_quoted(self) -> None:
        script = tmux_attach_command("my session", "@7", "%19")
        self.assertIn("'my session'", script)

    def test_a_session_without_window_or_pane_still_attaches(self) -> None:
        self.assertEqual(tmux_attach_command("2", "", ""), "tmux attach-session -t 2")


if __name__ == "__main__":
    unittest.main()
