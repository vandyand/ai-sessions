import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sessions.app import (
    Session,
    SessionRefresh,
    UserState,
    load_session_catalog,
    save_session_catalog,
)
from ai_sessions.config import LaunchConfig


def row() -> Session:
    return Session(
        "opencode",
        "ses_" + "A" * 26,
        "Catalog row",
        "/work",
        2,
        1,
        "preview",
        True,
        "database",
        is_open=True,
        open_pid=91,
    )


class StartupCatalogTests(unittest.TestCase):
    def test_catalog_round_trip_clears_live_state_and_reapplies_user_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "catalog.json"
            state = UserState(root / "state.json")
            item = row()
            state.set_hidden(item, True)
            save_session_catalog([item], path)
            loaded = load_session_catalog(state, path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].session_id, item.session_id)
        self.assertFalse(loaded[0].is_open)
        self.assertEqual(loaded[0].open_pid, 0)
        self.assertTrue(loaded[0].hidden)

    def test_invalid_catalog_is_an_empty_first_paint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(load_session_catalog(path=path), [])

    def test_background_refresh_publishes_only_a_complete_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = [row()]
            with (
                patch("ai_sessions.app.load_sessions", return_value=expected) as load,
                patch("ai_sessions.app.save_session_catalog") as save,
            ):
                refresh = SessionRefresh(
                    use_cache=True,
                    state=UserState(root / "state.json"),
                    config=LaunchConfig(path=root / "config.toml"),
                    catalog_path=root / "catalog.json",
                )
                sessions, _warnings, error = refresh.take(wait=True)
        self.assertEqual(sessions, expected)
        self.assertEqual(error, "")
        load.assert_called_once()
        save.assert_called_once_with(expected, root / "catalog.json")
        self.assertEqual(refresh.take(), (None, (), ""))


if __name__ == "__main__":
    unittest.main()
