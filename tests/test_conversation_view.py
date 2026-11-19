"""Conversation preview contracts, using a fictional native adapter."""

import curses
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fake_harness import make_adapter

from ai_sessions import conversation_view as view
from ai_sessions.compression import compress_file
from ai_sessions.model import PreparedTarget, Session, Turn
from ai_sessions.registry import REGISTRY


class Screen:
    def __init__(self, *keys, width=40, height=18, delay=75):
        self.keys = iter(keys)
        self.width, self.height, self.delay = width, height, delay
        self.frames = []
        self.rows = {}

    def getmaxyx(self):
        return self.height, self.width

    def getdelay(self):
        return self.delay

    def timeout(self, value):
        self.delay = value

    def erase(self):
        self.rows = {}

    def addstr(self, y, x, text):
        assert 0 <= y < self.height
        assert x == 0
        assert view.cell_width(text) < self.width
        self.rows[y] = text

    def refresh(self):
        self.frames.append("\n".join(self.rows.get(y, "") for y in range(self.height)))

    def get_wch(self):
        key = next(self.keys)
        if callable(key):
            return key(self)
        return key


def session(storage="fiction.thread"):
    return Session("fixture", "original-id", "A conversation", "/work", 1, 1, "", True, storage)


def test_wrap_prefers_word_boundaries_without_losing_spaces():
    assert view.wrap("word word", 6) == ["word ", "word"]


@pytest.mark.parametrize("tool", ["claude", "codex"])
@pytest.mark.parametrize("compressed", [False, True])
def test_real_provider_readers_keep_complete_messages_without_launching(
    tmp_path, monkeypatch, tool, compressed
):
    text = "Complete message " * 1500
    records = []
    for role in ("user", "assistant"):
        if tool == "claude":
            records.append(
                {
                    "type": role,
                    "sessionId": "original-id",
                    "message": {"role": role, "content": text + role},
                }
            )
        else:
            records.append(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": role,
                        "content": [
                            {
                                "type": "input_text" if role == "user" else "output_text",
                                "text": text + role,
                            }
                        ],
                    },
                }
            )
    source = tmp_path / "original-id.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    if compressed:
        compress_file(source)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    def forbidden(*args, **kwargs):
        raise AssertionError("Preview attempted a native process")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    item = replace(session(str(source)), tool=tool)
    messages, _ = view.load_messages(item)
    assert [(turn.role, turn.text) for turn in messages] == [
        (role, text + role) for role in ("user", "assistant")
    ]
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize("width", [39, 59])
def test_full_wrapping_preserves_long_message_and_blank_lines(width):
    text = "A" * 10000 + "\n\nlast line with spaces  "
    lines = view.wrap(text, width)
    assert "".join(lines) == text.replace("\n", "")
    assert "" in lines
    assert all(view.cell_width(line) <= width for line in lines)


def test_terminal_controls_are_visible_and_wide_characters_fit():
    lines = view.wrap("\x1b[31mred\x07\rX\u202e\t界界界e\u0301", 7)
    assert not any(c in "".join(lines) for c in ("\x1b", "\x07", "\r", "\u202e", "\t"))
    assert "red" in "".join(lines)
    assert all(view.cell_width(line) <= 7 for line in lines)


def test_latest_selection_navigation_and_latest_role_jumps(monkeypatch):
    turns = [
        Turn("user", "first question"),
        Turn("assistant", "answer"),
        Turn("assistant", "latest response"),
        Turn("tool", "hidden"),
    ]
    monkeypatch.setattr(view, "load_messages", lambda _: (turns[:3], ""))
    screen = Screen("p", "p", "n", "u", "a", "q")
    assert view.run(screen, session()) is None
    assert "latest response" in screen.frames[1]
    assert "answer" in screen.frames[2]
    assert "first question" in screen.frames[3]
    assert "first question" in screen.frames[5]
    assert "latest response" in screen.frames[6]
    assert screen.delay == 75


@pytest.mark.parametrize("width", [40, 60])
def test_scroll_resize_and_footer_never_truncated(monkeypatch, width):
    monkeypatch.setattr(
        view,
        "load_messages",
        lambda _: ([Turn("assistant", "\n".join(f"message line {n}" for n in range(100)))], ""),
    )

    def resize(screen):
        screen.width = 40 if width == 60 else 60
        return curses.KEY_RESIZE

    screen = Screen(
        curses.KEY_DOWN,
        curses.KEY_NPAGE,
        curses.KEY_END,
        resize,
        curses.KEY_HOME,
        curses.KEY_UP,
        curses.KEY_PPAGE,
        "\x1b",
        width=width,
    )
    view.run(screen, session())
    assert "message line 99" in screen.frames[4]
    assert "message line 0\n" in screen.frames[6]
    for frame in screen.frames:
        if "Loading" in frame:
            continue
        for label in (
            "m messages",
            "p older",
            "n newer",
            "Up/Down",
            "PgUp/PgDn",
            "Home/End",
            "q/Esc",
            "u user",
            "a assistant",
        ):
            assert label in frame


@pytest.mark.parametrize("error", [OSError("missing"), ValueError("invalid"), RuntimeError("bad")])
def test_read_errors_return_to_menu_and_restore_blocking(monkeypatch, error):
    def fail(_):
        raise error

    monkeypatch.setattr(view, "load_messages", fail)
    screen = Screen("q", delay=-1)
    view.run(screen, session())
    assert "unavailable" in screen.frames[-1]
    assert screen.delay == -1


def test_restore_timeout_when_drawing_fails(monkeypatch):
    monkeypatch.setattr(view, "load_messages", lambda _: ([], ""))
    screen = Screen("q", delay=900)

    def fail():
        raise RuntimeError("draw failed")

    screen.refresh = fail
    with pytest.raises(RuntimeError):
        view.run(screen, session())
    assert screen.delay == 900


@pytest.mark.parametrize("compressed", [False, True])
def test_native_adapter_full_text_read_only_integration(tmp_path, compressed):
    adapter = make_adapter(tmp_path / "fixture")
    reads = []
    original_read = adapter.read

    def tracked_read(ref, **kwargs):
        reads.append(ref)
        return original_read(ref, **kwargs)

    adapter = replace(adapter, read=tracked_read)
    with REGISTRY.temporary(adapter):
        written = adapter.write(
            cwd="/work",
            title="Test",
            prepared=PreparedTarget(adapter.default_command, adapter.budget),
            turns=[Turn("user", "question" * 1000), Turn("assistant", "answer" * 1000)],
        )
        path = Path(written.native.storage)
        if compressed:
            path = compress_file(path)
        item = replace(
            session(str(path)), session_id=written.native.session_id, compressed=compressed
        )
        before = path.read_bytes(), path.stat().st_mtime_ns, repr(item)
        messages, notice = view.load_messages(item)
        assert [t.text for t in messages] == ["question" * 1000, "answer" * 1000]
        assert not notice
        assert (path.read_bytes(), path.stat().st_mtime_ns, repr(item)) == before
        if compressed:
            assert not Path(written.native.storage).exists()
            assert reads[-1].session_id == item.session_id
            assert reads[-1].storage != item.storage
            assert not Path(reads[-1].storage).exists()


def test_cap_keeps_full_latest_user_even_after_many_assistant_messages(monkeypatch):
    from ai_sessions.model import Transcript

    turns = [Turn("user", "full question" * 1000)] + [Turn("assistant", str(n)) for n in range(500)]
    monkeypatch.setattr(view.conversion, "read_transcript", lambda *a, **k: Transcript(turns))
    messages, notice = view.load_messages(session())
    assert len(messages) == view.MAX_MESSAGES
    assert messages[0].text == turns[0].text
    assert messages[-1].text == "499"
    assert "501" in notice


def test_temporary_decompression_limit_is_explicit_and_cleans_up(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path / "fixture")
    path = tmp_path / "huge.thread"
    path.write_bytes(b"x" * 100)
    packed = compress_file(path)
    monkeypatch.setattr(view, "MAX_TEMP_BYTES", 20)
    with REGISTRY.temporary(adapter), pytest.raises(Exception, match="limit"):
        view.load_messages(session(str(packed)))
    assert packed.exists()
    assert not path.exists()


def test_empty_and_tiny_terminal_still_allow_exit(monkeypatch):
    monkeypatch.setattr(view, "load_messages", lambda _: ([], ""))
    normal = Screen("q")
    view.run(normal, session())
    assert "No conversational messages" in normal.frames[-1]
    tiny = Screen("\x1b", width=5, height=2)
    view.run(tiny, session())
    assert tiny.delay == 75


def test_compressed_parse_failure_cleans_private_file(tmp_path):
    seen = []

    def fail(ref, **kwargs):
        seen.append(Path(ref.storage))
        assert seen[-1].is_file()
        assert ref.session_id == "original-id"
        raise ValueError("corrupt native transcript")

    adapter = replace(make_adapter(tmp_path / "fixture"), read=fail)
    path = tmp_path / "broken.thread"
    path.write_bytes(b"broken\n")
    packed = compress_file(path)
    before = packed.read_bytes()
    with REGISTRY.temporary(adapter), pytest.raises(ValueError):
        view.load_messages(session(str(packed)))
    assert seen and not seen[0].exists()
    assert packed.read_bytes() == before
    assert not path.exists()


def test_storage_limit_error_is_visible_and_scrollable(monkeypatch):
    def fail(_):
        raise view.conversion.BridgeError("Preview exceeds temporary storage limit")

    monkeypatch.setattr(view, "load_messages", fail)
    screen = Screen("q", width=40)
    view.run(screen, session())
    assert "storage limit" in screen.frames[-1]


def test_missing_native_storage_is_unavailable_without_creating_files(tmp_path):
    adapter = make_adapter(tmp_path / "fixture")
    missing = tmp_path / "missing.thread"
    with REGISTRY.temporary(adapter):
        with pytest.raises(view.conversion.BridgeError, match="unavailable"):
            view.load_messages(session(str(missing)))
    assert not missing.exists()


def test_navigation_never_rereads_or_launches(monkeypatch):
    reads = []

    def load(item):
        reads.append(item)
        return [Turn("assistant", "older"), Turn("user", "latest question")], ""

    monkeypatch.setattr(view, "load_messages", load)
    screen = Screen("p", "n", "p", "a", "u", "q")
    item = session()
    view.run(screen, item)
    assert reads == [item]
    assert "latest question" in screen.frames[1]
