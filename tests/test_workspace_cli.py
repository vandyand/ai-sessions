import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_sessions.model import NativeRef, Session
from ai_sessions.workspaces import cli


class WorkspaceCliTests(unittest.TestCase):
    def test_live_save_rereads_identified_native_sessions_missing_from_catalog(self):
        from ai_sessions.model import NativeSession

        row = NativeSession(
            "fake", "fresh", "New session", self.home, 0, 0, "", False, "fresh.jsonl"
        )
        self.adapter.rediscover = Mock(return_value=[row])
        self.adapter.resolve.return_value = NativeRef("fresh", "fresh.jsonl")
        snapshot = dict(
            layout=self.workspace["layout"],
            panes={"pane-1": dict(terminal="abc", x=0, y=0, width=100, height=100)},
        )
        remembered = {"abc": dict(tool="fake", session_id="fresh", home=self.home)}
        with (
            patch("ai_sessions.workspaces.capture.current_tab", return_value=snapshot),
            patch("ai_sessions.workspaces.setup.configure") as configure,
            patch("ai_sessions.app.load_session_catalog", return_value=[]),
            patch("ai_sessions.app.load_sessions", return_value=[]),
            patch("ai_sessions.detached._read", return_value=remembered),
        ):
            self.assertEqual(self.main("save", "desk"), 0)
        self.assertEqual([item.session_id for item in configure.call_args.args[2]], ["fresh"])

    def test_restart_keeps_slot_context_for_same_or_other_harness(self):
        from ai_sessions import app

        for into in ("", "other"):
            with self.subTest(into=into):

                def opened(*args, **kwargs):
                    app._restart_into = into
                    return 0

                def restarted(argv):
                    self.assertEqual(os.environ["AI_SESSIONS_WORKSPACE"], "desk")
                    self.assertEqual(os.environ["AI_SESSIONS_WORKSPACE_PANE"], "pane-1")
                    self.assertEqual(argv, ["--reopen", *(["--launch-tool", into] if into else [])])
                    return 7

                self.open.side_effect = opened
                with patch.object(app, "_main", side_effect=restarted) as reopen:
                    try:
                        self.assertEqual(self.main("run", "desk", "pane-1"), 7)
                        reopen.assert_called_once()
                    finally:
                        app._restart_into = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = str(Path(self.temp.name).resolve())
        self.binding = dict(
            tool="fake",
            session_id="original",
            home=self.home,
            title="Worker",
            cwd=self.home,
            storage="native.jsonl",
        )
        self.workspace = dict(
            name="desk",
            layout=[dict(action="newTab", slot="pane-1")],
            panes={"pane-1": self.binding},
            updated=0,
        )
        self.store = Mock(path=Path(self.temp.name) / "workspaces.sqlite3")
        self.store.get.return_value = self.workspace
        self.adapter = SimpleNamespace(
            home=Path(self.home), resolve=Mock(return_value=NativeRef("original", "latest.jsonl"))
        )
        self.state = Mock(path=Path(self.temp.name) / "state.json")
        self.output = io.StringIO()
        self.addCleanup(patch.stopall)
        patch("ai_sessions.workspaces.cli.WorkspaceStore", return_value=self.store).start()
        patch("ai_sessions.app.UserState", return_value=self.state).start()
        patch("ai_sessions.app.REGISTRY.get", return_value=self.adapter).start()
        patch("ai_sessions.app.LaunchConfig.load", return_value=Mock()).start()
        self.open = patch("ai_sessions.app.open_again", return_value=0).start()
        self.live = patch("ai_sessions.app.detect_open_sessions", return_value=True).start()
        self.restore = patch("ai_sessions.app._restore_native_session", return_value=True).start()
        patch.dict(os.environ, {"AI_SESSIONS_STATE_FILE": str(self.state.path)}).start()

    def main(self, *args):
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            return cli.main(list(args))

    def test_run_exact_and_supervisor_metadata(self):
        self.state._apply_supervisors.side_effect = lambda items: setattr(
            items[0], "supervises", "role"
        )
        self.assertEqual(self.main("run", "desk", "pane-1"), 0)
        item = self.open.call_args.args[0]
        self.assertEqual(
            (item.session_id, item.storage, item.supervises), ("original", "latest.jsonl", "role")
        )
        self.assertTrue(self.open.call_args.kwargs["exact_native"])

    def test_running_target_is_skipped_without_open_or_restore(self):
        self.live.side_effect = lambda items: setattr(items[0], "is_open", True) or True
        self.assertEqual(self.main("run", "desk", "pane-1"), 0)
        self.open.assert_not_called()
        self.restore.assert_not_called()
        self.assertIn("skip", self.output.getvalue().lower())

    def test_home_mismatch_rejected(self):
        self.binding["home"] = str(Path(self.home) / "other")
        self.assertEqual(self.main("run", "desk", "pane-1"), 2)
        self.adapter.resolve.assert_not_called()
        self.open.assert_not_called()

    def test_empty_adapter_home_is_explicitly_refused(self):
        self.adapter.home = None
        self.assertEqual(self.main("run", "desk", "pane-1"), 2)
        self.assertIn("does not expose", self.output.getvalue())
        self.adapter.resolve.assert_not_called()

    def test_binding_optional_values_are_strings(self):
        item = Session(
            "fake", "original", None, Path(self.home), 0, 0, "", True, Path("native.jsonl")
        )
        with (
            patch("ai_sessions.app.load_session_catalog", return_value=[item]) as catalog,
            patch("ai_sessions.references.resolve_session", return_value=item),
        ):
            self.assertEqual(self.main("bind", "desk", "pane-0", "original"), 0)
        catalog.assert_called_once_with(state=self.state)
        binding = self.store.bind.call_args.args[2]
        self.assertTrue(all(isinstance(value, str) for value in binding.values()))
        self.assertEqual(binding["title"], "")

    def test_empty_slot_prints_binding_instruction(self):
        self.workspace["panes"]["pane-1"] = None
        with patch.object(cli.sys.stdin, "isatty", return_value=False):
            self.assertEqual(self.main("run", "desk", "pane-1"), 0)
        self.assertIn("workspace bind desk pane-1", self.output.getvalue())
        self.open.assert_not_called()

    def test_dry_run_does_not_decompress(self):
        with patch.dict(os.environ, {"WT_SESSION": "existing-pane"}):
            self.assertEqual(self.main("run", "desk", "pane-1", "--dry-run"), 0)
        self.restore.assert_not_called()
        self.store.register_terminal.assert_not_called()
        self.assertTrue(self.open.call_args.kwargs["dry_run"])

    def test_restore_opens_unbound_panes_for_guided_selection(self):
        self.workspace["panes"]["pane-1"] = None
        with (
            patch.object(cli.subprocess, "Popen") as popen,
            patch.object(cli.terminal, "restore_argv", return_value=["wt"]),
        ):
            self.assertEqual(self.main("restore", "desk"), 0)
            popen.assert_called_once()

    def test_default_save_captures_this_tab_instead_of_asking_for_a_tab_index(self):
        snapshot = dict(
            layout=self.workspace["layout"],
            panes={"pane-1": dict(terminal="abc", x=0, y=0, width=100, height=100)},
        )
        with (
            patch("ai_sessions.workspaces.capture.current_tab", return_value=snapshot) as capture,
            patch("ai_sessions.workspaces.setup.configure") as configure,
            patch("ai_sessions.app.load_session_catalog", return_value=[]),
            patch("ai_sessions.app.load_sessions", return_value=[]),
            patch("ai_sessions.detached._read", return_value={}),
        ):
            self.assertEqual(self.main("save", "desk"), 0)
            capture.assert_called_once()
            self.assertEqual(configure.call_args.args[1], snapshot)

    def test_empty_interactive_pane_offers_picker_and_opens_selected_session(self):
        self.workspace["panes"]["pane-1"] = None
        item = Session("fake", "original", "Worker", self.home, 0, 0, "", True, "native.jsonl")
        with (
            patch.object(cli.sys.stdin, "isatty", return_value=True),
            patch("ai_sessions.app.load_session_catalog", return_value=[item]),
            patch("ai_sessions.workspaces.setup.choose_session", return_value=item) as choose,
        ):
            self.assertEqual(self.main("run", "desk", "pane-1"), 0)
            choose.assert_called_once()
            self.assertEqual(self.open.call_args.args[0].session_id, "original")

    def test_restore_argv_array_and_dry_run(self):
        with (
            patch.object(cli.terminal, "restore_argv", return_value=["wt", "-w", "0"]) as argv,
            patch.object(cli.subprocess, "Popen") as popen,
        ):
            self.assertEqual(self.main("restore", "desk", "--dry-run"), 0)
            popen.assert_not_called()
            self.assertEqual(self.main("restore", "desk"), 0)
            popen.assert_called_once_with(["wt", "-w", "0"])
            self.assertEqual(argv.call_args.kwargs["window"], "0")

    def test_bind_catalog_fallback_records_native_home(self):
        item = Session("fake", "original", "Worker", self.home, 0, 0, "", True, "native.jsonl")
        with (
            patch("ai_sessions.app.load_session_catalog", return_value=[]),
            patch("ai_sessions.app.load_sessions", return_value=[item]) as load,
            patch(
                "ai_sessions.references.resolve_session", side_effect=[ValueError("missing"), item]
            ),
        ):
            self.assertEqual(self.main("bind", "desk", "pane-1", "worker-slug"), 0)
            load.assert_called_once()
            expected = dict(self.binding, home=os.path.normcase(self.home))
            self.assertEqual(self.store.bind.call_args.args[2], expected)

    def test_unknown_liveness_refuses_duplicate(self):
        self.live.return_value = False
        self.assertEqual(self.main("run", "desk", "pane-1"), 2)
        self.open.assert_not_called()

    def test_resolver_cannot_substitute_another_fork(self):
        self.adapter.resolve.return_value = NativeRef("other-fork", "other.jsonl")
        self.assertEqual(self.main("run", "desk", "pane-1"), 2)
        self.open.assert_not_called()

    def test_bookkept_storage_fallback(self):
        self.adapter.resolve.return_value = None
        with (
            patch("ai_sessions.retention.needs_restore", return_value=True),
            patch("ai_sessions.retention.decompress_session", return_value="") as decompress,
        ):
            self.assertEqual(self.main("run", "desk", "pane-1"), 0)
            self.assertEqual(decompress.call_args.args[0].session_id, "original")
        self.assertEqual(self.open.call_args.args[0].storage, "native.jsonl")

    def test_slot_environment_is_scoped_to_launch(self):
        previous = os.environ.get("AI_SESSIONS_WORKSPACE")

        def opened(*args, **kwargs):
            self.assertEqual(os.environ["AI_SESSIONS_WORKSPACE"], "desk")
            self.assertEqual(os.environ["AI_SESSIONS_WORKSPACE_PANE"], "pane-1")
            self.assertEqual(os.environ["AI_SESSIONS_WORKSPACE_FILE"], str(self.store.path))
            return 0

        self.open.side_effect = opened
        self.assertEqual(self.main("run", "desk", "pane-1"), 0)
        self.assertEqual(os.environ.get("AI_SESSIONS_WORKSPACE"), previous)

    def test_allow_unbound_restore(self):
        self.workspace["panes"]["pane-1"] = None
        with (
            patch.object(cli.terminal, "restore_argv", return_value=["wt"]),
            patch.object(cli.subprocess, "Popen") as popen,
        ):
            self.assertEqual(self.main("restore", "desk", "--allow-unbound"), 0)
            popen.assert_called_once_with(["wt"])

    def test_restored_native_storage_is_plain(self):
        self.adapter.resolve.return_value = NativeRef("original", "latest.jsonl.zst")
        self.assertEqual(self.main("run", "desk", "pane-1"), 0)
        self.assertEqual(self.open.call_args.args[0].storage, "latest.jsonl")

    def test_bookkept_decompression_error_is_reported(self):
        from ai_sessions.compression import CompressionError

        self.adapter.resolve.return_value = None
        with (
            patch("ai_sessions.retention.needs_restore", return_value=True),
            patch(
                "ai_sessions.retention.decompress_session",
                side_effect=CompressionError("restore failed"),
            ),
        ):
            self.assertEqual(self.main("run", "desk", "pane-1"), 2)
        self.open.assert_not_called()
        self.assertIn("restore failed", self.output.getvalue())

    def test_save_ambiguous_noninteractive_rejected(self):
        tabs = [
            dict(window=0, tab=i, title="Shell", pane_count=1, layout=self.workspace["layout"])
            for i in range(2)
        ]
        with (
            patch.object(cli.terminal, "saved_tabs", return_value=tabs),
            patch.object(cli.sys.stdin, "isatty", return_value=False),
        ):
            self.assertEqual(self.main("save", "desk", "--state-file", "terminal-state.json"), 2)
            self.store.save.assert_not_called()

    def test_save_picker_geometry_and_no_bind_guess(self):
        tabs = [
            dict(window=0, tab=i, title="Shell", pane_count=1, layout=self.workspace["layout"])
            for i in range(2)
        ]
        with (
            patch.object(cli.terminal, "saved_tabs", return_value=tabs),
            patch.object(cli.sys.stdin, "isatty", return_value=True),
            patch("builtins.input", return_value="2"),
        ):
            self.assertEqual(
                self.main("save", "desk", "--state-file", "terminal-state.json", "--force"), 0
            )
            self.store.save.assert_called_once_with("desk", tabs[1]["layout"], replace=True)
            self.store.bind.assert_not_called()
            self.assertIn("pane-1", self.output.getvalue())

    def test_clone_uses_store_layout_only_contract(self):
        self.assertEqual(self.main("clone", "desk", "copy"), 0)
        self.store.clone.assert_called_once_with("desk", "copy")

    def test_json_show_and_list(self):
        import json

        self.assertEqual(self.main("show", "desk", "--json"), 0)
        self.assertEqual(json.loads(self.output.getvalue()), self.workspace)
        self.output.seek(0)
        self.output.truncate()
        self.store.names.return_value = [{"name": "desk"}]
        self.assertEqual(self.main("list", "--json"), 0)
        self.assertEqual(json.loads(self.output.getvalue()), [{"name": "desk"}])


class WorkspaceCliStoreTests(unittest.TestCase):
    def test_clone_clears_bindings_in_isolated_store(self):
        from ai_sessions.workspaces.store import WorkspaceStore

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workspaces.sqlite3"
            store = WorkspaceStore(path)
            store.save("source", [{"action": "newTab", "slot": "pane-1", "cwd": directory}])
            store.bind(
                "source",
                "pane-1",
                dict(tool="fake", session_id="original", home=directory, storage="native.jsonl"),
            )
            with (
                patch.dict(os.environ, {"AI_SESSIONS_WORKSPACE_FILE": str(path)}),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli.main(["clone", "source", "copy"]), 0)
            self.assertEqual(store.get("copy")["panes"], {"pane-1": None})
            self.assertEqual(store.get("source")["panes"]["pane-1"]["session_id"], "original")


if __name__ == "__main__":
    unittest.main()
