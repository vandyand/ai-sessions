import hashlib
import json
from pathlib import Path

import pytest

from ai_sessions.desk import decision_context as context


def line(tool, text, role="assistant"):
    message = {"role": role, "content": [{"type": "text", "text": text}]}
    if tool == "claude":
        value = {"type": role, "message": message}
    else:
        value = {"type": "response_item", "payload": dict(message, type="message")}
    return (json.dumps(value) + "\n").encode()


def record(tmp_path, tool="claude", content=b""):
    path = tmp_path / "supervisor.jsonl"
    path.write_bytes(content)
    source = dict(
        tool=tool,
        session_id="supervisor",
        home=str(tmp_path),
        storage=str(path),
        title="Garden supervisor",
        role="supervisor",
    )
    return {"sources": [source]}, path


def question():
    return dict(
        key="oct16-test",
        question="Should we run the October 16 test?",
        context="The session is waiting.",
        evidence=[],
    )


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_finds_definition_outside_recent_tail_and_never_reads_tool_text(tmp_path, tool):
    definition = (
        "The October 16 test compares the two irrigation schedules on the saved garden dataset."
    )
    tool_row = (
        json.dumps({"type": "tool_result", "text": "PRIVATE_TOOL_OUTPUT " * 50000}).encode() + b"\n"
    )
    saved, path = record(
        tmp_path,
        tool,
        line(tool, definition) + tool_row + line(tool, "Still waiting on the October 16 test."),
    )
    result = context.gather_context(saved, question())
    text = "\n".join(e["text"] for e in result["evidence"])
    assert definition in text
    assert "PRIVATE_TOOL_OUTPUT" not in text
    assert any(e["context_only"] and definition in e["text"] for e in result["evidence"])
    assert any(not e["context_only"] and "Still waiting" in e["text"] for e in result["evidence"])
    for entry in result["evidence"]:
        raw = path.read_bytes()[entry["start"] : entry["end"]]
        assert hashlib.sha256(raw).hexdigest() == entry["digest"]
        assert entry["session_id"] == "supervisor"
        assert entry["identity"]


def test_same_input_has_stable_fingerprint_and_ids(tmp_path):
    saved, _ = record(
        tmp_path, content=line("claude", "The October 16 test compares two schedules.")
    )
    a = context.gather_context(saved, question())
    b = context.gather_context(saved, question())
    assert a["fingerprint"] == b["fingerprint"]
    assert a["evidence"] == b["evidence"]


def test_only_explicit_pinned_sources_are_opened(tmp_path, monkeypatch):
    saved, source = record(tmp_path, content=line("claude", "October 16 test is a comparison."))
    external = tmp_path / "not-selected.jsonl"
    external.write_bytes(line("claude", "UNSELECTED PRIVATE CONTENT"))
    wanted = question()
    wanted["evidence"] = [f"claude:other bytes 0..999 path {external}"]
    original = Path.open
    reads = []

    def opened(path, *args, **kwargs):
        reads.append(path.resolve())
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)
    result = context.gather_context(saved, wanted)
    assert set(reads) == {source.resolve()}
    assert "UNSELECTED" not in str(result)


def test_budget_is_finite_and_omitted_history_is_explicit(tmp_path, monkeypatch):
    saved, _ = record(tmp_path, content=line("claude", "October 16 test. " * 2000))
    monkeypatch.setattr(context, "MAX_SCAN_BYTES", 1024)
    result = context.gather_context(saved, question())
    assert result["bytes_read"] <= 1024
    assert result["limitations"]


def test_unavailable_source_is_not_fabricated_context(tmp_path):
    saved, path = record(tmp_path)
    path.unlink()
    result = context.gather_context(saved, question())
    assert not result["evidence"]
    assert result["limitations"]


def test_replacement_discards_captured_source_evidence(tmp_path, monkeypatch):
    saved, path = record(tmp_path, content=line("claude", "October 16 test uses the old schedule."))
    original = context._scan_source

    def replaced(*args, **kwargs):
        value = original(*args, **kwargs)
        other = tmp_path / "replacement.jsonl"
        other.write_bytes(line("claude", "Different history."))
        other.replace(path)
        return value

    monkeypatch.setattr(context, "_scan_source", replaced)
    result = context.gather_context(saved, question())
    assert not result["evidence"]
    assert any("changed" in s.lower() for s in result["limitations"])


def test_concurrent_append_cannot_enter_fixed_snapshot(tmp_path, monkeypatch):
    saved, path = record(
        tmp_path, content=line("claude", "October 16 test compares two schedules.")
    )
    original = context._scan_source

    def appended(*args, **kwargs):
        value = original(*args, **kwargs)
        with path.open("ab") as f:
            f.write(line("claude", "NEW CONTENT AFTER SNAPSHOT"))
        return value

    monkeypatch.setattr(context, "_scan_source", appended)
    result = context.gather_context(saved, question())
    assert result["evidence"]
    assert "NEW CONTENT AFTER SNAPSHOT" not in str(result)


def test_text_and_selected_excerpt_counts_are_bounded(tmp_path, monkeypatch):
    raw = b"".join(line("claude", f"October 16 test detail {i}. " * 100) for i in range(50))
    saved, _ = record(tmp_path, content=raw)
    monkeypatch.setattr(context, "MAX_CONTEXT_CHARS", 3000)
    result = context.gather_context(saved, question())
    assert sum(len(e["text"]) for e in result["evidence"]) <= 3000
    assert len([e for e in result["evidence"] if e["context_only"]]) <= context.MAX_HISTORICAL


def test_cancelled_retrieval_stops_without_another_read(tmp_path):
    import threading

    from ai_sessions.harnesses.desk_observer import ObservationCancelled

    saved, _ = record(tmp_path, content=line("claude", "October 16 test."))
    stop = threading.Event()
    stop.set()
    with pytest.raises(ObservationCancelled):
        context.gather_context(saved, question(), stop=stop)


def test_mainline_context_does_not_mix_in_a_sidechain(tmp_path):
    side = json.loads(line("claude", "October 16 test SIDECHAIN " * 100))
    side["isSidechain"] = True
    raw = (json.dumps(side) + "\n").encode() * 10 + line(
        "claude", "October 16 test compares the mainline schedules."
    )
    saved, _ = record(tmp_path, content=raw)
    result = context.gather_context(saved, question())
    assert "mainline schedules" in str(result)
    assert "SIDECHAIN" not in str(result)


def test_first_recent_passage_belongs_to_the_cited_primary_source(tmp_path):
    saved, _ = record(tmp_path, content=line("claude", "October 16 test primary explanation."))
    other = tmp_path / "manager.jsonl"
    other.write_bytes(line("codex", "Unrelated manager update."))
    saved["sources"].append(
        dict(
            tool="codex",
            session_id="manager",
            home=str(tmp_path),
            storage=str(other),
            title="Manager",
            role="team",
        )
    )
    result = context.gather_context(saved, question())
    assert result["evidence"][0]["session_id"] == "supervisor"


def test_many_selected_sources_fit_the_shared_evidence_schema(tmp_path):
    saved = {"sources": []}
    for i in range(20):
        path = tmp_path / f"source-{i}.jsonl"
        path.write_bytes(line("claude", f"October 16 test source {i} explanation."))
        saved["sources"].append(
            dict(
                tool="claude",
                session_id=str(i),
                home=str(tmp_path),
                storage=str(path),
                title=f"Source {i}",
            )
        )
    result = context.gather_context(saved, question())
    assert len(result["evidence"]) <= 16
    assert result["limitations"]
