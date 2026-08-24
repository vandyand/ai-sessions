import io
import json
import os
import tempfile
import unittest
import uuid
from contextlib import ExitStack, contextmanager, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fake_harness import make_adapter

from ai_sessions import app
from ai_sessions.app import (
    Browser,
    Session,
    UserState,
    available_launch_tools,
    command_for,
    list_output,
    load_sessions,
    publish_name,
    tool_column_width,
)
from ai_sessions.capabilities import HarnessAdapter, Unsupported
from ai_sessions.config import LaunchConfig, ProviderProfile
from ai_sessions.conversion import (
    BridgeError,
    bridge,
    bridged_title,
    complete_jsonl_cursor,
    conversation_change_status,
    native_session_exists,
    read_transcript,
    resolve_budget,
)
from ai_sessions.model import NativeRef, PreparedTarget, SourceKind, Turn
from ai_sessions.registry import REGISTRY


@contextmanager
def isolated_registry(
    root: Path,
    fixture: HarnessAdapter,
    *,
    preserve_discovery: frozenset[str] = frozenset(),
):
    """Keep conversion hooks real while suppressing discovery of user sessions."""
    with ExitStack() as stack:
        for name in REGISTRY.names():
            adapter = REGISTRY.get(name)
            discovery = (
                adapter.discover
                if name in preserve_discovery
                else Unsupported("isolated contract test")
            )
            stack.enter_context(
                REGISTRY.temporary(
                    replace(
                        adapter,
                        home=root / name,
                        discover=discovery,
                        inspect_liveness=Unsupported("isolated contract test"),
                    )
                )
            )
        stack.enter_context(REGISTRY.temporary(fixture))
        yield


class FakeScreen:
    def keypad(self, _enabled: bool) -> None:
        pass


class ThirdHarnessContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = make_adapter(self.root / "fixture")

    def write_fixture(self, *turns: Turn, title: str = "Fixture title") -> tuple[str, Path]:
        writer = self.fixture.write
        assert not isinstance(writer, Unsupported)
        written = writer(
            cwd="/fixture/work",
            turns=list(turns),
            title=title,
            prepared=PreparedTarget(self.fixture.default_command, self.fixture.budget),
        )
        return written.native.session_id, Path(written.native.storage)

    def test_unknown_harness_has_no_budget_fallback(self) -> None:
        with self.assertRaisesRegex(BridgeError, "unknown harness: not-registered"):
            resolve_budget("not-registered")

    def test_late_registered_adapter_participates_in_every_runtime_surface(self) -> None:
        self.assertNotIn("fixture", REGISTRY)
        with isolated_registry(self.root, self.fixture):
            session_id, path = self.write_fixture(
                Turn("user", "independent input"), Turn("assistant", "independent output")
            )
            marker = self.fixture.home / "live" / f"{session_id}.pid"
            marker.parent.mkdir(parents=True)
            marker.write_text(str(os.getpid()), encoding="ascii")

            rows = load_sessions(use_cache=False)
            self.assertEqual(len(rows), 1)
            item = rows[0]
            self.assertEqual(item.tool, "fixture")
            self.assertEqual(item.session_id, session_id)
            self.assertTrue(item.is_open)
            self.assertEqual(item.open_pid, os.getpid())
            self.assertEqual(
                available_launch_tools(item), ("fixture", "codex", "claude", "opencode")
            )
            self.assertTrue(native_session_exists("fixture", session_id))
            self.assertIn("fixture", app.build_parser()._option_string_actions["--tool"].choices)
            self.assertIn("fixture", Browser._pair_layout())
            self.assertEqual(tool_column_width(6), len("FixtureHarness"))

            config = LaunchConfig(path=self.root / "default-config.toml")
            self.assertEqual(
                command_for(item, config),
                ["fixture-cli", "thread", "--id", session_id],
            )
            config = LaunchConfig(
                path=self.root / "config.toml",
                providers={
                    "fixture": ProviderProfile(
                        command=["fixture-custom"], custom_args=["--bespoke"]
                    )
                },
            )
            self.assertEqual(
                command_for(item, config),
                ["fixture-custom", "thread", "--id", session_id],
            )
            config.mode = "dangerous"
            self.assertEqual(
                command_for(item, config),
                ["fixture-custom", "--reckless", "thread", "--id", session_id],
            )
            config.mode = "custom"
            self.assertEqual(
                command_for(item, config),
                ["fixture-custom", "--bespoke", "thread", "--id", session_id],
            )
            config.save()
            reloaded = LaunchConfig.load(config.path)
            self.assertEqual(
                command_for(item, reloaded),
                ["fixture-custom", "--bespoke", "thread", "--id", session_id],
            )

            self.assertEqual(publish_name(item, "Published fixture"), "")
            discovered = load_sessions(use_cache=False)[0]
            self.assertEqual(discovered.title, "Published fixture")
            self.assertTrue(discovered.named)
            missing = replace(discovered, storage=str(self.root / "missing.thread"))
            self.assertEqual(
                publish_name(missing, "must not create"),
                "fixture transcript is missing; name kept local only",
            )
            self.assertFalse(Path(missing.storage).exists())

            _, unnamed_path = self.write_fixture(Turn("user", "fallback title"), title="")
            unnamed = next(
                row for row in load_sessions(use_cache=False) if row.storage == str(unnamed_path)
            )
            self.assertEqual(unnamed.title, "fallback title")
            self.assertFalse(unnamed.named)

            with redirect_stdout(io.StringIO()) as output:
                list_output([discovered])
            self.assertIn("FixtureHarness", output.getvalue())
            budget = resolve_budget("fixture")
            self.assertEqual((budget.tokens, budget.chars), (25_000, 75_000))
            self.assertEqual(budget.origin, "target-default")
            self.assertEqual(
                bridged_title("Portable", "fixture"),
                "Portable (from FixtureHarness)",
            )
            self.assertTrue(path.is_file())
        self.assertNotIn("fixture", REGISTRY)
        self.assertNotIn("fixture", Browser._pair_layout())

    def test_overlapping_native_id_is_not_misattributed_between_discovered_harnesses(self) -> None:
        with isolated_registry(
            self.root,
            self.fixture,
            preserve_discovery=frozenset(("claude",)),
        ):
            claude_home = self.root / "claude"
            claude = REGISTRY.get("claude")
            self.assertEqual(claude.home, claude_home)
            shared_id, fixture_path = self.write_fixture(
                Turn("user", "fixture's own id is metadata")
            )
            referenced_id = str(uuid.uuid4())
            with fixture_path.open("ab") as handle:
                handle.write(
                    (
                        json.dumps(
                            {
                                "kind": "utterance",
                                "actor": "person",
                                "payload": {
                                    "text": (
                                        f"own {shared_id.upper()} and foreign "
                                        f"{referenced_id.upper()}"
                                    )
                                },
                            }
                        )
                        + "\n"
                    ).encode()
                )
            project = claude_home / "projects" / "project"
            project.mkdir(parents=True)
            for native_id in (shared_id, referenced_id):
                (project / f"{native_id}.jsonl").write_text(
                    json.dumps(
                        {
                            "type": "user",
                            "sessionId": native_id,
                            "cwd": "/claude/work",
                            "timestamp": "2026-08-23T00:00:00Z",
                            "message": {"content": "Claude's own id is metadata"},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
            raw = shared_id.encode("ascii")
            self.assertTrue(any(pattern.fullmatch(raw) for pattern in claude.id_patterns))
            self.assertTrue(any(pattern.fullmatch(raw) for pattern in self.fixture.id_patterns))
            rows = load_sessions(use_cache=False)
        by_key = {(item.tool, item.session_id): item for item in rows}
        self.assertEqual(
            set(by_key),
            {("claude", shared_id), ("claude", referenced_id), ("fixture", shared_id)},
        )
        self.assertEqual(by_key[("claude", shared_id)].launch_targets, {"claude": shared_id})
        self.assertEqual(
            by_key[("fixture", shared_id)].launch_targets,
            {"fixture": shared_id, "claude": referenced_id},
        )
        self.assertEqual(by_key[("claude", referenced_id)].launch_targets["fixture"], shared_id)

    def test_browser_initializes_the_late_adapter_color_pair(self) -> None:
        initialized: dict[int, tuple[int, int]] = {}
        with (
            isolated_registry(self.root, self.fixture),
            patch.dict(os.environ, {"TERM": "xterm-256color", "NO_COLOR": ""}, clear=False),
            patch.object(app.curses, "curs_set"),
            patch.object(app.curses, "has_colors", return_value=True),
            patch.object(app.curses, "start_color"),
            patch.object(app.curses, "use_default_colors"),
            patch.object(
                app.curses,
                "init_pair",
                side_effect=lambda number, foreground, background: initialized.__setitem__(
                    number, (foreground, background)
                ),
            ),
            patch.object(app.curses, "COLORS", 256, create=True),
            patch.object(app.curses, "COLOR_PAIRS", 64, create=True),
        ):
            browser = Browser(
                FakeScreen(),
                [],
                UserState(self.root / "state.json"),
                LaunchConfig(path=self.root / "config.toml"),
            )
        self.assertIn(browser.pairs["fixture"], initialized)
        self.assertNotEqual(browser.pairs["fixture"], browser.pairs["codex"])
        self.assertNotEqual(browser.pairs["fixture"], browser.pairs["claude"])
        self.assertNotEqual(
            initialized[browser.pairs["fixture"]], initialized[browser.pairs["codex"]]
        )
        self.assertNotEqual(
            initialized[browser.pairs["fixture"]], initialized[browser.pairs["claude"]]
        )

    def test_bridged_launch_passes_full_source_context_to_third_adapter(self) -> None:
        captured: dict[str, object] = {}

        def resume_args(**values: object) -> list[str]:
            captured.update(values)
            return ["select", str(values["session_id"])]

        fixture = replace(self.fixture, resume_args=resume_args)
        item = Session(
            "codex",
            "child",
            "",
            "",
            0,
            0,
            "",
            False,
            "source",
            source=SourceKind.SUBAGENT,
            resume_id="parent",
            parent_id="parent",
            launch_targets={"codex": "child", "fixture": "fixture-copy"},
            launch_tool="fixture",
        )
        with isolated_registry(self.root, fixture):
            command = command_for(item, LaunchConfig(path=self.root / "config.toml"))
        self.assertEqual(command, ["fixture-cli", "select", "fixture-copy"])
        self.assertEqual(
            captured,
            {
                "session_id": "fixture-copy",
                "source": SourceKind.SUBAGENT,
                "resume_id": "parent",
                "parent_id": "parent",
                "native": False,
            },
        )

    def test_windows_focus_for_open_third_harness_is_explicitly_unsupported(self) -> None:
        item = Session(
            "fixture",
            "open",
            "",
            "",
            0,
            0,
            "",
            False,
            "storage",
            is_open=True,
            open_pid=91,
        )
        with (
            patch.object(app, "IS_WINDOWS", True),
            patch("ai_sessions.platforms.windows.process_exists", return_value=True),
        ):
            focused, note = app.focus_open_session(item)
        self.assertFalse(focused)
        self.assertIn("not supported", note)

    def test_independent_format_round_trips_through_each_builtin_without_pairwise_code(
        self,
    ) -> None:
        with isolated_registry(self.root, self.fixture):
            source_id, source_path = self.write_fixture(
                Turn("user", "portable request"), Turn("assistant", "portable response")
            )
            self.assertEqual(
                [
                    (turn.role, turn.text)
                    for turn in read_transcript(
                        "fixture", NativeRef(source_id, str(source_path))
                    ).turns
                ],
                [("user", "portable request"), ("assistant", "portable response")],
            )
            for target in ("codex", "claude"):
                with self.subTest(target=target):
                    outbound = bridge(
                        source_tool="fixture",
                        target_tool=target,
                        session_id=source_id,
                        storage=str(source_path),
                        cwd="/fixture/work",
                        title="Portable",
                    )
                    native = read_transcript(target, outbound.native)
                    outbound_relevant = [
                        turn
                        for turn in native.turns
                        if "portable request" in turn.text or "portable response" in turn.text
                    ]
                    self.assertEqual(
                        [turn.role for turn in outbound_relevant], ["user", "assistant"]
                    )
                    self.assertTrue(outbound_relevant[0].text.endswith("portable request"))
                    self.assertEqual(outbound_relevant[1].text, "portable response")
                    returned = bridge(
                        source_tool=target,
                        target_tool="fixture",
                        session_id=outbound.session_id,
                        storage=outbound.storage,
                        cwd="/fixture/work",
                        title="Returned",
                    )
                    fixture_copy = read_transcript("fixture", returned.native)
                    returned_relevant = [
                        turn
                        for turn in fixture_copy.turns
                        if "portable request" in turn.text or "portable response" in turn.text
                    ]
                    self.assertEqual(
                        [turn.role for turn in returned_relevant], ["user", "assistant"]
                    )
                    self.assertTrue(returned_relevant[0].text.endswith("portable request"))
                    self.assertEqual(returned_relevant[1].text, "portable response")

    def test_fixture_compaction_window_is_preserved_and_honored_by_bridge(self) -> None:
        with isolated_registry(self.root, self.fixture):
            session_id, path = self.write_fixture(
                Turn("user", "superseded request"),
                Turn("assistant", "superseded response"),
                Turn("user", "fixture summary", compaction=True),
                Turn("assistant", "current response"),
            )
            ref = NativeRef(session_id, str(path))
            latest = read_transcript("fixture", ref, latest_window=True)
            whole = read_transcript("fixture", ref, latest_window=False)
            self.assertEqual(
                [(turn.text, turn.compaction) for turn in latest.turns],
                [("fixture summary", True), ("current response", False)],
            )
            self.assertEqual(len(whole.turns), 4)
            outbound = bridge(
                source_tool="fixture",
                target_tool="codex",
                session_id=session_id,
                storage=str(path),
                cwd="/fixture/work",
            )
            written = read_transcript("codex", outbound.native)
        joined = "\n".join(turn.text for turn in written.turns)
        self.assertIn("fixture summary", joined)
        self.assertIn("current response", joined)
        self.assertNotIn("superseded request", joined)

    def test_fixture_home_rebinding_and_malformed_entries_are_isolated(self) -> None:
        with isolated_registry(self.root, self.fixture):
            moved = self.root / "moved-fixture"
            with REGISTRY.temporary(replace(self.fixture, home=moved)):
                session_id, path = self.write_fixture(Turn("user", "moved home"))
                self.assertEqual(path.parent, moved / "threads")
                self.assertTrue(native_session_exists("fixture", session_id))
                (path.parent / "malformed.thread").write_bytes(b"not-json\n")
                (path.parent / "directory.thread").mkdir()
                rows = load_sessions(use_cache=False)
            self.assertEqual(
                [(row.tool, row.session_id) for row in rows], [("fixture", session_id)]
            )

    def test_same_harness_bridge_is_rejected_by_stable_name(self) -> None:
        with isolated_registry(self.root, self.fixture):
            session_id, path = self.write_fixture(Turn("user", "same harness"))
            with self.assertRaisesRegex(BridgeError, "already belongs"):
                bridge(
                    source_tool="fixture",
                    target_tool="fixture",
                    session_id=session_id,
                    storage=str(path),
                    cwd="/fixture/work",
                )

    def test_tail_status_and_concurrent_append_safety_use_fixture_hook(self) -> None:
        with isolated_registry(self.root, self.fixture):
            session_id, path = self.write_fixture(Turn("user", "before"))
            cursor = complete_jsonl_cursor(path)
            item = Session("fixture", session_id, "", "", 0, 0, "", False, str(path))
            self.assertEqual(publish_name(item, "metadata only"), "")
            self.assertEqual(
                conversation_change_status("fixture", NativeRef(session_id, str(path)), cursor),
                "unchanged",
            )
            with path.open("ab") as handle:
                handle.write(
                    (
                        json.dumps(
                            {
                                "kind": "utterance",
                                "actor": "system",
                                "payload": {"text": "ignored ą metadata"},
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    ).encode("utf-8")
                )
            self.assertEqual(
                conversation_change_status("fixture", NativeRef(session_id, str(path)), cursor),
                "unchanged",
            )
            with path.open("ab") as handle:
                handle.write(
                    (
                        json.dumps(
                            {
                                "kind": "utterance",
                                "actor": "person",
                                "payload": {"text": "after ą"},
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    ).encode("utf-8")
                )
            self.assertEqual(
                conversation_change_status("fixture", NativeRef(session_id, str(path)), cursor),
                "changed",
            )

            unstable_id, unstable_path = self.write_fixture(Turn("user", "stable"))
            unstable_cursor = complete_jsonl_cursor(unstable_path)
            with unstable_path.open("ab") as handle:
                handle.write(b'{"kind":"utterance"')
            self.assertEqual(
                conversation_change_status(
                    "fixture", NativeRef(unstable_id, str(unstable_path)), unstable_cursor
                ),
                "unstable",
            )
            self.assertEqual(
                conversation_change_status(
                    "fixture",
                    NativeRef(unstable_id, str(unstable_path)),
                    unstable_path.stat().st_size + 1,
                ),
                "unstable",
            )
            with self.assertRaisesRegex(
                BridgeError, r"^the source transcript changed or became incomplete"
            ):
                bridge(
                    source_tool="fixture",
                    target_tool="codex",
                    session_id="unstable",
                    storage=str(unstable_path),
                    cwd="/fixture/work",
                )

            racing_id, racing_path = self.write_fixture(Turn("user", "race"))
            reader = self.fixture.read
            assert not isinstance(reader, Unsupported)

            def racing_reader(ref: NativeRef, *, latest_window: bool = True):
                snapshot = reader(ref, latest_window=latest_window)
                with Path(ref.storage).open("ab") as handle:
                    handle.write(
                        (
                            json.dumps(
                                {
                                    "kind": "utterance",
                                    "actor": "person",
                                    "payload": {"text": "concurrent"},
                                }
                            )
                            + "\n"
                        ).encode()
                    )
                return snapshot

            with REGISTRY.temporary(replace(self.fixture, read=racing_reader)):
                with self.assertRaisesRegex(
                    BridgeError, r"^the source transcript changed or became incomplete"
                ):
                    bridge(
                        source_tool="fixture",
                        target_tool="codex",
                        session_id=racing_id,
                        storage=str(racing_path),
                        cwd="/fixture/work",
                    )


if __name__ == "__main__":
    unittest.main()
