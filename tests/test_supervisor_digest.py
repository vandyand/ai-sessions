import unittest

from ai_sessions.model import ToolCall
from ai_sessions.supervisor.digest import digest_entry, render_digest
from ai_sessions.supervisor.index import Exchange, tokens


def ex(seq, prompt="p", final="f", calls=()):
    return Exchange(seq, "m", prompt, prompt, (final,) if final else (), tuple(calls))


class DigestEntryTests(unittest.TestCase):
    def test_entry_contains_typed_prompt_final_message_tool_counts(self):
        calls = [ToolCall("Bash", f"c{i}", "") for i in range(3)] + [ToolCall("Edit", "e", "")]
        entry = digest_entry(ex(4, "do it", "all done", calls))
        self.assertIn("do it", entry)
        self.assertIn("all done", entry)
        self.assertIn("Bash ×3, Edit ×1", entry)

    def test_entry_lists_each_failed_call_once_and_collapses_repeats(self):
        bad = ToolCall("Bash", "make", "exit 2", failed=True)
        other = ToolCall("Bash", "lint", "exit 1", failed=True)
        entry = digest_entry(ex(1, calls=[bad] * 14 + [other]))
        failed = [line for line in entry.splitlines() if line.startswith("✗")]
        self.assertEqual(len(failed), 2)
        self.assertTrue(failed[0].endswith("×14"))
        self.assertNotIn("×", failed[1])

    def test_entry_clips_typed_prompt_at_6000_and_final_at_2000(self):
        entry = digest_entry(ex(1, "a" * 7000, "b" * 3000))
        self.assertEqual(entry.count("a"), 6000 - 1)
        self.assertEqual(entry.count("b"), 2000 - 1)


class RenderDigestTests(unittest.TestCase):
    def test_under_cap_digest_is_unchanged_concatenation(self):
        items = [ex(1, "one"), ex(2, "two")]
        self.assertEqual(render_digest(items), "\n\n".join(digest_entry(item) for item in items))

    def test_over_cap_shortens_oldest_first_keeping_first_prompt_whole(self):
        items = [ex(i, "p" * 3000, "f" * 1500) for i in range(1, 7)]
        full = sum(tokens(digest_entry(item)) for item in items)
        cap = full - 1500
        out = render_digest(items, cap_tokens=cap)
        self.assertLessEqual(tokens(out), cap)
        self.assertIn("p" * 3000, out)  # exchange 1 untouched
        self.assertNotIn("omitted", out)
        self.assertIn("#2 " + "p" * 499 + "…", out)  # oldest shortened
        self.assertIn("#6 " + "p" * 3000, out)  # newest still whole

    def test_still_over_cap_cuts_middle_with_note(self):
        items = [ex(i, "p" * 3000, "f" * 1500) for i in range(1, 41)]
        items[0] = ex(1, "FIRST " + "q" * 5000)
        cap = 3000
        out = render_digest(items, cap_tokens=cap)
        self.assertLessEqual(tokens(out), cap)
        self.assertRegex(out, r"\[… \d+ exchanges omitted …\]")
        self.assertIn("FIRST " + "q" * 5000, out)
        self.assertIn("#40 ", out)
        self.assertIn("#1 ", out)


if __name__ == "__main__":
    unittest.main()
