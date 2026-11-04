import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sessions import app
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

    def copies(self, root: Path) -> tuple[UserState, list[Session]]:
        state = UserState(root / "state.json")
        old, head = row(), row()
        old.session_id, head.session_id = "ses_" + "B" * 26, "ses_" + "C" * 26
        for item in (old, head):
            item.conversation_id = "conv"
        old.superseded = True
        head.title = head.original_title = head.provider_title = "Named once"
        state.conversations["conv"] = {
            "members": {old.key: {}, head.key: {}},
            "history": [],
        }
        return state, [old, head]

    def test_the_first_paint_groups_copies_without_reading_their_transcripts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, (old, head) = self.copies(root)
            path = root / "catalog.json"
            save_session_catalog([old, head], path)
            self.assertEqual(path.read_text(encoding="utf-8").count("Named once"), 1)
            with patch.object(
                UserState, "_apply_conversation_status", side_effect=AssertionError("read")
            ):
                loaded = {item.session_id: item for item in load_session_catalog(state, path)}
        self.assertEqual(loaded[old.session_id].conversation_id, "conv")
        self.assertTrue(loaded[old.session_id].superseded)
        self.assertFalse(loaded[head.session_id].superseded)
        self.assertEqual(loaded[head.session_id].original_title, "Named once")
        self.assertEqual(loaded[head.session_id].provider_title, "Named once")

    def test_a_copy_made_after_the_catalog_was_saved_joins_its_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, (old, head) = self.copies(root)
            newer = row()
            newer.session_id = "ses_" + "D" * 26
            path = root / "catalog.json"
            save_session_catalog([old, head, newer], path)
            state.conversations["conv"]["members"][newer.key] = {}
            del state.conversations["conv"]["members"][old.key]
            loaded = {item.session_id: item for item in load_session_catalog(state, path)}
        self.assertEqual(loaded[newer.session_id].conversation_id, "conv")
        self.assertFalse(loaded[newer.session_id].superseded)
        self.assertEqual(loaded[old.session_id].conversation_id, "")
        self.assertFalse(loaded[old.session_id].superseded)

    def test_a_catalog_from_the_previous_version_is_checked_in_full(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, items = self.copies(root)
            path = root / "catalog.json"
            save_session_catalog(items, path)
            document = json.loads(path.read_text(encoding="utf-8"))
            document["version"] = 1
            for payload in document["sessions"]:
                payload.setdefault("original_title", payload["title"])
                payload.setdefault("provider_title", payload["title"])
            path.write_text(json.dumps(document), encoding="utf-8")
            with patch.object(UserState, "_apply_conversation_status") as checked:
                self.assertEqual(len(load_session_catalog(state, path)), 2)
        checked.assert_called_once()

    def test_the_previous_catalog_file_is_read_until_a_refresh_writes_the_new_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "session-catalog-v1.json"
            save_session_catalog([row()], legacy)
            with (
                patch.object(app, "CATALOG_CACHE_FILE", root / "session-catalog-v2.json"),
                patch.object(app, "LEGACY_CATALOG_FILE", legacy),
            ):
                loaded = load_session_catalog(path=app.CATALOG_CACHE_FILE)
        self.assertEqual(len(loaded), 1)


if __name__ == "__main__":
    unittest.main()
