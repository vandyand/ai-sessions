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
    frame = view.render(snapshot(error="Observer timed out."), view.State(tab=2), 90, 30, now=NOW)
    text = "\n".join(frame.lines)
    assert "Plants are ready." in text
    assert "Update failed" in frame.lines[0] and "10m" in frame.lines[0]
    assert "Update failed" in text and "? Details" in text
    assert "secret-a" not in text and "fictional" not in text


def test_tabs_selection_and_next_action():
    data = snapshot()
    state = view.handle_key(view.State(tab=2), "3", data, 70, 25)
    assert state.tab == 3 and state.selected_key == "secret-a"
    state = view.handle_key(state, "n", data, 70, 25)
    text = "\n".join(view.render(data, state, 70, 25, now=NOW).lines)
    assert "Buy pots?" in text and "Choose a delivery day?" not in text
    assert "secret-b" not in text and "Old question" not in text
    state = view.handle_key(state, "4", data, 70, 25)
    assert "Buy ten." in "\n".join(view.render(data, state, 70, 25, now=NOW).lines)
    assert view.handle_key(state, "p", data, 70, 25).selected_key == "secret-a"
    assert view.handle_key(state, "q", data, 70, 25).quit


def test_refresh_preserves_key_and_clamps_scroll():
    state = view.State(tab=3, selected_key="secret-b", scroll=999)
    data = snapshot()
    data["decisions"].reverse()
    updated = view.normalize(state, data, 80, 25)
    assert updated.selected_key == "secret-b" and updated.scroll == 0
    data["decisions"] = []
    updated = view.normalize(updated, data, 80, 25)
    assert updated.selected_key == "secret-b"
    assert "No longer listed" in "\n".join(view.render(data, updated, 80, 25, now=NOW).lines)


@pytest.mark.parametrize("width", [1, 2, 10, 25, 80])
def test_narrow_frames_strip_terminal_sequences_and_fit(width):
    data = snapshot(review="\x1b[31mRED\x1b[0m\x1b]0;secret title\x07\n界🙂\x08" + "x" * 100)
    frame = view.render(data, view.State(tab=2), width, 18, now=NOW)
    assert len(frame.lines) <= 18
    assert all(view.cell_width(line) <= width for line in frame.lines)
    assert all("\x1b" not in line and "\x08" not in line for line in frame.lines)
    assert "secret title" not in "\n".join(frame.lines)


def test_scrolling_and_pages_reach_content_without_overflow():
    data = snapshot(review="\n".join(f"Line {n}" for n in range(100)))
    state = view.handle_key(view.State(tab=2), curses.KEY_NPAGE, data, 80, 15)
    assert state.scroll > 1
    state = view.handle_key(state, curses.KEY_END, data, 80, 15)
    assert "Line 99" in "\n".join(view.render(data, state, 80, 15, now=NOW).lines)
    assert view.handle_key(state, curses.KEY_HOME, data, 80, 15).scroll == 0


def test_unavailable_unobserved_and_partial_coverage():
    assert "Preparing" in view.render(None, view.State(tab=2), 80, 25).lines[0]
    data = snapshot(observed_at=None, review="Partial coverage: one source was truncated.")
    text = "\n".join(view.render(data, view.State(tab=2), 80, 25, now=NOW).lines)
    assert "Preparing" in text and "last observation" not in text.lower()


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
    text = "\n".join(view.render(kept, view.State(tab=2), 80, 25, now=NOW, read_error=error).lines)
    assert "Plants are ready." in text and "Reconnecting" in text


def test_long_error_remains_scrollable_to_end():
    data = snapshot(error="Start " + "failure " * 100 + "END OF ERROR", review="END OF REVIEW")
    state = view.handle_key(view.State(tab=2), curses.KEY_END, data, 20, 12)
    text = "\n".join(view.render(data, state, 20, 12, now=NOW).lines)
    assert "END OF REVIEW" in text


def test_long_error_can_be_read_in_needs_you():
    data = snapshot(error="Start " + "failure " * 100 + "END OF ERROR")
    state = view.State(tab=3, help=True)
    found = ""
    for _ in range(200):
        found += "\n".join(view.render(data, state, 20, 12, now=NOW).lines) + "\n"
        state = view.handle_key(state, curses.KEY_DOWN, data, 20, 12)
    assert "END OF ERROR" in found


def test_read_failure_marks_recent_snapshot_stale():
    data = snapshot(observed_at="2026-11-19T11:59:50Z")
    text = "\n".join(
        view.render(data, view.State(tab=2), 100, 25, now=NOW, read_error="Desk unavailable").lines
    )
    assert "Reconnecting" in text


def test_idle_loop_does_not_rebuild_frames(monkeypatch):
    calls = []
    original = view.render

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(view, "render", counted)
    test_fake_store_loop_is_read_only_rate_limited_and_quiet(monkeypatch)
    assert len(calls) == 1


def test_partial_history_detail_is_available_in_help():
    data = snapshot(review="Partial coverage: one source was truncated.")
    text = "\n".join(view.render(data, view.State(tab=3, help=True), 80, 60, now=NOW).lines)
    assert "Recent history only" in text


def test_observation_time_is_converted_to_eastern():
    data = snapshot(observed_at="2026-11-19T06:50:00-05:00")
    text = "\n".join(view.render(data, view.State(tab=2, help=True), 100, 60, now=NOW).lines)
    assert "2026-11-19 6:50 AM EST" in text


def test_backend_numeric_timestamp_and_structured_partial_coverage():
    data = snapshot(observed_at=1795089000.0, observation={"partial": True}, review="Plants ready.")
    text = "\n".join(view.render(data, view.State(tab=3, help=True), 100, 70, now=NOW).lines)
    assert "10m" in text and "Last observation" not in text
    assert "Recent history only" in text


def test_observer_partial_source_coverage_wording():
    data = snapshot(review="Partial source coverage. Plants ready.")
    text = "\n".join(view.render(data, view.State(tab=4, help=True), 100, 70, now=NOW).lines)
    assert "Recent history only" in text


def test_backend_source_citations_are_descriptive_without_internal_ids():
    data = snapshot(
        review="Plants ready. [claude:private-session bytes 10..20 source mtime 1 observed 2]",
        sources=[dict(tool="claude", session_id="private-session", storage="hidden")],
    )
    text = "\n".join(view.render(data, view.State(tab=2), 100, 25, now=NOW).lines)
    assert "Plants ready." in text and "source observation" not in text
    assert "private-session" not in text and "bytes 10" not in text


def test_render_with_explicit_time_is_pure(monkeypatch):
    def forbidden_clock():
        raise AssertionError("Pure rendering must not read the wall clock")

    monkeypatch.setattr(view, "_now", forbidden_clock)
    frame = view.render(snapshot(), view.State(tab=2), 100, 25, now=NOW)
    assert "Saved" in frame.lines[0] and "10m" in frame.lines[0]


@pytest.mark.parametrize(
    "changes, qualification",
    [
        ({"observed_at": None}, "first briefing"),
        ({"error": "Timeout"}, "failed"),
        ({"observation": {"partial": True}}, "earlier conversation"),
        ({}, "desk conversation"),
    ],
)
def test_empty_pending_state_does_not_claim_nothing_needs_attention(changes, qualification):
    data = snapshot(decisions=[], **changes)
    text = "\n".join(view.render(data, view.State(tab=3), 100, 25, now=NOW).lines)
    assert "No open decisions" in text
    assert qualification in text.lower()
    assert "No decisions need your attention" not in text


def test_old_next_action_key_maps_to_decisions():
    text = "\n".join(view.render(snapshot(), view.State(tab=4), 100, 25, now=NOW).lines)
    assert "[3 Decisions]" in text
    assert "Rain expected." in text and "Use Friday." in text


def test_compact_frame_starts_briefing_without_duplicate_metadata():
    data = snapshot(
        observation={"partial": True}, review="Partial source coverage. Plants are ready."
    )
    frame = view.render(data, view.State(tab=2), 88, 23, now=NOW)
    text = "\n".join(frame.lines)
    assert frame.lines[3] == "Plants are ready."
    assert "6:50 AM EST" not in text and "10m" in frame.lines[0]
    assert "partial" not in text.lower()
    assert "? Help" in frame.lines[-1] and "q Exit" in frame.lines[-1]


def test_observation_error_detail_appears_once():
    frame = view.render(
        snapshot(error="Observer timed out."), view.State(tab=2, help=True), 88, 70, now=NOW
    )
    assert "\n".join(frame.lines).count("Observer timed out.") == 1


def test_default_render_uses_current_utc(monkeypatch):
    monkeypatch.setattr(view, "_now", lambda: NOW)
    text = "\n".join(view.render(snapshot(), view.State(tab=2), 88, 23).lines)
    assert "10m" in text and "age unavailable" not in text


def test_eastern_timestamp_uses_daylight_saving_time():
    data = snapshot(observed_at="2026-07-01T16:00:00Z")
    text = "\n".join(
        view.render(
            data,
            view.State(tab=2, help=True),
            120,
            60,
            now=datetime(2026, 7, 1, 16, 1, tzinfo=timezone.utc),
        ).lines
    )
    assert "12:00 PM EDT" in text
    assert "UTC" not in text


@pytest.mark.parametrize(
    "key,delta", [(curses.KEY_RIGHT, 1), ("\t", 1), (curses.KEY_LEFT, -1), (curses.KEY_BTAB, -1)]
)
@pytest.mark.parametrize("tab", [1, 2, 3])
def test_arrow_and_tab_keys_cycle_briefing_views(key, delta, tab):
    data = snapshot()
    state = view.handle_key(view.State(tab=tab, scroll=3), key, data, 80, 25)
    assert state.tab == (tab - 1 + delta) % 3 + 1
    assert state.scroll == 0


def detailed_snapshot(**changes):
    data = snapshot()
    data["decision_details"] = {
        "secret-a": dict(
            schema_version=1,
            kind="choice",
            readiness="ready",
            summary="Choose whether the garden should open Friday or Monday.",
            background="The garden gives residents a shaded courtyard. Friday needs a covered unloading area.",
            why_now="The nursery needs a delivery booking today.",
            options=[
                dict(
                    id="fri",
                    label="Friday delivery",
                    description="Reserve the covered bay.",
                    benefits=["Plants arrive before the weekend."],
                    risks=["Rain may delay unloading."],
                    consequences="A volunteer must open the bay.",
                    evidence=["opaque-id"],
                )
            ],
            recommendation=dict(
                option_id="fri", reason="The covered bay protects the plants from rain."
            ),
            if_deferred="The nursery will offer the next slot to another customer.",
            deadline=None,
            missing_information=[],
            citations={"summary": ["opaque-id"]},
            evidence=[
                dict(
                    id="opaque-id",
                    title="Garden planning conversation",
                    tool="claude",
                    session_id="private-native-id",
                    home="C:/secret-home",
                    storage="C:/secret-path",
                    identity=[1, 2],
                    start=10,
                    end=20,
                    digest="private-digest",
                    text="A covered bay is available Friday.",
                    context_only=True,
                )
            ],
        )
    }
    data["decision_details"]["secret-a"].update(changes)
    return data


def test_complete_decision_has_options_reasons_and_context_without_disclosure():
    data = detailed_snapshot()
    text = "\n".join(view.render(data, view.State(tab=3), 100, 65, now=NOW).lines)
    for expected in (
        "Background ready",
        "shaded courtyard",
        "Friday delivery",
        "Reserve the covered bay",
        "Plants arrive",
        "Rain may delay",
        "volunteer must open",
        "protects the plants",
        "another customer",
        "Desk conversation",
        "d Sources",
    ):
        assert expected in text
    assert "covered bay is available" not in text
    for hidden in ("secret-a", "opaque-id", "private-native-id", "C:/secret", "private-digest"):
        assert hidden not in text


def test_inline_background_sources_are_human_and_selection_fold_survives_refresh():
    data = detailed_snapshot()
    state = view.handle_key(view.State(tab=3), "d", data, 60, 24)
    text = "\n".join(view._body(data, state, 60))
    assert "Garden planning conversation" in text
    assert "A covered bay is available Friday." in text
    assert "Earlier context" in text
    assert "private-native-id" not in text and "bytes 10" not in text
    state = view.handle_key(state, curses.KEY_NPAGE, data, 60, 24)
    refreshed = view.normalize(state, detailed_snapshot(), 60, 24)
    assert refreshed == state
    collapsed = view.handle_key(state, "d", data, 60, 24)
    assert "covered bay is available" not in "\n".join(view._body(data, collapsed, 60))


def test_resolved_selection_never_silently_jumps_to_another_decision():
    data = detailed_snapshot()
    state = view.normalize(view.State(tab=3), data, 80, 25)
    data["decisions"][0]["status"] = "resolved"
    state = view.normalize(state, data, 80, 25)
    text = "\n".join(view._body(data, state, 80))
    assert "Resolved" in text and "garden should open" in text
    assert state.selected_key == "secret-a"
    state = view.handle_key(state, "n", data, 80, 25)
    assert state.selected_key == "secret-b"


def test_missing_context_stays_explicit_and_legacy_never_claims_ready():
    data = detailed_snapshot(
        readiness="needs_context",
        options=[],
        missing_information=["What does the deposit cover?", "What is the refundable amount?"],
        recommendation=dict(option_id=None, reason="The refund terms have not been supplied."),
    )
    text = "\n".join(view._body(data, view.normalize(view.State(tab=3), data, 80, 25), 80))
    assert (
        "Background needed" in text and "refundable amount" in text and "not been supplied" in text
    )
    data.pop("decision_details")
    text = "\n".join(view._body(data, view.normalize(view.State(tab=3), data, 80, 25), 80))
    assert "Preparing background" in text and "Rain expected." in text
    assert "Background ready" not in text


@pytest.mark.parametrize("width", [40, 60, 100])
def test_rich_decision_scroll_reaches_every_section_and_disclosure_is_visible(width):
    data = detailed_snapshot()
    state = view.normalize(view.State(tab=3), data, width, 18)
    frame = view.render(data, state, width, 18, now=NOW)
    assert "d Sources" in "\n".join(frame.lines)
    assert "3 Decisions" in frame.lines[1]
    assert all(view.cell_width(line) <= width for line in frame.lines)
    state = view.handle_key(state, "d", data, width, 18)
    state = view.handle_key(state, curses.KEY_END, data, width, 18)
    text = "\n".join(view.render(data, state, width, 18, now=NOW).lines)
    assert "covered bay is available" in text
    assert "Desk conversation" in " ".join(text.split())


def test_changed_core_without_details_does_not_reuse_old_context():
    data = detailed_snapshot()
    state = view.normalize(view.State(tab=3), data, 80, 25)
    data["decisions"][0]["question"] = "Which supplier should replace the nursery?"
    data["decision_details"] = {}
    state = view.normalize(state, data, 80, 25)
    text = "\n".join(view._body(data, state, 80))
    assert "Which supplier" in text and "Preparing background" in text
    assert "covered bay protects" not in text


def test_sources_only_use_explicit_timestamps_and_are_terminal_safe():
    data = detailed_snapshot()
    evidence = data["decision_details"]["secret-a"]["evidence"][0]
    evidence["timestamp"] = "2026-07-01T16:00:00Z"
    evidence["title"] = "\x1b]0;secret title\x07Garden planning conversation"
    evidence["text"] += "\x1b[31mSafe text\x1b[0m"
    state = view.handle_key(view.State(tab=3), "d", data, 80, 25)
    text = "\n".join(view._body(data, state, 80))
    assert "12:00 PM EDT" in text and "UTC" not in text
    assert "secret title" not in text and "\x1b" not in text
    assert "1970" not in text  # start/end are byte offsets, never timestamps.


def test_fold_is_per_decision_and_reorder_preserves_selection_without_mutation():
    from copy import deepcopy

    data = detailed_snapshot()
    before = deepcopy(data)
    state = view.handle_key(view.State(tab=3), "d", data, 80, 25)
    state = view.handle_key(state, "n", data, 80, 25)
    assert state.selected_key == "secret-b"
    assert "Source excerpts are still" not in "\n".join(view._body(data, state, 80))
    state = view.handle_key(state, "p", data, 80, 25)
    assert "Garden planning conversation" in "\n".join(view._body(data, state, 80))
    assert data == before
    data["decisions"].reverse()
    refreshed = view.normalize(state, data, 80, 25)
    assert refreshed.selected_key == state.selected_key and refreshed.expanded == state.expanded


@pytest.mark.parametrize("kind", ["action", "information"])
def test_action_and_information_requests_do_not_invent_yes_no_choices(kind):
    data = detailed_snapshot(
        kind=kind,
        options=[],
        readiness="needs_context",
        summary="Provide the covered bay opening hours.",
        recommendation=dict(option_id=None, reason="Opening hours have not been provided."),
        missing_information=["Who holds the bay key?"],
    )
    state = view.normalize(view.State(tab=3), data, 80, 25)
    text = "\n".join(view._body(data, state, 80))
    assert "Opening hours have not been provided" in text and "Who holds the bay key" in text
    assert "Options and tradeoffs" not in text and "Yes" not in text and "No" not in text


def test_new_request_after_leaving_closed_card_has_working_disclosure():
    data = detailed_snapshot()
    state = view.normalize(view.State(tab=3), data, 80, 25)
    empty = snapshot(decisions=[])
    state = view.normalize(state, empty, 80, 25)
    state = view.handle_key(state, "2", empty, 80, 25)
    state = view.handle_key(state, "3", empty, 80, 25)
    state = view.normalize(state, data, 80, 25)
    assert state.selected_key == "secret-a"
    state = view.handle_key(state, "d", data, 80, 25)
    assert "Garden planning conversation" in "\n".join(view._body(data, state, 80))


def test_explicit_deadline_is_converted_to_eastern():
    data = detailed_snapshot(deadline="2026-11-23T14:00:00Z")
    state = view.normalize(view.State(tab=3), data, 80, 25)
    text = "\n".join(view._body(data, state, 80))
    assert "2026-11-23 9:00 AM EST" in text and "14:00:00Z" not in text


def test_reconstructed_summary_replaces_lossy_core_premise_without_approval_inference():
    data = detailed_snapshot(
        kind="information",
        readiness="needs_context",
        options=[],
        summary="Did you previously approve the October 16 test that already ran?",
        background="Result files exist, but the approval record is missing. Completion does not establish consent.",
        why_now="The team needs to reconcile the historical approval record.",
        recommendation=dict(
            option_id=None, reason="Prior consent cannot be inferred from the result files."
        ),
        missing_information=["Do you remember giving approval before that test?"],
    )
    data["decisions"][0].update(
        question="Should we run the October 16 test?", recommendation="Approve the test now."
    )
    state = view.normalize(view.State(tab=3), data, 100, 30)
    text = "\n".join(view._body(data, state, 100))
    assert "Did you previously approve" in text and "already ran?" in text
    assert "Should we run" not in text and "Approve the test now" not in text
    assert "cannot be inferred" in text and "Background needed" in text


@pytest.mark.parametrize("tab", [1, 2])
def test_overview_review_teasers_use_canonical_summary_and_bounded_count(tab):
    from copy import deepcopy

    data = detailed_snapshot(summary="Did you previously approve the test that already ran?")
    data["decisions"][0]["question"] = "Should we run a new test?"
    sample = deepcopy(data["decisions"][1])
    data["decisions"].extend(
        dict(sample, key=f"additional-{n}", question=f"Extra question {n}") for n in range(78)
    )
    state = view.normalize(view.State(tab=tab), data, 80, 25)
    text = "\n".join(view._body(data, state, 80))
    assert "Your decisions · 80 open" in text
    assert "Did you previously approve the test that already ran?" in text
    assert "Should we run a new test?" not in text and "Extra question 77" not in text
    assert "n/p: open Decisions" in text
    assert view.handle_key(state, "n", data, 80, 25).selected_key == "secret-a"


def test_background_labels_and_closed_cards_do_not_suggest_a_new_action():
    data = detailed_snapshot()
    state = view.normalize(view.State(tab=3), data, 40, 18)
    assert "Background ready" in "\n".join(view._body(data, state, 40))
    data["decisions"][0]["status"] = "resolved"
    state = view.normalize(state, data, 40, 18)
    text = "\n".join(view._body(data, state, 40))
    assert text.startswith("Resolved")
    assert "Background ready" not in text and "Background needed" not in text
    assert "Preparing background" not in text
    frame = view.render(data, state, 40, 18, now=NOW)
    assert "d Sources" in frame.lines[-1]
    assert "d Background" not in "\n".join(frame.lines)
