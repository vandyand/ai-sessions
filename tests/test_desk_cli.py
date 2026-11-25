import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sessions.desk import cli, launch
from ai_sessions.model import Session


class DeskCliTests(unittest.TestCase):
    def test_resolves_exact_native_source_without_catalog_sweep(self):
        item = Session("codex", "abc", "Operations", ".", 0, 0, "", True, "log.jsonl")
        with (
            patch("ai_sessions.app.load_session_catalog", return_value=[item]),
            patch("ai_sessions.app.load_sessions", side_effect=AssertionError("unexpected sweep")),
        ):
            sources, cwd = cli.resolve_sources("codex:abc", [])
        self.assertEqual(sources[0]["session_id"], "abc")
        self.assertEqual(sources[0]["role"], "supervisor")
        self.assertTrue(Path(cwd).is_absolute())

    def test_compressed_source_refuses(self):
        item = Session("codex", "abc", "Operations", ".", 0, 0, "", True, "log", compressed=True)
        with patch("ai_sessions.app.load_session_catalog", return_value=[item]):
            with self.assertRaisesRegex(ValueError, "compressed"):
                cli.resolve_sources("abc", [])

    def test_bad_interval_fails_before_model_or_store(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["watch", "elevate", "--interval", "0"])

    def test_cli_routes_without_loading_catalog_for_reads(self):
        with (
            patch("ai_sessions.desk.cli.Store") as factory,
            patch(
                "ai_sessions.app.load_session_catalog", side_effect=AssertionError("catalog read")
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            factory.return_value.list.return_value = [{"name": "elevate"}]
            self.assertEqual(cli.main(["list", "--json"]), 0)
        self.assertIn("elevate", output.getvalue())


class DeskLaunchTests(unittest.TestCase):
    def test_instructions_keep_team_traffic_out_and_preserve_dialogue(self):
        text = launch.instructions(
            {
                "name": "Elevate",
                "cwd": "C:/project",
                "sources": [{"tool": "codex", "session_id": "ops", "role": "supervisor"}],
            },
            Path("C:/desk"),
        )
        self.assertIn("review.md", text)
        self.assertIn("decisions.md", text)
        self.assertIn("codex:ops", text)
        self.assertIn("not user consent", text)
        self.assertIn("discuss", text)

    def test_dry_run_does_not_create_native_session_or_bind(self):
        with tempfile.TemporaryDirectory() as root:

            class Fake:
                def get(self, name):
                    return {"name": name, "cwd": root, "sources": [], "agents": {}}

                def folder(self, name):
                    return Path(root) / name

                def bind_agent(self, *args):
                    raise AssertionError("binding during dry-run")

            with (
                patch("ai_sessions.new_session.run", side_effect=AssertionError("launch")),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(launch.open_agent(Fake(), "test", "claude", dry_run=True), 0)
            self.assertIn("--session-id", output.getvalue())
            self.assertFalse((Path(root) / "test").exists())

    def test_resume_repeats_initialization_even_if_first_launch_was_interrupted(self):
        command = launch.arguments("claude", "abc", fresh=False, prompt="instructions")
        self.assertEqual(command, ["--resume", "abc", "instructions"])

    def test_codex_keeps_native_resume(self):
        self.assertEqual(
            launch.arguments("codex", "abc", fresh=True, prompt="hello"), ["resume", "abc", "hello"]
        )


class DeskIntegrationTests(unittest.TestCase):
    def test_app_dispatches_desk_without_browser_discovery(self):
        from ai_sessions.app import _main

        with patch("ai_sessions.desk.cli.main", return_value=0) as handler:
            self.assertEqual(_main(["desk", "list"]), 0)
        handler.assert_called_once_with(["list"])

    def test_real_store_dry_run_does_not_create_projection_directory(self):
        from ai_sessions.desk.store import Store

        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.create(
                "dry",
                [{"tool": "claude", "session_id": "fiction", "storage": "fiction.jsonl"}],
                root,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                launch.open_agent(store, "dry", "claude", dry_run=True)
            self.assertFalse((Path(root) / "dry").exists())
            self.assertEqual(store.get("dry")["agents"], {})

    def test_refresh_failure_returns_error_not_updated(self):
        with (
            patch("ai_sessions.desk.cli.Store"),
            patch("ai_sessions.desk.observer.refresh", return_value={"error": "model unavailable"}),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(cli.main(["refresh", "demo"]), 2)
        self.assertNotIn("Updated", output.getvalue())


class DeskLifetimeTests(unittest.TestCase):
    def test_view_exit_leaves_detached_observer_running(self):
        with (
            patch("ai_sessions.desk.cli.Store"),
            patch("ai_sessions.desk.cli.ensure_observer") as ensure,
            patch("ai_sessions.desk.cli.stop_observer") as stop,
            patch("ai_sessions.desk.view.run"),
        ):
            self.assertEqual(cli.main(["view", "demo", "--watch"]), 0)
        ensure.assert_called_once()
        stop.assert_not_called()


class DeskContextTests(unittest.TestCase):
    def test_new_agent_does_not_inherit_supervisor_or_sender_identity(self):
        import os

        from ai_sessions.desk.store import Store

        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.create(
                "demo", [{"tool": "claude", "session_id": "ops", "storage": "fiction.jsonl"}], root
            )
            old_cwd = Path.cwd()

            def native(tool, args):
                self.assertNotIn("AI_SESSIONS_SUPERVISOR", os.environ)
                self.assertNotIn("AI_SESSIONS_MESSAGE_FROM", os.environ)
                self.assertNotIn("CODEX_THREAD_ID", os.environ)
                raise RuntimeError("fictional startup failure")

            with (
                patch.dict(
                    os.environ,
                    {
                        "AI_SESSIONS_SUPERVISOR": "old-supervisor",
                        "AI_SESSIONS_MESSAGE_FROM": "old-sender",
                        "CODEX_THREAD_ID": "old-thread",
                    },
                ),
                patch("ai_sessions.desk.launch.create_id", return_value="fiction"),
                patch("ai_sessions.new_session.run", side_effect=native),
            ):
                with self.assertRaisesRegex(RuntimeError, "fictional startup"):
                    launch.open_agent(store, "demo", "claude")
                self.assertEqual(os.environ["AI_SESSIONS_SUPERVISOR"], "old-supervisor")
                self.assertEqual(os.environ["AI_SESSIONS_MESSAGE_FROM"], "old-sender")
                self.assertEqual(os.environ["CODEX_THREAD_ID"], "old-thread")
            self.assertEqual(Path.cwd(), old_cwd)


class DeskPlainOutputTests(unittest.TestCase):
    def test_show_does_not_execute_model_terminal_sequences(self):
        with (
            patch("ai_sessions.desk.cli.Store") as factory,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            factory.return_value.get.return_value = {"review": "hello\x1b[31mred", "decisions": []}
            self.assertEqual(cli.main(["show", "demo"]), 0)
        self.assertNotIn("\x1b", output.getvalue())
        self.assertIn("hello", output.getvalue())


class DeskRetryTests(unittest.TestCase):
    def test_failed_observations_back_off_instead_of_burning_usage_each_poll(self):
        waits = []

        class Stop:
            def is_set(self):
                return len(waits) >= 3

            def wait(self, seconds):
                waits.append(seconds)

        with (
            patch(
                "ai_sessions.desk.observer.refresh",
                side_effect=[{"error": "failed"}, {"error": "failed"}, {"error": None}],
            ),
            patch("ai_sessions.desk.cli.time.monotonic", return_value=0),
        ):
            cli.watch(None, "fiction", 60, Stop())
        self.assertEqual(waits, [120, 240, 60])


class DeskLiveHandleTests(unittest.TestCase):
    def test_view_watches_by_default_and_readonly_opts_out(self):
        for flags, expected in (([], 1), (["--watch"], 1), (["--no-watch"], 0)):
            with (
                patch("ai_sessions.desk.cli.Store"),
                patch("ai_sessions.desk.cli.ensure_observer") as observer,
                patch("ai_sessions.desk.view.run"),
            ):
                self.assertEqual(cli.main(["view", "demo", *flags]), 0)
            self.assertEqual(observer.call_count, expected)

    def test_view_resolves_session_handle_to_saved_desk(self):
        item = Session("codex", "ops", "Supervisor", ".", 0, 0, "", True, "ops.jsonl")
        with tempfile.TemporaryDirectory() as root:
            from ai_sessions.desk.store import Store

            store = Store(root)
            store.create(
                "old-desk-name",
                [
                    {
                        "tool": "codex",
                        "session_id": "ops",
                        "storage": str(Path("ops.jsonl").resolve()),
                    }
                ],
                root,
            )
            with (
                patch("ai_sessions.desk.cli.Store", return_value=store),
                patch("ai_sessions.handles_cli.catalog", return_value=([item], object())),
                patch("ai_sessions.references.resolve_session", return_value=item),
                patch("ai_sessions.desk.view.run") as view,
            ):
                self.assertEqual(cli.main(["view", "elev-sup", "--no-watch"]), 0)
                view.assert_called_once_with(store, "old-desk-name")

    def test_duplicate_desks_refuse_before_starting_observation(self):
        item = Session("codex", "ops", "Supervisor", ".", 0, 0, "", True, "ops.jsonl")
        with tempfile.TemporaryDirectory() as root:
            from ai_sessions.desk.store import Store

            store = Store(root)
            for name in ("one", "two"):
                store.create(
                    name,
                    [
                        {
                            "tool": "codex",
                            "session_id": "ops",
                            "storage": str(Path("ops.jsonl").resolve()),
                        }
                    ],
                    root,
                )
            with (
                patch("ai_sessions.desk.cli.Store", return_value=store),
                patch("ai_sessions.handles_cli.catalog", return_value=([item], object())),
                patch("ai_sessions.references.resolve_session", return_value=item),
                patch("ai_sessions.desk.cli.ensure_observer") as observer,
                contextlib.redirect_stderr(io.StringIO()) as error,
            ):
                self.assertEqual(cli.main(["view", "elev-sup"]), 2)
            self.assertIn("Multiple desks", error.getvalue())
            observer.assert_not_called()


def test_saved_desk_name_remains_readable_after_handle_retirement():
    with (
        patch("ai_sessions.desk.cli.Store") as factory,
        patch("ai_sessions.handles.HandleStore") as handles,
        patch("ai_sessions.desk.view.run") as view,
        patch("ai_sessions.desk.cli.ensure_observer") as watcher,
    ):
        factory.return_value.get.return_value = {"name": "old-handle"}
        handles.return_value.current_key.side_effect = ValueError("handle retired; use new-handle")
        assert cli.main(["view", "old-handle", "--no-watch"]) == 0
        view.assert_called_once_with(factory.return_value, "old-handle")
        watcher.assert_not_called()


class DeskStaleCatalogTests(unittest.TestCase):
    def test_saved_name_uses_unique_running_linked_copy_over_old_activity(self):
        from ai_sessions.references import native_key

        old = native_key("grok", "old")
        new = native_key("codex", "new")
        entry = dict(tool=new.tool, session_id=new.session_id, home=new.home)
        item = Session("codex", "new", "Supervisor", ".", 0, 0, "", True, "new.jsonl")
        record = {"name": "lego-sup-1"}
        with (
            patch("ai_sessions.desk.cli.Store") as factory,
            patch("ai_sessions.handles.HandleStore") as handles,
            patch("ai_sessions.detached.remembered", return_value={"tab": entry}),
            patch("ai_sessions.detached._live_remembered", return_value=True),
            patch("ai_sessions.handles_cli.catalog", return_value=([item], object())),
            patch("ai_sessions.references.resolve_session", return_value=item) as resolve,
            patch.object(cli, "target_desks", return_value=[record]),
            patch.object(cli, "update_target") as update,
        ):
            factory.return_value.get.return_value = record
            handles.return_value.current_key.return_value = old
            handles.return_value.lookup_members.return_value = [old, new]
            self.assertEqual(
                cli.resolve_desk_name(factory.return_value, "lego-sup-1", update=True), "lego-sup-1"
            )
            self.assertEqual(resolve.call_args.args[0], "codex:new")
            update.assert_called_once_with(factory.return_value, record, item)

    def test_view_refreshes_missing_canonical_copy_before_retargeting(self):
        from ai_sessions.references import native_key

        old = Session("grok", "old", "Supervisor", ".", 0, 0, "", True, "old.jsonl")
        new = Session("codex", "new", "Supervisor", ".", 0, 0, "", True, "new.jsonl")
        record = {"name": "lego-sup-1"}
        with (
            patch("ai_sessions.desk.cli.Store") as factory,
            patch("ai_sessions.handles.HandleStore") as handles,
            patch("ai_sessions.handles_cli.catalog", return_value=([old], object())),
            patch(
                "ai_sessions.references.resolve_session", side_effect=[ValueError("missing"), new]
            ),
            patch("ai_sessions.app.load_sessions", return_value=[old, new]) as discover,
            patch("ai_sessions.app.save_session_catalog") as save,
            patch.object(cli, "target_desks", return_value=[record]),
            patch.object(cli, "update_target") as update,
            patch.object(cli, "ensure_observer"),
            patch("ai_sessions.desk.view.run"),
        ):
            factory.return_value.get.return_value = record
            handles.return_value.current_key.return_value = native_key("codex", "new")
            self.assertEqual(cli.main(["view", "lego-sup-1"]), 0)
            discover.assert_called_once()
            save.assert_called_once_with([old, new])
            update.assert_called_once_with(factory.return_value, record, new)

    def test_existing_ambiguous_canonical_copy_does_not_trigger_retry(self):
        from ai_sessions.references import native_key

        item = Session("codex", "new", "Supervisor", ".", 0, 0, "", True, "new.jsonl")
        with (
            patch("ai_sessions.desk.cli.Store") as factory,
            patch("ai_sessions.handles.HandleStore") as handles,
            patch("ai_sessions.handles_cli.catalog", return_value=([item, item], object())),
            patch("ai_sessions.references.resolve_session", side_effect=ValueError("ambiguous")),
            patch("ai_sessions.app.load_sessions") as discover,
        ):
            handles.return_value.current_key.return_value = native_key("codex", "new")
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                cli.resolve_desk_name(factory.return_value, "lego-sup-1", update=True)
            discover.assert_not_called()
