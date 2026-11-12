import contextlib
import io
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_sessions.model import Session
from ai_sessions.workspaces import setup


def session(ident="one", title="Alpha"):
    return Session(
        "sample", ident, title, "/work", 0, 0, "", True, "/storage", slug="slug-" + ident
    )


class SetupTests(unittest.TestCase):
    def test_splitter_gaps_do_not_expand_into_fake_rows(self):
        panes = {
            "pane-0": dict(x=0, y=0, width=300, height=300),
            "pane-1": dict(x=304, y=0, width=300, height=148),
            "pane-2": dict(x=304, y=152, width=300, height=148),
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            setup._map(panes, {})
        drawing = [line for line in output.getvalue().splitlines() if line.startswith(("+", "|"))]
        self.assertEqual(len(drawing), 5)
        self.assertNotIn("pane-", "\n".join(drawing))

    def test_large_history_starts_with_search_only(self):
        items = [session(str(i), "Unrelated history " + str(i)) for i in range(6726)]
        output = io.StringIO()

        def answer(prompt):
            self.assertIn("search", prompt.lower())
            self.assertIn("6726", output.getvalue())
            self.assertNotIn("Unrelated history", output.getvalue())
            return "cancel"

        with contextlib.redirect_stdout(output), self.assertRaises(ValueError):
            setup.choose_session(items, input_fn=answer)

    def test_searched_matches_prioritize_running_then_recent(self):
        old = session("old", "Project old")
        recent = replace(session("recent", "Project recent"), updated=100)
        running = replace(session("running", "Project running"), is_open=True)
        selected, output = self.pick([old, recent, running], ["Project", "1"])
        self.assertIs(selected, running)
        searched = output.split("3. Project running", 1)[1]
        self.assertLess(searched.index("Project running"), searched.index("Project recent"))
        self.assertLess(searched.index("Project recent"), searched.index("Project old"))

    def test_unknown_and_explicit_empty_have_distinct_legends(self):
        _, output, _ = self.configure(["empty", "empty", "yes"])
        before, after = output.split("Workspace demo preview:")
        self.assertIn("unidentified", before.lower())
        self.assertNotIn("empty shell", before.lower())
        self.assertIn("could not identify", before.lower())
        self.assertIn("cancel", before.lower())
        self.assertIn("explicitly empty", after.lower())

    def test_map_uses_slots_only_and_full_legend(self):
        panes = {str(i): dict(x=i % 3, y=i // 3, width=1, height=1) for i in range(12)}
        title = "A full session title that belongs in the legend"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            setup._map(panes, {"0": dict(title=title, tool="sample", session_id="one")})
        drawing, legend = output.getvalue().split("0 (", 1)
        self.assertNotIn("A full", drawing)
        self.assertNotIn("unassigned", drawing)
        self.assertIn(title, legend)
        self.assertLessEqual(len(drawing.splitlines()), 14)

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
