import copy
import hashlib
import json

import pytest

from ai_sessions.desk import decisions
from ai_sessions.desk.store import ConflictError, Store


def core(question="Should we run the October 16 test?"):
    return dict(
        key="oct16",
        question=question,
        context="Trading project",
        recommendation="",
        status="pending",
        evidence=["original request"],
    )


def context():
    text = "User asks whether to run the October 16 test. Its purpose is not described."
    return dict(
        fingerprint="context-1",
        limitations=[],
        evidence=[
            dict(
                id="d0",
                title="Trading request",
                tool="codex",
                session_id="native",
                home="home",
                storage="/pinned/source.jsonl",
                identity=[1, 2],
                start=0,
                end=100,
                digest=hashlib.sha256(text.encode()).hexdigest(),
                text=text,
                context_only=True,
            )
        ],
    )


def details():
    return dict(
        schema_version=1,
        kind="choice",
        readiness="needs_context",
        summary="Should we run the October 16 test?",
        background=decisions.MISSING_INFO,
        why_now=decisions.MISSING_INFO,
        options=[],
        recommendation=dict(option_id=None, reason="The test's purpose is missing."),
        if_deferred=None,
        deadline=None,
        missing_information=["What the October 16 test measures and requires."],
        citations=dict(
            summary=["d0"],
            background=[],
            why_now=[],
            recommendation=[],
            if_deferred=[],
            deadline=[],
        ),
    )


@pytest.fixture
def desk(tmp_path):
    store = Store(tmp_path / "desk")
    store.create(
        "demo",
        [dict(tool="codex", session_id="native", storage="/pinned/source.jsonl", home="home")],
        "project",
    )
    store.publish("demo", 0, dict(sources=[]), "Current review", [core()])
    return store


def enrich(store, *, supplied=None, runner=None):
    return decisions.enrich_pending(
        store,
        "demo",
        context_runner=lambda *a: supplied or context(),
        runner=runner or (lambda p: json.dumps(details())),
    )


def test_details_sidecar_survives_old_publish_and_does_not_enter_cores(desk):
    before = desk.get("demo")
    enriched = enrich(desk)
    assert enriched["decisions"] == before["decisions"]
    assert enriched["review"] == before["review"]
    assert enriched["observation"] == before["observation"]
    assert enriched["decision_details"]["oct16"]["evidence"] == context()["evidence"]
    assert enriched["revision"] == before["revision"] + 1
    # Simulate the old worker's publication with no details keyword at all.
    result = desk.publish(
        "demo", enriched["revision"], before["observation"], "Updated review", [core()]
    )
    assert result["decision_details"] == enriched["decision_details"]
    assert set(result["decisions"][0]) == set(core())
    assert Store(desk.root).get("demo")["decision_details"] == enriched["decision_details"]


def test_old_worker_material_ask_change_hides_stale_details(desk):
    enrich(desk)
    with desk._connect() as db:
        saved = json.loads(db.execute("SELECT data FROM desks WHERE name='demo'").fetchone()[0])
        saved["decisions"] = [core("Should we delete the trading results?")]
        db.execute("UPDATE desks SET data=? WHERE name='demo'", (json.dumps(saved),))
    assert desk.get("demo")["decision_details"] == {}


def test_unchanged_migration_and_reload_call_model_only_once(desk):
    calls = []

    def runner(p):
        calls.append(p)
        return json.dumps(details())

    enrich(desk, runner=runner)
    enrich(Store(desk.root), runner=runner)
    assert len(calls) == 1
    assert "previous_decisions" not in calls[0]
    changed = context()
    changed["fingerprint"] = "context-2"
    enrich(desk, supplied=changed, runner=runner)
    assert len(calls) == 2


def test_invalid_enrichment_is_cached_without_failing_good_briefing(desk):
    calls = []

    def runner(p):
        calls.append(p)
        return "invalid output"

    before = desk.get("demo")
    failed = enrich(desk, runner=runner)
    assert failed["decision_enrichment"]["oct16"]["status"] == "failed"
    assert isinstance(failed["decision_enrichment"]["oct16"]["checked_at"], (float, int))
    assert failed["decision_details"] == before["decision_details"]
    for key in ("revision", "decisions", "review", "error"):
        assert failed[key] == before[key]
    assert enrich(desk, runner=runner)["decision_enrichment"]["oct16"]["status"] == "failed"
    assert len(calls) == 1
    assert desk.get("demo")["error"] is None


def test_enrichment_metadata_is_derived_and_survives_legacy_omission(desk):
    result = enrich(desk)
    assert result["decision_enrichment"]["oct16"]["status"] == "success"
    result = desk.publish(
        "demo", result["revision"], result["observation"], result["review"], result["decisions"]
    )
    assert result["decision_enrichment"]["oct16"]["status"] == "success"
    assert result["decision_details"]["oct16"]
    with desk._connect() as db:
        data = json.loads(db.execute("SELECT data FROM desks WHERE name='demo'").fetchone()[0])
    assert "decision_enrichment" not in data and "decision_details" not in data
    assert set(result["decision_enrichment"]["oct16"]) == {"status", "checked_at"}
    changed = desk.publish(
        "demo", result["revision"], result["observation"], result["review"], [core("Changed ask?")]
    )
    assert changed["decision_enrichment"] == {}


def test_claimed_attempt_exposes_in_progress_without_raw_internal_data(desk):
    assert desk.claim_decision_enrichment(
        "demo", "oct16", decisions.core_fingerprint(core()), "context-1"
    )
    progress = desk.get("demo")["decision_enrichment"]["oct16"]
    assert progress["status"] == "in_progress"
    assert set(progress) == {"status", "checked_at"}


def test_interrupted_claim_expires_despite_polls_and_needs_manual_retry(desk, monkeypatch):
    from ai_sessions.desk import store as storage

    clock = [1000.0]
    monkeypatch.setattr(storage.time, "time", lambda: clock[0])
    fingerprint = decisions.core_fingerprint(core())
    token = desk.claim_decision_enrichment("demo", "oct16", fingerprint, "context-1")
    assert token
    clock[0] += decisions.ENRICHMENT_LEASE_SECONDS - 1
    assert not desk.claim_decision_enrichment("demo", "oct16", fingerprint, "context-1")
    clock[0] += 2
    assert desk.get("demo")["decision_enrichment"]["oct16"]["status"] == "failed"
    assert not desk.claim_decision_enrichment("demo", "oct16", fingerprint, "context-1")
    assert not desk.claim_decision_enrichment("demo", "oct16", fingerprint, "new-context")
    retry = desk.claim_decision_enrichment("demo", "oct16", fingerprint, "context-1", force=True)
    assert retry and retry != token


def test_force_cannot_duplicate_active_model_even_for_a_different_context(desk):
    fingerprint = decisions.core_fingerprint(core())
    assert desk.claim_decision_enrichment("demo", "oct16", fingerprint, "context-1")
    calls = []
    result = decisions.enrich_pending(
        desk,
        "demo",
        context_runner=lambda *a: dict(context(), fingerprint="new-context"),
        runner=lambda p: calls.append(p) or json.dumps(details()),
        force=True,
    )
    assert calls == []
    assert result["decision_enrichment"]["oct16"]["status"] == "in_progress"


def test_expired_owner_cannot_publish_or_finish_retried_claim(desk, monkeypatch):
    from ai_sessions.desk import store as storage

    clock = [1000.0]
    monkeypatch.setattr(storage.time, "time", lambda: clock[0])
    fingerprint = decisions.core_fingerprint(core())
    first = desk.claim_decision_enrichment("demo", "oct16", fingerprint, "context-1")
    clock[0] += decisions.ENRICHMENT_LEASE_SECONDS + 1
    retry = desk.claim_decision_enrichment("demo", "oct16", fingerprint, "context-1", force=True)
    assert retry != first
    record = desk.get("demo")
    value = {**details(), "evidence": context()["evidence"]}
    with pytest.raises(ConflictError):
        desk.publish_decision_details(
            "demo",
            record["revision"],
            "oct16",
            fingerprint,
            "context-1",
            value,
            attempt_token=first,
        )
    desk.finish_decision_enrichment("demo", "oct16", fingerprint, "context-1", attempt_token=first)
    assert desk.get("demo")["decision_enrichment"]["oct16"]["status"] == "in_progress"
    result = desk.publish_decision_details(
        "demo", record["revision"], "oct16", fingerprint, "context-1", value, attempt_token=retry
    )
    assert result["decision_enrichment"]["oct16"]["status"] == "success"


def test_simultaneous_force_claims_have_only_one_owner(desk):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    start = Barrier(2)
    fingerprint = decisions.core_fingerprint(core())

    def claim(index):
        start.wait(timeout=5)
        return desk.claim_decision_enrichment(
            "demo", "oct16", fingerprint, f"context-{index}", force=True
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (1, 2)))
    assert sum(bool(token) for token in results) == 1


def test_legacy_unowned_claim_reads_failed_and_migrates_for_manual_retry(desk):
    fingerprint = decisions.core_fingerprint(core())
    with desk._connect() as db:
        db.execute("ALTER TABLE decision_enrichment_attempts DROP COLUMN started_at")
        db.execute("ALTER TABLE decision_enrichment_attempts DROP COLUMN attempt_token")
        db.execute("ALTER TABLE decision_enrichment_attempts DROP COLUMN retry_count")
        db.execute(
            "INSERT INTO decision_enrichment_attempts VALUES (?,?,?,?,?,?,?,?)",
            ("demo", "oct16", fingerprint, "context-1", 1, "claimed", 9999999999, "old"),
        )
    assert (
        Store(desk.root, readonly=True).get("demo")["decision_enrichment"]["oct16"]["status"]
        == "failed"
    )
    migrated = Store(desk.root)
    assert not migrated.claim_decision_enrichment("demo", "oct16", fingerprint, "context-1")
    assert migrated.claim_decision_enrichment("demo", "oct16", fingerprint, "context-1", force=True)


def test_cancelled_owned_model_attempt_exposes_failed_for_manual_retry(desk):
    from ai_sessions.harnesses.desk_observer import ObservationCancelled

    def cancelled(prompt):
        raise ObservationCancelled()

    with pytest.raises(ObservationCancelled):
        enrich(desk, runner=cancelled)
    assert desk.get("demo")["decision_enrichment"]["oct16"]["status"] == "failed"


def test_force_prioritizes_failed_attempt_even_if_previous_details_were_ready(desk, monkeypatch):
    ready = details()
    ready.update(readiness="ready", missing_information=[])
    enrich(desk, runner=lambda p: json.dumps(ready))
    record = desk.get("demo")
    other = dict(core(), key="other")
    desk.publish(
        "demo", record["revision"], record["observation"], record["review"], [core(), other]
    )
    enrich(desk, runner=lambda p: json.dumps(ready))
    token = desk.claim_decision_enrichment(
        "demo", "other", decisions.core_fingerprint(other), "failed-new-context"
    )
    assert token
    desk.finish_decision_enrichment(
        "demo",
        "other",
        decisions.core_fingerprint(other),
        "failed-new-context",
        attempt_token=token,
    )
    monkeypatch.setattr(decisions, "MAX_PER_PASS", 1)
    selected = []
    decisions.enrich_pending(
        desk,
        "demo",
        force=True,
        context_runner=lambda r, d: selected.append(d["key"]) or context(),
        runner=lambda p: json.dumps(ready),
    )
    assert selected == ["other"]


def test_old_database_without_sidecars_exposes_empty_metadata(desk):
    with desk._connect() as db:
        db.execute("DROP TABLE decision_enrichment_attempts")
        db.execute("DROP TABLE decision_details")
    saved = Store(desk.root, readonly=True).get("demo")
    assert saved["decision_enrichment"] == {} and saved["decision_details"] == {}


def test_changed_recommendation_invalidates_details_and_cached_attempt(desk):
    result = enrich(desk)
    updated = dict(core(), recommendation="Do not run until the new risk is addressed.")
    result = desk.publish(
        "demo", result["revision"], result["observation"], result["review"], [updated]
    )
    assert result["decision_details"] == {} and result["decision_enrichment"] == {}
    calls = []
    result = enrich(desk, runner=lambda p: calls.append(p) or json.dumps(details()))
    assert len(calls) == 1 and "oct16" in result["decision_details"]


def test_force_prioritizes_missing_or_failed_before_ready_but_can_retry_ready(desk, monkeypatch):
    ready = details()
    ready.update(readiness="ready", missing_information=[])
    enrich(desk, runner=lambda p: json.dumps(ready))
    record = desk.get("demo")
    missing = dict(core(), key="missing", question="Missing context question?")
    desk.publish(
        "demo", record["revision"], record["observation"], record["review"], [core(), missing]
    )
    monkeypatch.setattr(decisions, "MAX_PER_PASS", 1)
    calls = []

    def model(prompt):
        calls.append(json.loads(prompt[len(decisions.SYSTEM) + 1 :])["decision"]["key"])
        return json.dumps(ready)

    result = decisions.enrich_pending(
        desk, "demo", context_runner=lambda *a: context(), runner=model, force=True
    )
    assert calls == ["missing"]
    assert result["decision_enrichment"]["missing"]["status"] == "success"
    calls.clear()
    decisions.enrich_pending(
        desk, "demo", context_runner=lambda *a: context(), runner=model, force=True
    )
    assert len(calls) == 1  # Both are now ready; explicit force still refreshes one.


def test_enrichment_claim_prevents_concurrent_or_crashed_duplicate(desk):
    fingerprint = decisions.core_fingerprint(core())
    assert desk.claim_decision_enrichment("demo", "oct16", fingerprint, "context-1")
    calls = []
    enrich(desk, runner=lambda p: calls.append(p) or json.dumps(details()))
    assert calls == []


def test_stale_core_cannot_publish_enrichment(desk):
    before = desk.get("demo")
    fingerprint = decisions.core_fingerprint(core())
    desk.publish("demo", before["revision"], before["observation"], "New", [core("Different ask?")])
    with pytest.raises(ConflictError):
        desk.publish_decision_details(
            "demo",
            before["revision"],
            "oct16",
            fingerprint,
            "context-1",
            {**details(), "evidence": context()["evidence"]},
        )
    assert desk.get("demo")["decision_details"] == {}


@pytest.mark.parametrize(
    "mutation", ["unknown_citation", "fake_path", "resolution", "false_ready", "option"]
)
def test_model_output_cannot_add_paths_status_or_unsupported_fields(desk, mutation):
    output = details()
    if mutation == "unknown_citation":
        output["citations"]["summary"] = ["made-up"]
    elif mutation == "fake_path":
        output["evidence"] = [dict(storage="/secret")]
    elif mutation == "resolution":
        output["status"] = "resolved"
    elif mutation == "false_ready":
        output["readiness"] = "ready"
    else:
        output["recommendation"]["option_id"] = "nonexistent"
    assert enrich(desk, runner=lambda p: json.dumps(output))["decision_details"] == {}
    assert desk.get("demo")["decisions"][0]["status"] == "pending"


def test_enrichment_never_creates_notification_or_resolves_historical_request(desk):

    with desk._connect() as db:
        before = db.execute("SELECT COUNT(*) FROM desk_notification_events").fetchone()[0]
    result = enrich(desk)
    assert result["decisions"][0]["status"] == "pending"
    with desk._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM desk_notification_events").fetchone()[0] == before


def test_markdown_export_has_same_revision_and_sanitized_details(desk):
    output = details()
    output["summary"] = "Run test?\n# injected heading"
    result = enrich(desk, runner=lambda p: json.dumps(output))
    paths = desk.export("demo")
    projected = paths["decisions"].read_text(encoding="utf-8")
    assert f"revision: {result['revision']}" in projected
    assert "Trading request" in projected
    assert "\n# injected heading" not in projected


def test_manual_force_retries_cached_failure_once(desk):
    calls = []
    decisions.enrich_pending(
        desk, "demo", context_runner=lambda *a: context(), runner=lambda p: calls.append(p) or "bad"
    )
    result = decisions.enrich_pending(
        desk,
        "demo",
        context_runner=lambda *a: context(),
        runner=lambda p: calls.append(p) or json.dumps(details()),
        force=True,
    )
    assert len(calls) == 2
    assert "oct16" in result["decision_details"]


def test_ready_details_reuse_without_reading_context_until_ask_changes(desk):
    supplied = context()
    supplied["evidence"][0]["context_only"] = False
    output = details()
    output.update(
        readiness="ready",
        background="The trading project needs a test.",
        why_now="The operator requested this test.",
        missing_information=[],
    )
    output["citations"].update(background=["d0"], why_now=["d0"], recommendation=["d0"])
    enrich(desk, supplied=supplied, runner=lambda p: json.dumps(output))
    decisions.enrich_pending(
        desk, "demo", context_runner=lambda *a: pytest.fail("ready rehydration")
    )


def test_cached_unknowns_do_not_starve_later_pending_items(desk, monkeypatch):
    record = desk.get("demo")
    cores = [dict(core(), key=f"q{i}", question=f"Question {i}?") for i in range(5)]
    desk.publish("demo", record["revision"], record["observation"], "Review", cores)
    calls = []
    for _ in range(3):
        decisions.enrich_pending(
            desk,
            "demo",
            context_runner=lambda r, d: calls.append(d["key"]) or context(),
            runner=lambda p: json.dumps(details()),
        )
    assert set(calls) == {"oct16", *(f"q{i}" for i in range(5))}
    assert len(calls) == 6


def test_harmless_poll_revision_does_not_discard_matching_core_and_source(desk):
    def model(prompt):
        current = desk.get("demo")
        observation = copy.deepcopy(current["observation"])
        observation["observed_at"] = 123456
        desk.publish(
            "demo", current["revision"], observation, current["review"], current["decisions"]
        )
        return json.dumps(details())

    assert "oct16" in enrich(desk, runner=model)["decision_details"]


def test_source_change_during_model_cannot_publish_stale_context(desk):
    def model(prompt):
        current = desk.get("demo")
        desk.publish(
            "demo",
            current["revision"],
            dict(
                sources=[
                    dict(
                        tool="codex",
                        session_id="native",
                        storage="/pinned/source.jsonl",
                        fingerprint=[1, 2, 300, 900],
                    )
                ]
            ),
            "New evidence",
            current["decisions"],
        )
        return json.dumps(details())

    assert enrich(desk, runner=model)["decision_details"] == {}


def test_unpinned_context_is_not_sent_to_model(desk):
    supplied = context()
    supplied["evidence"][0]["storage"] = "/unregistered/private.jsonl"
    assert (
        enrich(desk, supplied=supplied, runner=lambda p: pytest.fail("unpinned context"))[
            "decision_details"
        ]
        == {}
    )


def test_context_error_negative_cache_ignores_observation_clock(desk):
    def unavailable(*args):
        raise OSError("private error")

    decisions.enrich_pending(desk, "demo", context_runner=unavailable)
    current = desk.get("demo")
    desk.publish("demo", current["revision"], dict(sources=[], observed_at=123), "Review", [core()])
    decisions.enrich_pending(desk, "demo", context_runner=unavailable)
    with desk._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM decision_enrichment_attempts").fetchone()[0] == 1


def test_observer_migrates_pending_details_even_when_core_sources_unchanged(desk, monkeypatch):
    from ai_sessions.desk import observer

    before = desk.get("demo")
    monkeypatch.setattr(observer, "_refresh", lambda *a, **kw: before)
    calls = []
    result = observer.refresh(
        desk,
        "demo",
        runner=lambda p: pytest.fail("core model call"),
        context_runner=lambda *a: context(),
        details_runner=lambda p: calls.append(p) or json.dumps(details()),
    )
    assert "oct16" in result["decision_details"]
    observer.refresh(
        desk,
        "demo",
        runner=lambda p: pytest.fail("core model call"),
        context_runner=lambda *a: context(),
        details_runner=lambda p: calls.append(p) or json.dumps(details()),
    )
    assert len(calls) == 1


def test_original_information_request_can_correct_lossy_run_question_without_editing_core(desk):
    supplied = context()
    supplied["evidence"][0]["text"] = (
        "Result files exist for October 16, but the log says NOT RUN awaiting approval. "
        "Did you approve it then? If approved, read and record the result; otherwise record "
        "that 2026 data was already viewed and is not a clean holdout."
    )
    corrected = details()
    corrected.update(kind="information", summary="Was the October 16 test approved at the time?")
    corrected["missing_information"] = ["Whether you approved the test then."]

    def model(prompt):
        assert "LOSSY" in prompt and "false premise" in prompt
        assert "Existing result files do not prove prior approval" in prompt
        assert supplied["evidence"][0]["text"] in prompt
        return json.dumps(corrected)

    result = enrich(desk, supplied=supplied, runner=model)
    assert result["decisions"][0] == core()
    assert result["decision_details"]["oct16"]["kind"] == "information"
    assert result["decision_details"]["oct16"]["summary"].startswith("Was the October")


def test_ready_means_context_understood_not_that_human_answer_is_known(desk):
    output = details()
    output.update(
        kind="information",
        readiness="ready",
        missing_information=[],
        summary="Did you previously approve the October 16 test?",
    )
    output["recommendation"]["reason"] = "Please say whether you approved it previously."
    result = enrich(desk, runner=lambda p: json.dumps(output))
    assert result["decision_details"]["oct16"]["readiness"] == "ready"
    assert result["decisions"][0]["status"] == "pending"
    assert all(e["context_only"] for e in result["decision_details"]["oct16"]["evidence"])


def test_base_prompt_references_canonical_ask_without_importing_deep_details(desk):
    from ai_sessions.desk import observer

    output = details()
    output["summary"] = "Did you previously approve the October 16 test?"
    record = enrich(desk, runner=lambda p: json.dumps(output))
    record["decision_details"]["oct16"]["evidence"][0]["text"] = "DEEP_CONTEXT_NOT_FOR_BASE_PROMPT"
    coverage = [dict(id="s0", status="available", changed=True)]
    prompt = observer._build_prompt(
        record, coverage, [dict(citation="s0", text="Latest source text")]
    )
    payload = json.loads(prompt[len(observer.SYSTEM) + 1 :])
    assert payload["canonical_decision_refs"] == [
        dict(key="oct16", summary=output["summary"], readiness="needs_context")
    ]
    assert payload["previous_decisions"] == [core()]
    assert "DEEP_CONTEXT_NOT_FOR_BASE_PROMPT" not in prompt
    assert "NOT new evidence" in observer.SYSTEM


def test_canonical_reference_budget_uses_bounded_window_and_keeps_current_text(desk):
    from ai_sessions.desk import observer

    record = desk.get("demo")
    record["decisions"] = [dict(core(), key=f"q{i}") for i in range(80)]
    record["decision_details"] = {
        f"q{i}": dict(summary="Question " + "x" * 580, readiness="ready") for i in range(80)
    }
    coverage = [dict(id="s0", status="available", changed=True)]
    prompt = observer._build_prompt(
        record, coverage, [dict(citation="s0", text="Latest evidence " * 1000)]
    )
    payload = json.loads(prompt[len(observer.SYSTEM) + 1 :])
    assert len(payload["previous_decisions"]) <= 20
    assert payload["pending_total"] == 80
    assert len(record["decisions"]) == 80
    assert (
        len(json.dumps(payload["canonical_decision_refs"], separators=(",", ":")))
        <= observer.MAX_CANONICAL_REF_CHARS
    )
    assert payload["evidence"][0]["text"]
    assert len(prompt) <= observer.MAX_PROMPT_CHARS


def test_default_model_uses_details_schema_and_existing_tools_disabled_backend(monkeypatch):
    import subprocess
    import sys

    from ai_sessions.harnesses import desk_observer

    original = subprocess.Popen
    commands = []
    envelope = json.dumps({"structured_output": details()})

    def spawn(command, **kwargs):
        commands.append(command)
        return original(
            [sys.executable, "-c", f"import sys; sys.stdin.read(); print({envelope!r})"], **kwargs
        )

    monkeypatch.setattr(desk_observer.shutil, "which", lambda name: sys.executable)
    monkeypatch.setattr(desk_observer.subprocess, "Popen", spawn)
    assert json.loads(decisions._run_model("fixture only", ["d0"], None))["kind"] == "choice"
    command = commands[0]
    schema = json.loads(command[command.index("--json-schema") + 1])
    assert schema["additionalProperties"] is False
    assert "evidence" not in schema["properties"] and "status" not in schema["properties"]
    assert schema["properties"]["citations"]["properties"]["summary"]["items"]["enum"] == ["d0"]
    assert command[command.index("--tools") + 1] == ""
    assert "--safe-mode" in command and "--strict-mcp-config" in command


def test_failed_background_retries_are_bounded_and_explicit_retry_resets_budget(desk, monkeypatch):
    import time

    fingerprint = decisions.core_fingerprint(core())
    clock = [time.time()]
    monkeypatch.setattr("ai_sessions.desk.store.time.time", lambda: clock[0])

    def fail():
        token = desk.claim_decision_enrichment("demo", "oct16", fingerprint, "context-1")
        assert token
        desk.finish_decision_enrichment(
            "demo", "oct16", fingerprint, "context-1", attempt_token=token
        )

    fail()
    assert not desk.claim_decision_enrichment("demo", "oct16", fingerprint, "context-1")
    clock[0] += 61
    fail()
    clock[0] += 301
    fail()
    clock[0] += 1000
    assert not desk.claim_decision_enrichment("demo", "oct16", fingerprint, "context-1")
    desk.retry_decision_background("demo", "oct16", fingerprint)
    assert desk.get("demo")["decision_enrichment"]["oct16"]["status"] == "in_progress"
    assert desk.claim_decision_enrichment("demo", "oct16", fingerprint, "context-1")
