from datetime import datetime, timezone

from ai_sessions.desk import view


def saved(path):
    stat = path.stat()
    source = dict(tool="claude", session_id="fiction", storage=str(path))
    return dict(
        name="Garden",
        review="Plants are ready.",
        decisions=[],
        error=None,
        sources=[source],
        observed_at=1,
        observation=dict(
            synthesized_at=1,
            sources=[
                dict(
                    source,
                    status="available",
                    fingerprint=[stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns],
                )
            ],
        ),
    )


def test_old_briefing_is_current_when_source_is_unchanged(tmp_path):
    path = tmp_path / "source.jsonl"
    path.write_text("{}\n")
    data = saved(path)
    store = type("Store", (), {"get": lambda self, name: data})()
    checked, error = view.read_snapshot(store, "Garden", None)
    frame = view.render(
        checked, view.State(), 80, 20, now=datetime.fromtimestamp(7201, timezone.utc)
    )
    assert frame.lines[0].endswith("Up to date · 2h")
    assert "Stale" not in "\n".join(frame.lines)
    assert "Last observation" not in "\n".join(frame.lines)
    assert "_freshness" not in data  # UI metadata never mutates stored snapshot.
    assert error is None


def test_source_changes_and_removal_are_distinct_from_age(tmp_path):
    path = tmp_path / "source.jsonl"
    path.write_text("{}\n")
    data = saved(path)
    store = type("Store", (), {"get": lambda self, name: data})()
    path.write_text("{}\n{}\n")
    checked, _ = view.read_snapshot(store, "Garden", None)
    assert "Supervisor has new content" in view.render(checked, view.State(), 80, 20).lines[0]
    path.unlink()
    checked, _ = view.read_snapshot(store, "Garden", None)
    assert "Source unavailable" in view.render(checked, view.State(), 80, 20).lines[0]


def test_source_rebinding_is_not_current(tmp_path):
    path = tmp_path / "source.jsonl"
    path.write_text("{}\n")
    data = saved(path)
    data["sources"][0] = dict(data["sources"][0], session_id="different")
    store = type("Store", (), {"get": lambda self, name: data})()
    checked, _ = view.read_snapshot(store, "Garden", None)
    assert "Supervisor has new content" in view.render(checked, view.State(), 80, 20).lines[0]


def test_narrow_tabs_help_and_scroll_are_discoverable():
    data = dict(
        name="Garden",
        review="\n".join(f"Line {n}" for n in range(100)),
        decisions=[],
        observed_at=1,
        observation={},
    )
    frame = view.render(data, view.State(), 40, 12)
    assert "? Help" in frame.lines[-1] and "q Exit" in frame.lines[-1]
    assert "1 Review" in frame.lines[1] and "2" in frame.lines[1] and "3" in frame.lines[1]
    assert "1-" in frame.lines[-1] and "/100" in frame.lines[-1]
    state = view.handle_key(view.State(), "?", data, 40, 12)
    text = "\n".join(view.render(data, state, 40, 40).lines)
    assert "Shift+Tab" in text and "Up to date" in text
    assert view.handle_key(state, "?", data, 40, 12).help is False


def test_help_aliases_and_watched_sessions_are_explicit():
    data = dict(
        name="Garden",
        review="Ready",
        decisions=[],
        observed_at=None,
        sources=[dict(title="Garden supervisor", role="supervisor")],
    )
    for key in ("h", "H", "?"):
        state = view.handle_key(view.State(), key, data, 80, 60)
        text = "\n".join(view.render(data, state, 80, 60).lines)
        assert state.help and "Garden supervisor" in text
        assert "not included automatically" in " ".join(text.split())
        assert not view.handle_key(state, "\x1b", data, 80, 60).help


def test_question_navigation_from_review_and_contextual_controls():
    data = dict(
        name="Garden",
        review="Ready",
        decisions=[
            dict(key="one", question="Which day?", status="pending"),
            dict(key="two", question="Which plant?", status="pending"),
        ],
    )
    assert view.handle_key(view.State(), "n", data, 80, 20).selected_key == "one"
    last = view.handle_key(view.State(), "p", data, 80, 20)
    assert last.tab == 2 and last.selected_key == "two"
    frame = view.render(data, view.State(), 100, 20)
    assert "Scroll" not in frame.lines[-1] and "n Questions" in frame.lines[-1]
    data["decisions"] = data["decisions"][:1]
    frame = view.render(data, view.State(tab=2), 100, 20)
    assert "n/p" not in "\n".join(frame.lines)
