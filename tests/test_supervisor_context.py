import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import test_bridge as tb  # noqa: E402

from ai_sessions.conversion import BridgeError
from ai_sessions.model import NativeRef, ReadSnapshot, Transcript, Turn
from ai_sessions.supervisor import context, index
from ai_sessions.supervisor.context import (
    INLINE_LIMIT,
    build_brief,
    build_update,
    prepare_context,
)
from ai_sessions.supervisor.index import Exchange, TargetIndex
from ai_sessions.supervisor.store import HookState

KEY = "codex:t1"


class FakeState:
    """Just enough of UserState for prepare_context."""

    def __init__(self, status, heads, record=None):
        self.supervisors = {"r1": record or {"target_conversation": "c1", "title": "T"}}
        self._result = (status, heads)

    def conversation_head(self, conversation_id):
        return self._result


def snapshot(turns, checkpoint):
    return ReadSnapshot(Transcript(turns), checkpoint)


class ContextCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.folder = self.root / "r1"
        self.folder.mkdir()
        self.transcript = self.root / "target.jsonl"
        self.transcript.write_text("{}\n", encoding="utf-8")
        self.member = {
            "tool": "codex",
            "session_id": "t1",
            "storage": str(self.transcript),
            "cwd": "/work",
            "title": "Target title",
            "generation": 0,
            "checkpoint": 10,
        }
        self.index = TargetIndex(self.folder / "index.jsonl")

    def state(self, status="ok", heads=None):
        return FakeState(status, [(KEY, self.member)] if heads is None else heads)

    def seed(self, count, *, size=10):
        self.index.append(
            [
                Exchange(n, KEY, f"prompt {n}", f"prompt {n}", ("a" * size,))
                for n in range(1, count + 1)
            ]
        )

    def hook(self, **kw):
        values = dict(
            record_id="r1",
            member=KEY,
            checkpoint=10,
            last_seq_sent=1,
            brief_due=False,
            updated=1_000.0,
        )
        values.update(kw)
        return HookState(**values)

    def prepare(self, state, hook):
        return prepare_context("r1", state=state, hook=hook, root=self.root)

    def since(self, turns, checkpoint=20):
        return patch.object(
            index.conversion, "read_since", return_value=snapshot(turns, checkpoint)
        )


class BriefTests(ContextCase):
    def brief(self, hook=None):
        return build_brief(
            {"title": "T"},
            hook or self.hook(),
            self.index,
            target_title="Target title",
            target_tool="codex",
            cwd="/work",
            liveness="last target activity 3h 12m ago",
            root=self.root,
        )

    def test_first_prompt_gets_brief_with_all_sections_in_order(self):
        self.seed(3)
        (self.folder / "ledger.md").write_text("LEDGER BODY", encoding="utf-8")
        with self.since([]):
            text, new = self.prepare(self.state(), self.hook(brief_due=True, last_seq_sent=-1))
        self.assertEqual(new.briefs, 1)
        self.assertFalse(new.brief_due)
        self.assertEqual(new.last_seq_sent, 3)
        marks = [
            "# Supervisor brief",
            "Target title",
            "last target activity",
            "## Starting objective",
            "First prompt: prompt 1",
            "## Digest of earlier activity",
            "## Recent activity (cleaned)",
            "## Ledger (",
            "LEDGER BODY",
            "## Standing instructions",
            "sessions --target-log r1",
        ]
        positions = [text.index(m) for m in marks]
        self.assertEqual(positions, sorted(positions))
        self.assertIn(str((self.folder / "ledger.md").resolve()), text)

    def test_missing_ledger_is_noted(self):
        self.seed(1)
        self.assertIn("(no ledger yet)", self.brief())

    def test_brief_tail_and_digest_partition_exchanges_without_overlap(self):
        self.seed(5, size=70_000)
        text = self.brief()
        digest = text.split("## Digest of earlier activity\n")[1].split("\n## ")[0]
        tail = text.split("## Recent activity (cleaned)\n")[1].split("\n## Ledger")[0]
        in_digest = {int(n) for n in re.findall(r"^#(\d+) ", digest, re.M)}
        in_tail = {int(n) for n in re.findall(r"^#(\d+)$", tail, re.M)}
        self.assertEqual(in_digest | in_tail, {1, 2, 3, 4, 5})
        self.assertEqual(in_digest & in_tail, set())
        self.assertEqual(in_tail, {4, 5})

    def test_long_brief_is_delivered_by_file(self):
        self.seed(1, size=20_000)
        with self.since([]):
            text, new = self.prepare(self.state(), self.hook(brief_due=True, last_seq_sent=-1))
        self.assertLess(len(text), 1_000)
        path = Path(re.search(r"(\S*brief-1\.md)", text).group(1))
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.parent, self.folder.resolve())
        body = path.read_text(encoding="utf-8")
        self.assertGreater(len(body), INLINE_LIMIT)
        self.assertIn("## Standing instructions", body)
        self.assertEqual(new.briefs, 1)

    def test_old_delivery_files_are_pruned_to_five(self):
        for number in range(1, 9):
            context.deliver("brief", "x" * (INLINE_LIMIT + 1), self.folder)
        names = sorted(p.name for p in self.folder.glob("brief-*.md"))
        self.assertEqual(names, [f"brief-{n}.md" for n in range(4, 9)])


class UpdateTests(ContextCase):
    def test_update_contains_only_new_exchanges(self):
        self.seed(1)
        turns = [Turn("user", "second prompt"), Turn("assistant", "second answer")]
        with self.since(turns):
            text, new = self.prepare(self.state(), self.hook())
        self.assertIn("second prompt", text)
        self.assertNotIn("prompt 1", text)
        self.assertEqual((new.last_seq_sent, new.checkpoint, new.briefs), (2, 20, 0))

    def test_short_update_is_inline(self):
        self.seed(1)
        with self.since([Turn("user", "go"), Turn("assistant", "b" * 500)]):
            text, _ = self.prepare(self.state(), self.hook())
        self.assertLess(len(text), INLINE_LIMIT)
        self.assertIn("b" * 500, text)
        self.assertEqual(list(self.folder.glob("update-*.md")), [])

    def test_open_exchange_is_resent_when_it_grew(self):
        self.seed(1)
        with self.since([Turn("assistant", "late finish")]):
            text, new = self.prepare(self.state(), self.hook())
        self.assertIn("late finish", text)
        self.assertIn("prompt 1", text)
        self.assertEqual(new.last_seq_sent, 1)

    def test_nothing_new_line(self):
        self.seed(1)
        with self.since([]):
            text, new = self.prepare(self.state(), self.hook())
        self.assertRegex(text, r"no target activity since")
        self.assertEqual(new.updated, 1_000.0)

    def test_long_gap_sends_brief_instead_of_update(self):
        self.seed(1)
        turns = [Turn("user", "huge"), Turn("assistant", "z" * 200_000)]
        with self.since(turns):
            text, new = self.prepare(self.state(), self.hook())
        self.assertEqual(new.briefs, 1)
        self.assertIn("brief-1.md", text)
        body = (self.folder / "brief-1.md").read_text(encoding="utf-8")
        self.assertIn("# Supervisor brief", body)

    def test_unlocatable_checkpoint_rebriefs(self):
        self.seed(2)
        fresh = snapshot([Turn("user", "rewritten start"), Turn("assistant", "ok")], 7)
        with (
            patch.object(index.conversion, "read_since", return_value=None),
            patch.object(index.conversion, "read_snapshot", return_value=fresh),
        ):
            text, new = self.prepare(self.state(), self.hook())
        self.assertEqual((new.briefs, new.checkpoint, new.last_seq_sent), (1, 7, 1))
        self.assertIn("rewritten", text)
        self.assertIn("# Supervisor brief", text)
        self.assertEqual([e.prompt for e in self.index.exchanges()], ["rewritten start"])

    def test_unavailable_target_reports_and_keeps_checkpoint(self):
        self.seed(1)
        hook = self.hook()
        text, new = self.prepare(self.state("unavailable", []), hook)
        self.assertIn("target unavailable since", text)
        self.assertEqual(new, hook)
        with patch.object(index.conversion, "read_since", side_effect=BridgeError("gone")):
            text, new = self.prepare(self.state(), hook)
        self.assertIn("target unavailable since", text)
        self.assertEqual(new, hook)

    def test_diverged_injects_warning_and_no_activity(self):
        self.seed(1)
        other = dict(self.member, tool="claude", session_id="t2", title="Other copy")
        heads = [(KEY, self.member), ("claude:t2", other)]
        hook = self.hook()
        with self.since([Turn("user", "must not appear")]):
            text, new = self.prepare(self.state("diverged", heads), hook)
        self.assertIn("diverged", text)
        self.assertIn("Other copy", text)
        self.assertIn("Target title", text)
        self.assertNotIn("must not appear", text)
        self.assertEqual(new, hook)

    def test_errors_become_one_warning_line(self):
        hook = self.hook()
        text, new = prepare_context("r1", state=object(), hook=hook, root=self.root)
        self.assertTrue(text.startswith("⚠ supervisor:"))
        self.assertNotIn("\n", text)
        self.assertEqual(new, hook)

    def test_build_update_header_has_liveness(self):
        self.seed(2)
        text = build_update(
            {},
            self.hook(),
            self.index,
            target_title="T",
            target_tool="codex",
            cwd="/w",
            liveness="last target activity 5m ago",
        )
        self.assertIn("last target activity 5m ago", text)
        self.assertIn("prompt 2", text)
        self.assertNotIn("prompt 1", text)


class HeadMovedTests(unittest.TestCase):
    def test_head_moved_notes_new_harness_and_continues_from_member_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            from test_supervisor_store import _linked_state

            state, source, copy, conversation_id = _linked_state(directory)
            root = Path(directory) / "supervisors"
            folder = root / "r1"
            idx = TargetIndex(folder / "index.jsonl")
            state.apply([source, copy])
            checkpoint = idx.build(
                "codex", NativeRef(source.session_id, source.storage), source.key
            )
            self.assertTrue(idx.exchanges())
            hook = HookState(
                "r1",
                member=source.key,
                checkpoint=checkpoint,
                brief_due=False,
                last_seq_sent=idx.last_seq(),
                updated=1_000.0,
            )
            state.supervisors["r1"] = {"target_conversation": conversation_id, "title": "T"}
            with Path(copy.storage).open("a", encoding="utf-8") as handle:
                handle.write(tb.claude_line("user", "Appended in Claude") + "\n")
            text, new = prepare_context("r1", state=state, hook=hook, root=root)
            self.assertIn("Claude", text)
            self.assertIn("Appended in Claude", text)
            self.assertNotIn("Ship it", text)
            self.assertEqual(new.member, copy.key)
            self.assertEqual(new.last_seq_sent, hook.last_seq_sent + 1)

    def test_diverged_with_real_members(self):
        with tempfile.TemporaryDirectory() as directory:
            from test_supervisor_store import _linked_state

            state, source, copy, conversation_id = _linked_state(directory)
            with Path(source.storage).open("a", encoding="utf-8") as handle:
                handle.write(tb.codex_line("user", "Codex branch") + "\n")
            with Path(copy.storage).open("a", encoding="utf-8") as handle:
                handle.write(tb.claude_line("user", "Claude branch") + "\n")
            state.apply([source, copy])
            state.supervisors["r1"] = {"target_conversation": conversation_id}
            text, _ = prepare_context(
                "r1", state=state, hook=HookState("r1"), root=Path(directory) / "s"
            )
            self.assertIn("diverged", text)
            self.assertNotIn("Codex branch", text)


if __name__ == "__main__":
    unittest.main()
