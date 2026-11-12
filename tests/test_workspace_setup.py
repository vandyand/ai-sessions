import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_sessions.model import Session
from ai_sessions.workspaces import setup


def session(ident="one", title="Alpha"):
    return Session(
        "sample", ident, title, "/work", 0, 0, "", True, "/storage", slug="slug-" + ident
    )


class SetupTests(unittest.TestCase):
    def pick(self, items, answers):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = setup.choose_session(items, input_fn=Mock(side_effect=answers))
        return result, output.getvalue()

    def test_picker_number_search_and_identity(self):
        a, b = session(), session("two", "Beta")
        for answers, expected in [(["2"], b), (["Beta", "1"], b), (["slug-one"], a), (["two"], b)]:
            selected, output = self.pick([a, b], answers)
            self.assertIs(selected, expected)
            for label in ["Alpha", "sample", "slug-one", "one"]:
                self.assertIn(label, output)

    def test_picker_bounded_even_after_filter(self):
        items = [session(str(i), "Common " + str(i)) for i in range(6000)]
        selected, output = self.pick(items, ["Common", "5999"])
        self.assertIs(selected, items[-1])
        self.assertLess(len(output.splitlines()), 55)
        self.assertIn("narrow", output.lower())

    def test_empty_and_cancel_are_distinct(self):
        self.assertIsNone(self.pick([], ["empty"])[0])
        for answer in ["cancel", EOFError(), KeyboardInterrupt()]:
            with self.assertRaises(ValueError):
                self.pick([session()], [answer])

    def configure(self, answers, remembered=None, auto_confirm=False):
        item = session()
        snapshot = {
            "layout": [
                {"action": "newTab", "slot": "left"},
                {"action": "splitPane", "slot": "right"},
            ],
            "panes": {
                "left": {"terminal": "t1", "x": 0, "y": 0, "width": 50, "height": 100},
                "right": {"terminal": "t2", "x": 50, "y": 0, "width": 50, "height": 100},
            },
        }
        store = Mock()
        self.last_store = store
        output = io.StringIO()
        with (
            patch.object(setup.REGISTRY, "get", return_value=SimpleNamespace(home="/home/sample")),
            patch("builtins.input", side_effect=answers),
            contextlib.redirect_stdout(output),
        ):
            setup.configure(
                "demo",
                snapshot,
                [item],
                remembered or {},
                SimpleNamespace(slugs=None),
                store,
                replace=True,
                auto_confirm=auto_confirm,
            )
        return store, output.getvalue(), snapshot

    def test_exact_auto_assignment_and_atomic_save(self):
        remembered = {
            "t1": dict(tool="sample", session_id="one", home="/home/sample", secret="discard")
        }
        store, output, snapshot = self.configure(["empty", "yes"], remembered)
        args = store.save_snapshot.call_args
        self.assertEqual(args.args[0:2], ("demo", snapshot["layout"]))
        self.assertEqual(args.args[2]["left"]["session_id"], "one")
        self.assertEqual(
            set(args.args[2]["left"]), {"tool", "session_id", "home", "title", "cwd", "storage"}
        )
        self.assertIsNone(args.args[2]["right"])
        self.assertEqual(args.args[3], {"left": "t1", "right": "t2"})
        self.assertEqual(args.kwargs, {"replace": True})
        self.assertIn("Alpha", output)
        self.assertIn("+", output)
        self.assertIn("sessions workspace restore demo", output)
        self.assertEqual(store.save_snapshot.call_count, 1)

    def test_wrong_home_or_title_does_not_auto_assign(self):
        for entry in [
            dict(tool="sample", session_id="one"),
            dict(tool="sample", session_id="one", home="/different"),
            dict(tool="sample", session_id="unknown", home="/home/sample", title="Alpha"),
        ]:
            store, _, _ = self.configure(["empty", "empty", "yes"], {"t1": entry})
            self.assertEqual(store.save_snapshot.call_args.args[2], {"left": None, "right": None})

    def test_remembered_terminal_keys_are_canonicalized(self):
        entry = dict(tool="sample", session_id="one", home="/home/sample")
        store, _, _ = self.configure(["empty", "yes"], {"{T1}": entry})
        self.assertEqual(store.save_snapshot.call_args.args[2]["left"]["session_id"], "one")

    def test_auto_confirm_skips_final_input_when_all_panes_bound(self):
        item = session()
        snapshot = {
            "layout": [{"action": "newTab", "slot": "main"}],
            "panes": {"main": dict(terminal="t1", x=0, y=0, width=80, height=24)},
        }
        remembered = {"{T1}": dict(tool="sample", session_id="one", home="/home/sample")}
        store = Mock()
        with (
            patch.object(setup.REGISTRY, "get", return_value=SimpleNamespace(home="/home/sample")),
            patch("builtins.input", side_effect=AssertionError("unexpected input")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            setup.configure(
                "demo",
                snapshot,
                [item],
                remembered,
                SimpleNamespace(slugs=None),
                store,
                auto_confirm=True,
            )
        self.assertEqual(store.save_snapshot.call_count, 1)
        self.assertEqual(store.save_snapshot.call_args.args[2]["main"]["session_id"], "one")

    def test_auto_confirm_still_selects_unknown_panes(self):
        store, _, _ = self.configure(["empty", "empty"], auto_confirm=True)
        self.assertEqual(store.save_snapshot.call_args.args[2], {"left": None, "right": None})

    def test_preview_and_unknown_prompts_identify_positions(self):
        _, output, _ = self.configure(["empty", "empty", "yes"])
        self.assertIn("left (left; x=0, y=0, width=50, height=100)", output)
        self.assertIn(
            "Choose a session for pane right (right; x=50, y=0, width=50, height=100)", output
        )
        self.assertNotIn("'action':", output)

    def test_map_fits_skinny_terminal_with_many_panes(self):
        panes = {str(i): dict(x=i, y=0, width=1, height=1) for i in range(11)}
        output = io.StringIO()
        with (
            patch.object(
                setup.shutil, "get_terminal_size", return_value=SimpleNamespace(columns=40)
            ),
            contextlib.redirect_stdout(output),
        ):
            setup._map(panes, {})
        map_lines = [line for line in output.getvalue().splitlines() if line.startswith(("+", "|"))]
        self.assertTrue(map_lines)
        self.assertTrue(all(len(line) <= 39 for line in map_lines))
        self.assertIn("10 (right;", output.getvalue())

    def test_duplicate_native_binding_not_automatically_reused(self):
        entry = dict(tool="sample", session_id="one", home="/home/sample")
        store, _, _ = self.configure(["empty", "yes"], {"t1": entry, "t2": entry})
        self.assertIsNone(store.save_snapshot.call_args.args[2]["right"])

    def test_declined_confirmation_does_not_save(self):
        with self.assertRaises(ValueError):
            self.configure(["empty", "empty", "no"])
        self.last_store.save_snapshot.assert_not_called()

    def test_picker_cancellation_does_not_save(self):
        with self.assertRaises(ValueError):
            self.configure(["cancel"])
        self.last_store.save_snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
