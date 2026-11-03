import json
import tempfile
import unittest
from pathlib import Path

from ai_sessions.harnesses.claude import claude_pastes
from ai_sessions.supervisor import pastes


class PasteTests(unittest.TestCase):
    def test_paired_fences(self):
        text = "see ```AAA``` and ```BBB``` ok"
        spans = pastes.fenced_spans(text)
        self.assertEqual([text[s:e] for s, e in spans], ["AAA", "BBB"])

    def test_unclosed_final_fence_runs_to_end(self):
        text = "look ```log line\nmore"
        spans = pastes.fenced_spans(text)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0][1], len(text))

    def test_language_tagged_fence_is_inner_block(self):
        text = "x ```outer\n```python\nx=1\n```\nmore``` tail"
        spans = pastes.fenced_spans(text)
        self.assertEqual(len(spans), 1)
        start, end = spans[0]
        self.assertTrue(text[start:end].startswith("outer"))
        self.assertTrue(text[start:end].endswith("more"))

    def test_marker_keeps_head_and_tail(self):
        body = "".join(chr(97 + i % 26) for i in range(5000))
        out = pastes.typed_text(f"go ```{body}``` now", pastes=[])
        self.assertIn("pasted 5,000 chars", out)
        self.assertIn(body[:300], out)
        self.assertIn(body[-500:], out)
        self.assertLess(len(out), 1000)
        self.assertTrue(out.startswith("go ") and out.endswith(" now"))

    def test_short_paste_kept_whole(self):
        body = "q" * 600
        out = pastes.typed_text(f"```{body}```", pastes=[])
        self.assertIn(body, out)
        self.assertIn("pasted 600 chars", out)

    def test_forgotten_close_keeps_trailing_typed_words(self):
        text = "```" + "x" * 3000 + " please fix the retry loop"
        out = pastes.typed_text(text, pastes=[])
        self.assertIn("please fix the retry loop", out)

    def test_claude_history_paste_spans(self):
        inline = "inline paste " * 100
        hashed = "hashed paste " * 100
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "paste-cache").mkdir()
            (home / "paste-cache" / "abc123.txt").write_text(hashed, encoding="utf-8")
            entries = [
                {
                    "sessionId": "s1",
                    "display": "x",
                    "pastedContents": {
                        "1": {"type": "text", "content": inline},
                        "2": {"type": "text", "contentHash": "abc123"},
                        "3": {"type": "text", "contentHash": "missing"},
                    },
                },
                {"sessionId": "other", "pastedContents": {"1": {"content": "nope"}}},
            ]
            (home / "history.jsonl").write_text(
                "\n".join(json.dumps(e) for e in entries), encoding="utf-8"
            )
            found = claude_pastes("s1", home)
        self.assertEqual(sorted(found), sorted([inline, hashed]))
        plain = f"a {inline} b {hashed} c"
        fenced = f"a ```{inline}``` b ```{hashed}``` c"
        self.assertEqual(len(pastes.content_spans(plain, found)), 2)
        self.assertEqual(
            pastes.typed_text(plain, pastes=found),
            pastes.typed_text(fenced, pastes=found),
        )

    def test_no_history_match_falls_back_to_fences(self):
        body = "z" * 900
        out = pastes.typed_text(f"hi ```{body}``` bye", pastes=["unrelated"])
        self.assertIn("pasted 900 chars", out)
        self.assertTrue(out.startswith("hi ") and out.endswith(" bye"))


if __name__ == "__main__":
    unittest.main()
