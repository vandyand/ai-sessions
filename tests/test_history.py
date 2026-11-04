import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_sessions import app
from ai_sessions.app import Session, UserState, history_lines, route_label


def session(tool: str, session_id: str, title: str, created: float = 0.0) -> Session:
    return Session(
        tool, session_id, title, "/work", created, created, "", True, "", provider_title=title
    )


class HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.json"
        self.state = UserState(self.path)

    def conversation(self, *members: tuple[Session, int]) -> str:
        self.state.conversations["conv"] = {
            "members": {
                item.key: {
                    "tool": item.tool,
                    "session_id": item.session_id,
                    "title": item.title,
                    "generation": generation,
                }
                for item, generation in members
            }
        }
        for item, _ in members:
            self.state.session_conversations[item.key] = "conv"
        return "conv"

    def test_old_suffixes_are_turned_into_the_history_before_they_go(self) -> None:
        original = session("codex", "c-1", "top-lego", created=100.0)
        copy = session("claude", "a-1", "top-lego (from Codex) (from Claude) (from Codex)", 400.0)
        self.conversation((original, 0), (copy, 1))
        self.state.apply([original, copy])
        history = self.state.history("conv")
        self.assertEqual([step["tool"] for step in history], ["codex", "claude", "codex", "claude"])
        self.assertEqual((history[0]["session_id"], history[0]["at"]), ("c-1", 100.0))
        self.assertEqual((history[-1]["session_id"], history[-1]["at"]), ("a-1", 400.0))
        self.assertIsNone(history[1]["at"])
        # Captured on disk, so dropping the suffixes later loses nothing.
        self.assertEqual(len(UserState(self.path).history("conv")), 4)

    def test_without_suffixes_the_copies_give_the_history(self) -> None:
        original = session("claude", "a-1", "minecraft", created=100.0)
        copy = session("codex", "c-1", "minecraft", created=200.0)
        self.conversation((original, 0), (copy, 1))
        self.state.apply([original, copy])
        self.assertEqual(
            [(step["tool"], step["at"]) for step in self.state.history("conv")],
            [("claude", 100.0), ("codex", 200.0)],
        )

    def test_each_handoff_to_another_harness_is_recorded_once(self) -> None:
        original = session("codex", "c-1", "work", created=100.0)
        self.conversation((original, 0))
        self.state.apply([original])
        self.state.note_handoff("conv", "claude", "a-1")
        self.state.note_handoff("conv", "claude", "a-1")
        self.state.note_handoff("conv", "codex", "c-1")
        history = UserState(self.path).history("conv")
        self.assertEqual([step["tool"] for step in history], ["codex", "claude", "codex"])
        self.assertTrue(all(step["at"] for step in history))

    def test_route_label_uses_one_letter_per_harness(self) -> None:
        steps = [{"tool": tool} for tool in ("codex", "claude", "codex", "opencode")]
        self.assertEqual(route_label(steps), "C→A→C→O")
        self.assertEqual(route_label(steps[:1]), "")
        self.assertEqual(route_label(steps * 3, 9), "…C→A→C→O")

    def test_details_name_each_harness_in_full_with_its_date(self) -> None:
        lines = history_lines(
            [{"tool": "codex", "at": 1_790_000_000.0}, {"tool": "claude", "at": None}]
        )
        self.assertTrue(lines[0].startswith("History    Codex"))
        self.assertIn(app.exact_time(1_790_000_000.0), lines[0])
        self.assertIn("→ Claude Code", lines[1])
        self.assertEqual(lines[0].index("Codex"), lines[1].index("Claude Code"))
        self.assertEqual(lines[0].index("20"), lines[1].index("date not recorded"))
        self.assertIn("date not recorded", lines[1])
        self.assertEqual(history_lines([{"tool": "codex", "at": 1.0}]), [])


if __name__ == "__main__":
    unittest.main()
