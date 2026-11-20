"""Read-only desk viewer behavior, without a real terminal."""

import curses
from datetime import datetime, timezone

import pytest

from ai_sessions.desk import view

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


def snapshot(**changes):
    value = dict(
        name="Garden",
        cwd="fictional",
        revision=1,
        sources=[],
        review="Plants are ready.",
        decisions=[
            dict(
                key="secret-a",
                question="Choose a delivery day?",
                context="Rain expected.",
                recommendation="Use Friday.",
                status="pending",
                evidence=[],
            ),
            dict(
                key="secret-b",
                question="Buy pots?",
                context="Stock is low.",
                recommendation="Buy ten.",
                status="pending",
                evidence=[],
            ),
            dict(
                key="secret-c",
                question="Old question",
                context="",
                recommendation="",
                status="resolved",
                evidence=[],
            ),
        ],
        observed_at="2026-11-19T11:50:00Z",
        updated_at=None,
        error=None,
    )
    value.update(changes)
    return value


def test_review_status_and_no_internal_metadata():
    frame = view.render(snapshot(error="Observer timed out."), view.State(), 90, 30, now=NOW)
    text = "\n".join(frame.lines)
    assert "Plants are ready." in text
    assert "6:50 AM EST" in text and "10 min" in text and "Stale" in text
    assert "Observer timed out." in text
    assert "secret-a" not in text and "fictional" not in text


def test_tabs_selection_and_next_action():
    data = snapshot()
    state = view.handle_key(view.State(), "2", data, 70, 25)
    assert state.tab == 2 and state.selected_key == "secret-a"
    state = view.handle_key(state, "n", data, 70, 25)
    text = "\n".join(view.render(data, state, 70, 25, now=NOW).lines)
    assert "Buy pots?" in text and "Choose a delivery day?" not in text
    assert "secret-b" not in text and "Old question" not in text
    state = view.handle_key(state, "3", data, 70, 25)
    assert "Buy ten." in "\n".join(view.render(data, state, 70, 25, now=NOW).lines)
    assert view.handle_key(state, "p", data, 70, 25).selected_key == "secret-a"
    assert view.handle_key(state, "q", data, 70, 25).quit


def test_refresh_preserves_key_and_clamps_scroll():
    state = view.State(tab=2, selected_key="secret-b", scroll=999)
    data = snapshot()
    data["decisions"].reverse()
    updated = view.normalize(state, data, 80, 25)
    assert updated.selected_key == "secret-b" and updated.scroll == 0
    data["decisions"] = []
    updated = view.normalize(updated, data, 80, 25)
    assert updated.selected_key is None
    assert "No pending requests recorded" in "\n".join(
        view.render(data, updated, 80, 25, now=NOW).lines
    )


@pytest.mark.parametrize("width", [1, 2, 10, 25, 80])
def test_narrow_frames_strip_terminal_sequences_and_fit(width):
    data = snapshot(review="\x1b[31mRED\x1b[0m\x1b]0;secret title\x07\n界🙂\x08" + "x" * 100)
    frame = view.render(data, view.State(), width, 18, now=NOW)
    assert len(frame.lines) <= 18
    assert all(view.cell_width(line) <= width for line in frame.lines)
    assert all("\x1b" not in line and "\x08" not in line for line in frame.lines)
    assert "secret title" not in "\n".join(frame.lines)


def test_scrolling_and_pages_reach_content_without_overflow():
    data = snapshot(review="\n".join(f"Line {n}" for n in range(100)))
    state = view.handle_key(view.State(), curses.KEY_NPAGE, data, 80, 15)
    assert state.scroll > 1
    state = view.handle_key(state, curses.KEY_END, data, 80, 15)
    assert "Line 99" in "\n".join(view.render(data, state, 80, 15, now=NOW).lines)
    assert view.handle_key(state, curses.KEY_HOME, data, 80, 15).scroll == 0


def test_unavailable_unobserved_and_partial_coverage():
    assert "unavailable" in "\n".join(view.render(None, view.State(), 80, 25).lines).lower()
    data = snapshot(observed_at=None, review="Partial coverage: one source was truncated.")
    text = "\n".join(view.render(data, view.State(), 80, 25, now=NOW).lines)
    assert "not observed" in text.lower() and "Partial coverage" in text


def test_fake_store_loop_is_read_only_rate_limited_and_quiet(monkeypatch):
    class Store:
        calls = 0

        def get(self, name):
            assert name == "Garden"
            self.calls += 1
            return snapshot()

    class Screen:
        paints = 0
        ticks = 0

        def getmaxyx(self):
            return 25, 80

        def timeout(self, value):
            pass

        def erase(self):
            self.paints += 1

        def addstr(self, *args):
            pass

        def refresh(self):
            pass

        def get_wch(self):
            self.ticks += 1
            clock[0] += 0.1
            if self.ticks == 26:
                return "q"
            raise curses.error()

    clock = [0.0]
    monkeypatch.setattr(view.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(view, "_now", lambda: NOW)
    monkeypatch.setattr(curses, "curs_set", lambda _: None)
    screen, store = Screen(), Store()
    monkeypatch.setattr(curses, "wrapper", lambda callback: callback(screen))
    view.run(store, "Garden")
    assert 2 <= store.calls <= 3
    assert screen.paints == 1


def test_store_error_keeps_last_good_content(monkeypatch):
    data, error = view.read_snapshot(
        type("Store", (), {"get": lambda self, name: snapshot()})(), "Garden", None
    )
    assert error is None

    class Broken:
        def get(self, name):
            raise OSError("database locked")

    kept, error = view.read_snapshot(Broken(), "Garden", data)
    assert kept == data
    text = "\n".join(view.render(kept, view.State(), 80, 25, now=NOW, read_error=error).lines)
    assert "Plants are ready." in text and "unavailable" in text.lower()


def test_long_error_remains_scrollable_to_end():
    data = snapshot(error="Start " + "failure " * 100 + "END OF ERROR", review="END OF REVIEW")
    state = view.handle_key(view.State(), curses.KEY_END, data, 20, 12)
    text = "\n".join(view.render(data, state, 20, 12, now=NOW).lines)
    assert "END OF REVIEW" in text


def test_long_error_can_be_read_in_needs_you():
    data = snapshot(error="Start " + "failure " * 100 + "END OF ERROR")
    state = view.State(tab=2)
    found = ""
    for _ in range(200):
        found += "\n".join(view.render(data, state, 20, 12, now=NOW).lines) + "\n"
        state = view.handle_key(state, curses.KEY_DOWN, data, 20, 12)
    assert "END OF ERROR" in found


def test_read_failure_marks_recent_snapshot_stale():
    data = snapshot(observed_at="2026-11-19T11:59:50Z")
    text = "\n".join(
        view.render(data, view.State(), 100, 25, now=NOW, read_error="Desk unavailable").lines
    )
    assert "Stale" in text


def test_idle_loop_does_not_rebuild_frames(monkeypatch):
    calls = []
    original = view.render

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(view, "render", counted)
    test_fake_store_loop_is_read_only_rate_limited_and_quiet(monkeypatch)
    assert len(calls) == 1


def test_partial_coverage_warning_remains_visible_in_decision_tab():
    data = snapshot(review="Partial coverage: one source was truncated.")
    text = "\n".join(view.render(data, view.State(tab=2), 80, 25, now=NOW).lines)
    assert "Partial coverage" in text


def test_observation_time_is_converted_to_eastern():
    data = snapshot(observed_at="2026-11-19T06:50:00-05:00")
    text = "\n".join(view.render(data, view.State(), 100, 25, now=NOW).lines)
    assert "2026-11-19 6:50 AM EST" in text


def test_backend_numeric_timestamp_and_structured_partial_coverage():
    data = snapshot(observed_at=1795089000.0, observation={"partial": True}, review="Plants ready.")
    text = "\n".join(view.render(data, view.State(tab=2), 100, 25, now=NOW).lines)
    assert "6:50 AM EST" in text and "10 min" in text
    assert "Partial coverage" in text


def test_observer_partial_source_coverage_wording():
    data = snapshot(review="Partial source coverage. Plants ready.")
    text = "\n".join(view.render(data, view.State(tab=3), 100, 25, now=NOW).lines)
    assert "Partial coverage" in text


def test_backend_source_citations_are_descriptive_without_internal_ids():
    data = snapshot(
        review="Plants ready. [claude:private-session bytes 10..20 source mtime 1 observed 2]",
        sources=[dict(tool="claude", session_id="private-session", storage="hidden")],
    )
    text = "\n".join(view.render(data, view.State(), 100, 25, now=NOW).lines)
    assert "Plants ready." in text and "source observation" not in text
    assert "private-session" not in text and "bytes 10" not in text


def test_render_with_explicit_time_is_pure(monkeypatch):
    def forbidden_clock():
        raise AssertionError("Pure rendering must not read the wall clock")

    monkeypatch.setattr(view, "_now", forbidden_clock)
    frame = view.render(snapshot(), view.State(), 100, 25, now=NOW)
    assert "6:50 AM EST" in "\n".join(frame.lines)


@pytest.mark.parametrize(
    "changes, qualification",
    [
        ({"observed_at": None}, "first observation"),
        ({"error": "Timeout"}, "failed"),
        ({"observation": {"partial": True}}, "partial"),
        ({}, "observation status"),
    ],
)
def test_empty_pending_state_does_not_claim_nothing_needs_attention(changes, qualification):
    data = snapshot(decisions=[], **changes)
    text = "\n".join(view.render(data, view.State(tab=2), 100, 25, now=NOW).lines)
    assert "No pending requests recorded" in text
    assert qualification in text.lower()
    assert "No decisions need your attention" not in text


def test_next_action_makes_context_tab_obvious():
    text = "\n".join(view.render(snapshot(), view.State(tab=3), 100, 25, now=NOW).lines)
    assert "Press 2 for context" in text


def test_compact_frame_starts_briefing_without_duplicate_metadata():
    data = snapshot(
        observation={"partial": True}, review="Partial source coverage. Plants are ready."
    )
    frame = view.render(data, view.State(), 88, 23, now=NOW)
    text = "\n".join(frame.lines)
    assert frame.lines[4] == "Plants are ready."
    assert text.count("6:50 AM EST") == 1
    assert text.lower().count("partial") == 1
    assert "n/p Request" in frame.lines[-1]


def test_observation_error_detail_appears_once():
    frame = view.render(snapshot(error="Observer timed out."), view.State(), 88, 23, now=NOW)
    assert "\n".join(frame.lines).count("Observer timed out.") == 1


def test_default_render_uses_current_utc(monkeypatch):
    monkeypatch.setattr(view, "_now", lambda: NOW)
    text = "\n".join(view.render(snapshot(), view.State(), 88, 23).lines)
    assert "10 min" in text and "age unavailable" not in text


def test_eastern_timestamp_uses_daylight_saving_time():
    data = snapshot(observed_at="2026-07-01T16:00:00Z")
    text = "\n".join(
        view.render(
            data, view.State(), 120, 25, now=datetime(2026, 7, 1, 16, 1, tzinfo=timezone.utc)
        ).lines
    )
    assert "12:00 PM EDT" in text
    assert "UTC" not in text
