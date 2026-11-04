"""What keeps a refresh fast: fewer reads, smaller labels and one walk of each tree."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sessions.app import session_from_native
from ai_sessions.compression import ZST_SUFFIX
from ai_sessions.discovery import (
    PROMPT_LABEL_CHARS,
    HarnessContext,
    clean_prompt,
    prefetch_stats,
    stat_key,
    trim_labels,
)
from ai_sessions.harnesses import claude, codex
from ai_sessions.model import NativeSession, SourceKind

try:
    from test_activity_metrics import _claude_line, _json_line
except ImportError:  # run as tests.test_discovery_speed
    from tests.test_activity_metrics import _claude_line, _json_line

DAY_NS = 86_400 * 1_000_000_000


def codex_line(text: str) -> bytes:
    return _json_line({"type": "event_msg", "payload": {"type": "user_message", "message": text}})


class SettledTranscriptTests(unittest.TestCase):
    """An unchanged transcript last written long before it was cached is not reread."""

    def scan(self, module, path: Path, cache) -> None:
        if module is claude:
            cache.scan(path, count_user_messages=True)
        else:
            cache.scan(path, SourceKind.INTERACTIVE)

    def cases(self):
        return ((claude, _claude_line("user", "one")), (codex, codex_line("one")))

    def test_a_settled_transcript_is_not_sampled_again(self) -> None:
        for module, line in self.cases():
            with self.subTest(provider=module.__name__), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "transcript.jsonl"
                path.write_bytes(line)
                old = time.time_ns() - DAY_NS
                os.utime(path, ns=(old, old))
                context = HarnessContext.create(use_cache=False)
                first = module.DiscoveryCache(context)
                self.scan(module, path, first)
                warm = module.DiscoveryCache(context)
                warm.entries = first.entries
                with patch.object(module, "_cached_prefix_identity") as sampled:
                    self.scan(module, path, warm)
                sampled.assert_not_called()

    def test_a_transcript_written_just_before_it_was_cached_is_still_sampled(self) -> None:
        # The coarse-clock case: a same-size rewrite in the same tick must still be caught.
        for module, line in self.cases():
            with self.subTest(provider=module.__name__), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "transcript.jsonl"
                path.write_bytes(line)
                context = HarnessContext.create(use_cache=False)
                first = module.DiscoveryCache(context)
                self.scan(module, path, first)
                warm = module.DiscoveryCache(context)
                warm.entries = first.entries
                with patch.object(
                    module, "_cached_prefix_identity", wraps=module._cached_prefix_identity
                ) as sampled:
                    self.scan(module, path, warm)
                sampled.assert_called()

    def test_a_settled_transcript_that_changed_is_read_again(self) -> None:
        for module, line, longer in (
            (claude, _claude_line("user", "one"), _claude_line("user", "three")),
            (codex, codex_line("one"), codex_line("three")),
        ):
            with self.subTest(provider=module.__name__), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "transcript.jsonl"
                path.write_bytes(line)
                old = time.time_ns() - DAY_NS
                os.utime(path, ns=(old, old))
                context = HarnessContext.create(use_cache=False)
                first = module.DiscoveryCache(context)
                self.scan(module, path, first)
                path.write_bytes(longer)
                warm = module.DiscoveryCache(context)
                warm.entries = first.entries
                if module is claude:
                    meta, _ = warm.scan(path, count_user_messages=True)
                    self.assertEqual(meta["last_prompt"], "three")
                else:
                    self.assertEqual(warm.scan(path, SourceKind.INTERACTIVE)[3], "three")


class LabelTests(unittest.TestCase):
    def test_a_prompt_label_is_bounded(self) -> None:
        self.assertEqual(len(clean_prompt("word " * 100_000)), PROMPT_LABEL_CHARS)
        self.assertEqual(clean_prompt("  short   prompt "), "short prompt")

    def test_scaffolding_before_a_long_prompt_is_still_removed(self) -> None:
        text = "<environment_context>cwd</environment_context> real prompt " + "x" * 200_000
        self.assertTrue(clean_prompt(text).startswith("real prompt"))

    def native(self, title: str, named: bool) -> NativeSession:
        return NativeSession(
            tool="codex",
            session_id="id",
            title=title,
            cwd="/work",
            updated=1.0,
            created=1.0,
            preview="p" * 50_000,
            named=named,
            storage="",
            source=SourceKind.INTERACTIVE,
        )

    def test_a_title_made_from_a_prompt_is_bounded_but_a_harness_name_is_kept_whole(self) -> None:
        generated = session_from_native(self.native("t" * 50_000, named=False))
        self.assertEqual(len(generated.title), PROMPT_LABEL_CHARS)
        self.assertEqual(generated.provider_title, generated.title)
        self.assertEqual(len(generated.preview), PROMPT_LABEL_CHARS)
        named = session_from_native(self.native("n" * 5_000, named=True))
        self.assertEqual(len(named.title), 5_000)
        self.assertEqual(named.provider_title, named.title)

    def test_labels_cached_before_the_bound_are_cut_once(self) -> None:
        entries = {"a": {"last_prompt": "x" * 5_000, "other": "y" * 5_000}, "b": {}}
        self.assertTrue(trim_labels(entries, ("last_prompt",)))
        self.assertEqual(len(entries["a"]["last_prompt"]), PROMPT_LABEL_CHARS)
        self.assertEqual(len(entries["a"]["other"]), 5_000)
        self.assertFalse(trim_labels(entries, ("last_prompt",)))


class ListingTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "sessions"
        day = self.root / "2026" / "11" / "03"
        day.mkdir(parents=True)
        self.plain = day / "rollout-2026-11-03T00-00-00-aaaa.jsonl"
        self.only_compressed = day / "rollout-2026-11-03T00-00-00-bbbb.jsonl"
        self.plain.write_text("{}\n", encoding="utf-8")
        Path(str(self.plain) + ZST_SUFFIX).write_bytes(b"leftover")
        Path(str(self.only_compressed) + ZST_SUFFIX).write_bytes(b"z")
        (day / "notes.txt").write_text("not a rollout", encoding="utf-8")

    def test_one_walk_finds_each_rollout_and_prefers_the_plain_file(self) -> None:
        listing = codex.RolloutListing(self.root)
        self.assertEqual(len(listing.paths), 3)
        with patch.object(Path, "is_file", side_effect=AssertionError("stat")):
            self.assertEqual(listing.resolve(self.plain), self.plain)
            self.assertEqual(
                listing.resolve(self.only_compressed),
                Path(str(self.only_compressed) + ZST_SUFFIX),
            )
            self.assertIsNone(listing.resolve(self.root / "2026" / "rollout-x-cccc.jsonl"))

    def test_a_rollout_outside_the_tree_is_checked_on_disk(self) -> None:
        outside = self.root.parent / "archived_sessions" / "rollout-2026-dddd.jsonl"
        outside.parent.mkdir()
        outside.write_text("{}\n", encoding="utf-8")
        self.assertEqual(codex.RolloutListing(self.root).resolve(outside), outside)

    def test_stats_taken_together_match_one_at_a_time(self) -> None:
        paths = [self.plain, self.only_compressed] * 40
        stats = prefetch_stats(paths)
        self.assertEqual(stats[stat_key(self.plain)].st_size, self.plain.stat().st_size)
        self.assertNotIn(stat_key(self.only_compressed), stats)


if __name__ == "__main__":
    unittest.main()
