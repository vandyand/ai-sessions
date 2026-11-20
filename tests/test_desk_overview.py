import json
import os

import pytest

from ai_sessions.desk import observer
from ai_sessions.desk.store import ConflictError, Store
from ai_sessions.harnesses.desk_observer import BRIEFING_SCHEMA


def overview(citation="s0"):
    return {
        key: f"Reported {key} [{'s0' if key == 'current' and citation == 's32' else citation}]"
        for key in ("background", "current", "direction", "goal")
    }


def setup(tmp_path):
    path = tmp_path / "supervisor.jsonl"
    path.write_text(
        json.dumps({"type": "user", "message": {"content": "Build a color chooser."}}) + "\n",
        encoding="utf-8",
    )
    store = Store(tmp_path / "desk")
    store.create(
        "demo",
        [{"tool": "claude", "session_id": "fiction", "storage": str(path), "role": "supervisor"}],
        "x",
    )
    return store, path


def output(value=None, citation="s0"):
    data = {"review": f"Current work [{citation}]", "decisions": []}
    if value is not None:
        data["overview"] = value
    return json.dumps(data)


def test_overview_schema_and_atomic_store_persistence(tmp_path):
    store, _ = setup(tmp_path)
    first = store.publish("demo", 0, {}, "Review", [], overview=overview())
    assert Store(store.root).get("demo")["overview"] == overview()
    assert store.publish("demo", 1, {}, "Legacy", [])["overview"] == first["overview"]
    with pytest.raises(ConflictError):
        store.publish("demo", 0, {}, "stale", [], overview=overview("s1"))
    assert store.get("demo")["overview"] == first["overview"]
    with pytest.raises(ValueError):
        store.publish("demo", 2, {}, "bad", [], overview={"goal": 7})
    assert store.get("demo")["revision"] == 2
    assert "overview" in BRIEFING_SCHEMA["required"]
    assert set(BRIEFING_SCHEMA["properties"]["overview"]["required"]) == set(overview())


def test_markdown_projection_includes_sanitized_overview_at_same_revision(tmp_path):
    store, _ = setup(tmp_path)
    value = overview()
    value["background"] = "Project context\n# injected heading\n\x1b[31mred\x00 text"
    current = store.publish("demo", 0, {}, "Recent review", [], overview=value)
    paths = store.export("demo")
    review = paths["review"].read_text(encoding="utf-8")
    decisions = paths["decisions"].read_text(encoding="utf-8")
    assert f"revision: {current['revision']}" in review.splitlines()[0]
    assert review.splitlines()[0] == decisions.splitlines()[0]
    for title in ("Overview", "Background", "Current", "Direction", "Goal", "Review"):
        assert f"# {title}\n" in review
    assert review.index("## Overview") < review.index("## Review")
    assert "Recent review" in review and "Reported goal" in review
    assert "\n# injected heading\n" not in review
    assert "\x1b" not in review and "\x00" not in review
    assert store.get("demo")["overview"] == value  # Sanitization is projection-only.


def test_native_overview_is_validated_attributed_and_skips_unchanged(tmp_path):
    store, _ = setup(tmp_path)
    result = observer.refresh(store, "demo", runner=lambda p: output(overview()))
    assert result["error"] is None
    assert "claude:fiction bytes" in result["overview"]["goal"]
    assert "[s0]" not in result["overview"]["goal"]
    prompts = []
    again = observer.refresh(store, "demo", runner=lambda p: prompts.append(p) or output())
    assert not prompts and again["overview"] == result["overview"]


@pytest.mark.parametrize(
    "bad",
    [
        {"goal": "missing fields"},
        {**overview(), "goal": 1},
        {**overview(), "goal": "A fabricated objective without attribution"},
        {**overview(), "goal": "Wrong source [s99]"},
        {**overview(), "goal": "x" * 6001},
    ],
)
def test_invalid_overview_preserves_last_good_snapshot(tmp_path, bad):
    store, path = setup(tmp_path)
    first = observer.refresh(store, "demo", runner=lambda p: output(overview()))
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "user", "message": {"content": "Update"}}) + "\n")
    result = observer.refresh(store, "demo", runner=lambda p: output(bad))
    assert result["error"] and result["overview"] == first["overview"]
    assert result["review"] == first["review"]


def test_plain_missing_information_and_legacy_outputs(tmp_path):
    store, path = setup(tmp_path)
    missing = {
        key: "This has not been described in the watched conversations yet." for key in overview()
    }
    result = observer.refresh(store, "demo", runner=lambda p: output(missing))
    assert result["error"] is None and result["overview"] == missing
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "user", "message": {"content": "Progress"}}) + "\n")
    result = observer.refresh(store, "demo", runner=lambda p: output())
    assert result["error"] is None and result["overview"] == missing


def test_initial_supervisor_prefix_is_bounded_complete_and_preserves_start(tmp_path, monkeypatch):
    store, path = setup(tmp_path)
    first_line = path.read_bytes()
    with path.open("ab") as stream:
        stream.write(
            (
                json.dumps({"type": "user", "message": {"content": "old context " * 500}}) + "\n"
            ).encode("utf-8")
            * 100
        )
    calls = []
    original = observer.read_since

    def read(tool, ref, checkpoint, *, end):
        calls.append((checkpoint, end))
        return original(tool, ref, checkpoint, end=end)

    monkeypatch.setattr(observer, "read_since", read)
    prompts = []
    result = observer.refresh(
        store, "demo", runner=lambda p: prompts.append(p) or output(overview("s32"))
    )
    assert result["error"] is None
    payload = json.loads(prompts[0].split("\n", 1)[1])
    context = next(e for e in payload["evidence"] if e["citation"] == "s32")
    assert "Build a color chooser" in context["text"] and len(context["text"]) <= 6000
    prefix_call = next((start, end) for start, end in calls if start == 0 and end <= 16384)
    assert prefix_call[1] >= len(first_line)
    assert path.read_bytes()[prefix_call[1] - 1 : prefix_call[1]] == b"\n"
    assert "bytes 0.." in result["overview"]["goal"]


def test_existing_desk_gets_one_bounded_context_generation_without_tail_changes(tmp_path):
    store, _ = setup(tmp_path)
    observer.refresh(store, "demo", runner=lambda p: output())
    # Simulate an older persisted observation that never attempted overview context.
    with store._connect() as db:
        record = store._get(db, "demo")
        record["observation"].pop("overview_context_attempted", None)
        store._write(db, record)
    prompts = []
    result = observer.refresh(
        store, "demo", runner=lambda p: prompts.append(p) or output(overview("s32"), "s32")
    )
    assert len(prompts) == 1 and result["error"] is None
    assert result["overview"]["goal"]
    observer.refresh(store, "demo", runner=lambda p: pytest.fail("unchanged model call"))


def test_bootstrap_replays_current_bounded_tail_when_fingerprint_unchanged(tmp_path, monkeypatch):
    store, path = setup(tmp_path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            (json.dumps({"type": "user", "message": {"content": "routine " * 100}}) + "\n") * 500
        )
        stream.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": "Latest: preview has shipped; contrast checks are next."
                    },
                }
            )
            + "\n"
        )
    observer.refresh(store, "demo", runner=lambda p: output())
    with store._connect() as db:
        before = store._get(db, "demo")
        before["observation"].pop("overview_context_attempted", None)
        store._write(db, before)
    reads, prompts = [], []
    original = observer.read_since

    def read(tool, ref, checkpoint, *, end):
        reads.append((checkpoint, end))
        return original(tool, ref, checkpoint, end=end)

    monkeypatch.setattr(observer, "read_since", read)
    result = observer.refresh(
        store, "demo", runner=lambda p: prompts.append(p) or output(overview("s32"))
    )
    assert result["error"] is None
    evidence = json.loads(prompts[0].split("\n", 1)[1])["evidence"]
    assert "preview has shipped" in next(e["text"] for e in evidence if e["citation"] == "s0")
    assert any(start > 0 and end - start <= observer.MAX_SOURCE_BYTES for start, end in reads)
    assert "claude:fiction bytes" in result["overview"]["current"]
    observer.refresh(store, "demo", runner=lambda p: pytest.fail("second bootstrap"))


def test_historical_only_bootstrap_never_rolls_back_existing_review(tmp_path, monkeypatch):
    store, _ = setup(tmp_path)
    store.publish("demo", 0, {}, "Latest review: preview shipped.", [])
    source = store.get("demo")["sources"][0]
    coverage = dict(
        source,
        id="s0",
        status="available",
        changed=False,
        partial=False,
        start=0,
        checkpoint=0,
        source_modified_at=1,
        observed_at=1,
    )
    monkeypatch.setattr(observer, "_source", lambda *a: (coverage.copy(), ""))
    value = overview("s32")
    value["current"] = observer.MISSING_OVERVIEW_INFO
    result = observer.refresh(store, "demo", runner=lambda p: output(value, "s32"))
    assert result["error"] is None
    assert result["review"] == "Latest review: preview shipped."
    assert result["overview"]["current"] == observer.MISSING_OVERVIEW_INFO
    observer.refresh(store, "demo", runner=lambda p: pytest.fail("second bootstrap"))


@pytest.mark.parametrize("refs, accepted", [("[s0] [s32]", True), ("[s32]", False)])
def test_current_overview_accepts_mixed_but_rejects_only_historical_citations(refs, accepted):
    source = {
        "tool": "claude",
        "session_id": "fixture",
        "status": "available",
        "changed": True,
        "start": 0,
        "checkpoint": 100,
        "source_modified_at": 1,
        "observed_at": 1,
    }
    coverage = [dict(source, id="s0"), dict(source, id="s32", context_only=True)]
    value = overview()
    value["current"] = f"Current work with background context {refs}"
    if accepted:
        result = observer._validate(output(value), coverage)
        assert "Current work with background context" in result["overview"]["current"]
        assert "[s0]" not in result["overview"]["current"]
    else:
        with pytest.raises(ValueError, match="current tail evidence"):
            observer._validate(output(value), coverage)


def test_legacy_missing_overview_does_not_cause_repeated_context_calls(tmp_path):
    store, _ = setup(tmp_path)
    observer.refresh(store, "demo", runner=lambda p: output())
    observer.refresh(store, "demo", runner=lambda p: pytest.fail("legacy context retry"))


def test_context_cannot_resolve_decisions_as_current_evidence(tmp_path):
    store, _ = setup(tmp_path)
    item = {
        "key": "color",
        "question": "Which color?",
        "context": "Palette",
        "recommendation": "Blue",
        "status": "resolved",
        "evidence": ["s32"],
    }
    result = observer.refresh(
        store,
        "demo",
        runner=lambda p: json.dumps(
            {"review": "Old context [s32]", "overview": overview("s32"), "decisions": [item]}
        ),
    )
    assert result["error"] and result["decisions"] == []


def test_oversize_first_jsonl_record_never_reads_partial_context(tmp_path, monkeypatch):
    _, path = setup(tmp_path)
    path.write_bytes(b"x" * 16385 + b"\n")
    source = {"tool": "claude", "session_id": "fiction", "storage": str(path)}
    monkeypatch.setattr(observer, "read_since", lambda *a, **kw: pytest.fail("partial JSONL read"))
    assert observer._initial_context(source) == (None, "")


def test_empty_context_is_only_attempted_once(tmp_path, monkeypatch):
    store, path = setup(tmp_path)
    path.write_bytes(b"{}\n")
    calls = []
    original = observer._initial_context
    monkeypatch.setattr(
        observer, "_initial_context", lambda source: calls.append(source) or original(source)
    )
    observer.refresh(store, "demo", runner=lambda p: pytest.fail("no evidence"))
    observer.refresh(store, "demo", runner=lambda p: pytest.fail("no evidence"))
    assert len(calls) == 1


def test_budget_clips_historical_context_from_start(tmp_path, monkeypatch):
    store, _ = setup(tmp_path)
    coverage = [{"id": "s32", "context_only": True, "partial": False}]
    context = "ORIGINAL GOAL " + "routine text " * 1000
    monkeypatch.setattr(observer, "MAX_PROMPT_CHARS", len(observer.SYSTEM) + 1800)
    prompt = observer._build_prompt(
        store.get("demo"), coverage, [{"citation": "s32", "text": context}]
    )
    evidence = json.loads(prompt.split("\n", 1)[1])["evidence"][0]
    assert evidence["text"].startswith("ORIGINAL GOAL")
    assert len(evidence["text"]) < len(context)


def test_historical_context_excluded_from_native_decision_citations(monkeypatch):
    calls = []
    monkeypatch.setattr(
        observer.model_backend,
        "run_model",
        lambda *args, **kwargs: calls.append(kwargs) or output(),
    )
    payload = {
        "evidence": [
            {"citation": "s0", "text": "current"},
            {"citation": "s32", "text": "historical"},
        ],
        "coverage": [{"id": "s0"}, {"id": "s32", "context_only": True}],
    }
    observer._run_model(observer.SYSTEM + "\n" + json.dumps(payload))
    assert calls[0]["citation_ids"] == ["s0"]


@pytest.mark.skipif(
    os.environ.get("AI_SESSIONS_DESK_NATIVE_SMOKE") != "1",
    reason="Explicit opt-in: one tools-disabled native Haiku fixture call, bounded to 120 seconds",
)
def test_native_haiku_overview_fixture_smoke(monkeypatch):
    monkeypatch.setenv("AI_SESSIONS_DESK_MODEL", "haiku")
    source = {"tool": "claude", "session_id": "fixture", "storage": "fixture.jsonl"}
    coverage = [
        dict(
            source,
            id="s0",
            status="available",
            changed=True,
            partial=False,
            start=0,
            checkpoint=120,
            source_modified_at=1,
            observed_at=1,
        )
    ]
    before = {"review": "", "decisions": [], "overview": {}, "sources": [source]}
    evidence = [
        {
            "citation": "s0",
            "text": (
                "user: We are building a color chooser for a small design tool. "
                "Its goal is to let a person preview accessible color combinations. "
                "assistant: The first preview is implemented; next we will add contrast checks. "
                "No human decision is pending."
            ),
        }
    ]
    prompt = observer._build_prompt(before, coverage, evidence)
    result = observer._validate(observer._run_model(prompt), coverage)
    assert set(result["overview"]) == {"background", "current", "direction", "goal"}
    assert result["overview"]["goal"] != observer.MISSING_OVERVIEW_INFO
