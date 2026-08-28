import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from ai_sessions.discovery import HarnessContext
from ai_sessions.harnesses import claude, codex, opencode
from ai_sessions.model import NativeSession, Session, SourceKind


def _json_line(value: dict[str, object]) -> bytes:
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


def _claude_line(
    role: str,
    text: str,
    *,
    meta: bool = False,
    sidechain: bool = False,
    compact: bool = False,
) -> bytes:
    content: object = text
    if text == "tool-only":
        content = [{"type": "tool_use", "id": "tool", "name": "bash", "input": {}}]
    return _json_line(
        {
            "type": role,
            "isMeta": meta,
            "isSidechain": sidechain,
            "isCompactSummary": compact,
            "message": {"content": content},
        }
    )


def _codex_message(role: str, text: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text", "text": text}],
        },
    }


class ModelMetricCompatibilityTests(unittest.TestCase):
    def test_message_count_is_a_two_way_compatibility_fallback(self) -> None:
        old_native = NativeSession("fixture", "old", "", "", 0, 0, "", False, "x", message_count=4)
        old_session = Session("fixture", "old", "", "", 0, 0, "", False, "x", message_count=4)
        new_native = NativeSession("fixture", "new", "", "", 0, 0, "", False, "x", prompt_count=3)
        self.assertEqual((old_native.prompt_count, old_native.message_count), (4, 4))
        self.assertEqual((old_session.prompt_count, old_session.message_count), (4, 4))
        self.assertEqual((new_native.prompt_count, new_native.message_count), (3, 3))
        self.assertEqual(old_session.activity, "0t 0c 4p")


class JsonlMetricTests(unittest.TestCase):
    def test_claude_classifier_and_partial_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            path.write_bytes(
                _claude_line("user", "one")
                + _claude_line("assistant", "answer")
                + _claude_line("assistant", "tool-only")
                + _claude_line("user", "ignored", meta=True)
                + _claude_line("user", "sidechain", sidechain=True)
                + _claude_line("user", "summary", compact=True)
            )
            context = HarnessContext.create(use_cache=False)
            cache = claude.DiscoveryCache(context)
            before, _ = cache.scan(path, count_user_messages=True)
            self.assertEqual(
                (before["turn_count"], before["compaction_count"], before["prompt_count"]),
                (3, 1, 1),
            )
            partial = _claude_line("user", "later")[:-1]
            with path.open("ab") as handle:
                handle.write(partial)
            cache.scan(path, count_user_messages=True)
            self.assertEqual(cache.entries[str(path)]["prompt_count"], 1)
            with path.open("ab") as handle:
                handle.write(b"\n")
            after, _ = claude.DiscoveryCache(context).scan(path, count_user_messages=True)
            self.assertEqual(after["prompt_count"], 2)

    def test_codex_pairs_user_event_and_replays_partial_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_bytes(
                _json_line(_codex_message("user", "one"))
                + _json_line(
                    {"type": "event_msg", "payload": {"type": "user_message", "message": "one"}}
                )
                + _json_line(_codex_message("assistant", "answer"))
            )
            context = HarnessContext.create(use_cache=False)
            cache = codex.DiscoveryCache(context)
            first = cache.scan(path, SourceKind.INTERACTIVE)
            self.assertEqual(first[:3], (2, 0, 1))
            compact = _json_line({"type": "compacted", "payload": {}})
            with path.open("ab") as handle:
                handle.write(compact[:-1])
            partial = cache.scan(path, SourceKind.INTERACTIVE)
            self.assertEqual(partial[:3], (2, 0, 1))
            with path.open("ab") as handle:
                handle.write(b"\n")
            complete = codex.DiscoveryCache(context).scan(path, SourceKind.INTERACTIVE)
            self.assertEqual(complete[:3], (2, 1, 1))

    def test_jsonl_cache_rejects_same_size_rewrite_with_coarse_mtime(self) -> None:
        for module, source, initial, replacement in (
            (claude, None, _claude_line("user", "one"), _claude_line("user", "two")),
            (
                codex,
                SourceKind.INTERACTIVE,
                _json_line(
                    {"type": "event_msg", "payload": {"type": "user_message", "message": "one"}}
                ),
                _json_line(
                    {"type": "event_msg", "payload": {"type": "user_message", "message": "two"}}
                ),
            ),
        ):
            with self.subTest(provider=module.__name__), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "transcript.jsonl"
                path.write_bytes(initial)
                context = HarnessContext.create(use_cache=False)
                cache = module.DiscoveryCache(context)
                if source is None:
                    cache.scan(path, count_user_messages=True)
                else:
                    cache.scan(path, source)
                original_mtime = path.stat().st_mtime_ns
                path.write_bytes(replacement)
                os.utime(path, ns=(original_mtime, original_mtime))
                if source is None:
                    result = module.DiscoveryCache(context).scan(path, count_user_messages=True)
                else:
                    result = module.DiscoveryCache(context).scan(path, source)
                if source is None:
                    self.assertEqual(result[0]["last_prompt"], "two")
                else:
                    self.assertEqual(result[3], "two")

    def test_warm_claude_cache_does_not_reopen_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            path.write_bytes(_claude_line("user", "one"))
            context = HarnessContext.create(use_cache=False)
            cache = claude.DiscoveryCache(context)
            cache.scan(path, count_user_messages=True)
            warm = claude.DiscoveryCache(context)
            warm.entries = cache.entries
            result, _ = warm.scan(path, count_user_messages=True)
            self.assertEqual(result["prompt_count"], 1)


class OpenCodeMetricTests(unittest.TestCase):
    def test_active_semantic_messages_and_completed_compaction(self) -> None:
        schema = """
        CREATE TABLE session (id TEXT, parent_id TEXT, directory TEXT, title TEXT,
            agent TEXT, time_created INTEGER, time_updated INTEGER, time_archived INTEGER,
            revert TEXT);
        CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
        CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "opencode.db"
            connection = sqlite3.connect(database)
            connection.executescript(schema)
            session_id = "ses_000000000001AAAAAAAAAAAAAA"
            connection.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, None, "/work", "title", "", 1, 2, None, None),
            )

            def add(
                message_id: str, role: str, parts: list[dict[str, object]], **info: object
            ) -> None:
                payload = {"role": role, **info}
                connection.execute(
                    "INSERT INTO message VALUES (?, ?, ?, ?)",
                    (message_id, session_id, int(message_id[2:]), json.dumps(payload)),
                )
                for index, part in enumerate(parts):
                    connection.execute(
                        "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
                        (
                            f"part-{message_id}-{index}",
                            message_id,
                            session_id,
                            int(message_id[2:]) + index,
                            json.dumps(part),
                        ),
                    )

            add("m01", "user", [{"type": "text", "id": "p1", "text": "one"}])
            add("m02", "assistant", [{"type": "text", "id": "p2", "text": "answer"}])
            add("m03", "assistant", [{"type": "tool", "id": "p3"}])
            add("m04", "user", [{"type": "text", "id": "p4", "text": "ignored", "ignored": True}])
            add("m05", "user", [{"type": "compaction", "id": "p5", "auto": True}])
            add(
                "m06",
                "assistant",
                [{"type": "text", "id": "p6", "text": "summary"}],
                summary=True,
                finish="stop",
                parentID="m05",
            )
            add("m07", "user", [{"type": "text", "id": "p7", "text": "latest"}])
            connection.commit()
            connection.close()
            command = (sys.executable, "-c", f"print({str(database)!r})")
            context = HarnessContext.create(
                use_cache=False, provider_commands={"opencode": command}
            )
            rows = opencode.discover(context, use_cache=False)
            connection = sqlite3.connect(database)
            connection.execute(
                "UPDATE session SET revert=? WHERE id=?",
                (json.dumps({"messageID": "m07"}), session_id),
            )
            connection.commit()
            connection.close()
            context = HarnessContext.create(
                use_cache=False, provider_commands={"opencode": command}
            )
            reverted = opencode.discover(context, use_cache=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0].turn_count, rows[0].compaction_count, rows[0].prompt_count),
            (4, 1, 2),
        )
        self.assertEqual(rows[0].message_count, rows[0].prompt_count)
        self.assertEqual(
            (reverted[0].turn_count, reverted[0].compaction_count, reverted[0].prompt_count),
            (3, 1, 1),
        )


if __name__ == "__main__":
    unittest.main()
