import io
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ai_sessions import app
from ai_sessions.registry import REGISTRY


class CliSlugTests(unittest.TestCase):
    def setUp(self):
        self.stack = self.enterContext(ExitStack())
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.state = app.UserState(path=Path(tmp) / "state.json")
        self.item = SimpleNamespace(
            tool=REGISTRY.adapters()[0].name,
            session_id="native-id",
            slug="quiet-build",
            title="Build",
            named=True,
            compressed=True,
            hidden=True,
            origin="agent",
        )
        self.item.key = self.item.tool + ":native-id"
        self.stack.enter_context(patch.object(app, "UserState", return_value=self.state))
        self.stack.enter_context(patch.object(app, "load_sessions", return_value=[self.item]))
        self.stack.enter_context(patch.object(app, "save_session_catalog"))
        self.stack.enter_context(patch.object(app, "load_warnings", return_value=[]))
        self.stack.enter_context(patch.object(app.LaunchConfig, "load", return_value=MagicMock()))
        self.stack.enter_context(patch.object(app, "display_title", return_value="Build"))
        self.stack.enter_context(patch.object(self.state, "apply"))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(redirect_stderr(io.StringIO()))

    def test_all_actions_accept_slug_without_filtering(self):
        for reference in (
            "quiet-build",
            self.item.tool + ":quiet-build",
            "native-id",
            self.item.key,
        ):
            with self.subTest(reference=reference):
                self._exercise_actions(reference)

    def _exercise_actions(self, reference):
        for flag, function, extra in [
            ("--resume", "open_again", []),
            ("--supervise", "supervise_session", []),
            ("--rename", "rename_session", ["New name"]),
        ]:
            with self.subTest(flag=flag), patch.object(app, function, return_value=0) as action:
                self.assertEqual(app._main([flag, reference, *extra]), 0)
                self.assertIs(action.call_args.args[0 if flag != "--rename" else 1], self.item)
        for flag in ("--hide", "--unhide"):
            with self.subTest(flag=flag), patch.object(self.state, "set_hidden") as action:
                self.assertEqual(app._main([flag, reference]), 0)
                action.assert_called_once_with(self.item, flag == "--hide")
        for flag in ("--compress", "--decompress"):
            with (
                self.subTest(flag=flag),
                patch("ai_sessions.retention." + flag[2:] + "_session", return_value="") as action,
                patch.object(app, "sessions_are_known", return_value=True),
            ):
                self.assertEqual(app._main([flag, reference]), 0)
                action.assert_called_once_with(self.item)

    def test_ambiguity_does_not_resume(self):
        with (
            patch.object(app, "load_sessions", return_value=[self.item, self.item]),
            patch.object(app, "open_again") as action,
        ):
            self.assertEqual(app._main(["--resume", self.item.tool + ":native-id"]), 2)
            action.assert_not_called()

    def test_set_slug_uses_registry_without_rename(self):
        with (
            patch.object(self.state, "slugs", create=True) as slugs,
            patch.object(app, "rename_session") as rename,
        ):
            slugs.claim_owner.return_value = None
            slugs.lookup.return_value = None
            slugs.set_slug.return_value = "new-slug"
            self.assertEqual(app._main(["--set-slug", "quiet-build", "new-slug"]), 0)
            slugs.set_slug.assert_called_once()
            self.assertEqual(slugs.set_slug.call_args.args[1], "new-slug")
            rename.assert_not_called()

    def test_set_slug_persists_in_isolated_registry(self):
        from ai_sessions.references import SlugStore, native_key

        with tempfile.TemporaryDirectory() as tmp:
            store = SlugStore(Path(tmp) / "slugs.sqlite3")
            key = native_key(self.item.tool, self.item.session_id)
            store.ensure([(key, "Build")])
            with patch.object(self.state, "slugs", store, create=True):
                self.assertEqual(app._main(["--set-slug", "quiet-build", "custom-build"]), 0)
                self.assertEqual(store.slug_for(key), "custom-build")
                self.assertEqual(self.item.title, "Build")

    def test_invalid_slug_returns_error_without_renaming(self):
        with (
            patch.object(self.state, "slugs", create=True) as store,
            patch.object(app, "rename_session") as rename,
        ):
            store.claim_owner.return_value = None
            store.lookup.return_value = None
            store.set_slug.side_effect = ValueError("invalid slug")
            self.assertEqual(app._main(["--set-slug", "quiet-build", "INVALID"]), 2)
            rename.assert_not_called()

    def test_target_log_stays_catalog_independent(self):
        with (
            patch("ai_sessions.supervisor.create.target_log", return_value="log") as log,
            patch.object(app, "load_sessions") as discover,
        ):
            self.assertEqual(app._main(["--target-log", "quiet-build"]), 0)
            log.assert_called_once_with(self.state, "quiet-build", None, 20)
            discover.assert_not_called()

    def test_set_slug_exits_without_additional_actions(self):
        with (
            patch.object(self.state, "slugs") as store,
            patch.object(self.state, "set_hidden") as hide,
            patch.object(app, "open_again") as resume,
            patch.object(app, "list_output") as listing,
            patch.object(app.curses, "wrapper") as browser,
        ):
            store.claim_owner.return_value = None
            store.lookup.return_value = None
            store.set_slug.return_value = "custom-build"
            self.assertEqual(
                app._main(
                    [
                        "--set-slug",
                        "quiet-build",
                        "custom-build",
                        "--hide",
                        "quiet-build",
                        "--resume",
                        "quiet-build",
                        "--list",
                    ]
                ),
                0,
            )
            hide.assert_not_called()
            resume.assert_not_called()
            listing.assert_not_called()
            browser.assert_not_called()

    def test_retired_slug_cannot_hide_another_sessions_matching_title(self):
        from ai_sessions.references import native_key

        first = native_key(self.item.tool, self.item.session_id)
        second = native_key(self.item.tool, "other-id")
        self.state.slugs.ensure([(first, "foo"), (second, "foo")])
        self.state.slugs.set_slug(first, "bar")
        other = SimpleNamespace(
            tool=self.item.tool,
            session_id="other-id",
            key=self.item.tool + ":other-id",
            slug=self.state.slugs.slug_for(second),
            title="foo",
            named=True,
        )
        self.item.slug = "bar"
        with (
            patch.object(app, "load_sessions", return_value=[self.item, other]),
            patch.object(self.state, "set_hidden") as hide,
        ):
            self.assertEqual(app._main(["--hide", "foo"]), 2)
            hide.assert_not_called()
