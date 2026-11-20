import json

import pytest

from ai_sessions.desk import observer
from ai_sessions.desk.store import Store
from ai_sessions.model import ReadSnapshot, Transcript, Turn


def setup_source(tmp_path, monkeypatch):
    path = tmp_path / "fiction.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    store = Store(tmp_path / "desk")
    store.create("demo", [{"tool": "claude", "session_id": "fiction", "storage": str(path)}], "x")
    monkeypatch.setattr(
        observer,
        "read_since",
        lambda *a, **kw: ReadSnapshot(
            Transcript([Turn("user", "Choose a color?")]), path.stat().st_size
        ),
    )
    return store, path


def output(decisions=None):
    return json.dumps({"review": "Recent source activity [s0]", "decisions": decisions or []})


def decision(status="pending"):
    return {
        "key": "color",
        "question": "Which color?",
        "context": "Palette",
        "recommendation": "Blue",
        "status": status,
        "evidence": ["s0"],
    }


def test_refresh_incremental_and_no_unchanged_model_call(tmp_path, monkeypatch):
    store, path = setup_source(tmp_path, monkeypatch)
    calls = []
    result = observer.refresh(
        store, "demo", runner=lambda p: calls.append(p) or output([decision()])
    )
    assert result["revision"] == 1 and result["decisions"][0]["status"] == "pending"
    assert result["observation"]["sources"][0]["checkpoint"] == path.stat().st_size
    observer.refresh(store, "demo", runner=lambda p: pytest.fail("unchanged model call"))
    assert len(calls) == 1
    path.write_text("{}\n{}\n", encoding="utf-8")
    result = observer.refresh(store, "demo", runner=lambda p: output())
    assert result["decisions"][0]["status"] == "pending"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "{}",
        "not json",
        '{"review": [], "decisions": []}',
        '{"review": "fine", "decisions": [{}]}',
    ],
)
def test_invalid_model_preserves_last_good_and_cursor(tmp_path, monkeypatch, raw):
    store, path = setup_source(tmp_path, monkeypatch)
    good = observer.refresh(store, "demo", runner=lambda p: output([decision()]))
    path.write_text("{}\n{}\n", encoding="utf-8")
    failed = observer.refresh(store, "demo", runner=lambda p: raw)
    assert failed["error"]
    for key in ("review", "decisions", "observation", "observed_at"):
        assert failed[key] == good[key]


def test_partial_current_cited_resolution_can_resolve_pending(tmp_path, monkeypatch):
    store, path = setup_source(tmp_path, monkeypatch)
    good = observer.refresh(store, "demo", runner=lambda p: output([decision()]))
    monkeypatch.setattr(observer, "MAX_SOURCE_BYTES", 6)
    path.write_text("{}\n" * 12, encoding="utf-8")
    result = observer.refresh(store, "demo", runner=lambda p: output([decision("resolved")]))
    assert result["decisions"][0]["status"] == "resolved"
    assert result["observation"]["partial"]
    assert result["error"] is None
    assert (
        result["observation"]["sources"][0]["checkpoint"]
        > good["observation"]["sources"][0]["checkpoint"]
    )


def test_stale_completion_refuses_overwrite(tmp_path, monkeypatch):
    store, _ = setup_source(tmp_path, monkeypatch)

    def runner(prompt):
        store.publish("demo", 0, {}, "winner", [])
        return output()

    result = observer.refresh(store, "demo", runner=runner)
    assert result["review"] == "winner" and result["revision"] == 1


def test_unavailable_does_not_resolve(tmp_path, monkeypatch):
    store, path = setup_source(tmp_path, monkeypatch)
    good = observer.refresh(store, "demo", runner=lambda p: output([decision()]))
    path.unlink()
    result = observer.refresh(store, "demo", runner=lambda p: pytest.fail("no usable sources"))
    assert result["error"] and result["decisions"] == good["decisions"]


def test_real_claude_read_and_later_explicit_resolution(tmp_path):
    path = tmp_path / "fiction.jsonl"

    def append(role, text):
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": role, "message": {"content": text}}) + "\n")

    append("user", "Choose a color?")
    store = Store(tmp_path / "desk")
    store.create("demo", [{"tool": "claude", "session_id": "fiction", "storage": str(path)}], "x")
    first = observer.refresh(store, "demo", runner=lambda p: output([decision()]))
    assert first["decisions"][0]["status"] == "pending"
    append("user", "Decision: use blue. This resolves the color question.")
    prompts = []
    second = observer.refresh(
        store, "demo", runner=lambda p: prompts.append(p) or output([decision("resolved")])
    )
    assert second["decisions"][0]["status"] == "resolved"
    payload = json.loads(prompts[0].split("\n", 1)[1])
    assert "Choose a color?" not in payload["evidence"][0]["text"]
    assert "resolves the color" in payload["evidence"][0]["text"]
    assert "bytes" in second["decisions"][0]["evidence"][0]


def test_empty_source_skips_model(tmp_path):
    path = tmp_path / "fiction.jsonl"
    path.touch()
    store = Store(tmp_path / "desk")
    store.create("demo", [{"tool": "claude", "session_id": "fiction", "storage": str(path)}], "x")
    result = observer.refresh(store, "demo", runner=lambda p: pytest.fail("empty model call"))
    assert result["error"] is None and result["decisions"] == []


@pytest.mark.parametrize("mode", ["stdout", "stderr", "timeout", "exit", "empty", "error"])
def test_model_subprocess_limits_and_error_envelopes(monkeypatch, mode):
    import subprocess
    import sys

    original = subprocess.Popen
    commands = []
    code = {
        "stdout": "import sys; sys.stdout.write('x'*10000)",
        "stderr": "import sys; sys.stderr.write('x'*10000)",
        "timeout": "import time; time.sleep(5)",
        "exit": "raise SystemExit(3)",
        "empty": "pass",
        "error": 'print(\'{"is_error": true, "result": "private detail"}\')',
    }[mode]

    def spawn(command, **kwargs):
        commands.append(command)
        return original([sys.executable, "-c", code], **kwargs)

    monkeypatch.setattr(observer.model_backend.shutil, "which", lambda name: sys.executable)
    monkeypatch.setattr(observer.model_backend.subprocess, "Popen", spawn)
    monkeypatch.setattr(observer, "MAX_OUTPUT_BYTES", 512)
    monkeypatch.setattr(observer, "MODEL_TIMEOUT", 0.3 if mode == "timeout" else 5)
    with pytest.raises((ValueError, RuntimeError)):
        observer._run_model("fictional data only")
    command = commands[0]
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--strict-mcp-config" in command and "--safe-mode" in command
    assert "--disable-slash-commands" in command and "--bare" not in command
    assert not any("bypass" in arg or "skip-permissions" in arg for arg in command)


def test_model_subprocess_valid_envelope(monkeypatch):
    import subprocess
    import sys

    original = subprocess.Popen
    envelope = json.dumps({"result": output()})

    def spawn(command, **kwargs):
        return original(
            [sys.executable, "-c", f"import sys; sys.stdin.read(); print({envelope!r})"], **kwargs
        )

    monkeypatch.setattr(observer.model_backend.shutil, "which", lambda name: sys.executable)
    monkeypatch.setattr(observer.model_backend.subprocess, "Popen", spawn)
    assert json.loads(observer._run_model("fictional data only"))["review"]


def test_unknown_citation_and_runner_error_preserve_snapshot(tmp_path, monkeypatch):
    store, _ = setup_source(tmp_path, monkeypatch)

    def broken(prompt):
        raise RuntimeError("sensitive raw model text")

    failed = observer.refresh(store, "demo", runner=broken)
    assert "sensitive" not in failed["error"] and failed["observation"] == {}
    bad = decision()
    bad["evidence"] = ["s99"]
    failed = observer.refresh(store, "demo", runner=lambda p: output([bad]))
    assert failed["decisions"] == [] and failed["observed_at"] is None


def test_unchanged_source_does_not_read_history_again(tmp_path, monkeypatch):
    store, path = setup_source(tmp_path, monkeypatch)
    observer.refresh(store, "demo", runner=lambda p: output())
    from pathlib import Path

    original = Path.open

    def checked_open(self, *args, **kwargs):
        if self == path:
            pytest.fail("unchanged transcript was reopened")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    observer.refresh(store, "demo", runner=lambda p: pytest.fail("unchanged model call"))


def test_invented_review_citation_rejected(tmp_path, monkeypatch):
    store, _ = setup_source(tmp_path, monkeypatch)
    result = observer.refresh(
        store,
        "demo",
        runner=lambda p: json.dumps(
            {"review": "Claims [s0] and fabricated evidence [s99]", "decisions": []}
        ),
    )
    assert result["error"] and result["observed_at"] is None


def test_long_incomplete_record_never_becomes_checkpoint(tmp_path, monkeypatch):
    store, path = setup_source(tmp_path, monkeypatch)
    monkeypatch.setattr(observer, "MAX_SOURCE_BYTES", 16)
    path.write_bytes(b"x" * 64)
    monkeypatch.setattr(observer, "read_since", lambda *a, **kw: ReadSnapshot(Transcript([]), 0))
    result = observer.refresh(store, "demo", runner=lambda p: pytest.fail("no complete records"))
    assert result["observation"]["sources"][0]["checkpoint"] == 0
    assert result["observation"]["partial"]


def test_stale_failure_cannot_mark_newer_success_failed(tmp_path, monkeypatch):
    store, _ = setup_source(tmp_path, monkeypatch)

    def runner(prompt):
        store.publish("demo", 0, {"observed_at": 123}, "winner", [])
        raise RuntimeError("older model failed")

    result = observer.refresh(store, "demo", runner=runner)
    assert result["review"] == "winner" and result["error"] is None


def test_prompt_bound_preserves_snapshot(tmp_path, monkeypatch):
    store, _ = setup_source(tmp_path, monkeypatch)
    monkeypatch.setattr(observer, "MAX_PROMPT_CHARS", 20)
    result = observer.refresh(store, "demo", runner=lambda p: pytest.fail("oversize model prompt"))
    assert result["error"] and result["observation"] == {}


def test_source_reset_rebuilds_bounded_tail_and_preserves_pending(tmp_path, monkeypatch):
    store, path = setup_source(tmp_path, monkeypatch)
    observer.refresh(store, "demo", runner=lambda p: output([decision()]))
    path.write_text('{"changed":true}\n', encoding="utf-8")
    result = observer.refresh(store, "demo", runner=lambda p: output())
    assert result["observation"]["partial"]
    assert result["decisions"][0]["status"] == "pending"


def test_refresh_cancelled_does_not_publish_or_fail(tmp_path, monkeypatch):
    import threading

    store, _ = setup_source(tmp_path, monkeypatch)
    stop = threading.Event()
    original = store.get("demo")
    stop.set()
    result = observer.refresh(
        store, "demo", stop=stop, runner=lambda p: pytest.fail("cancelled runner invoked")
    )
    assert result == original
    stop.clear()

    def runner(prompt):
        stop.set()
        return output()

    assert observer.refresh(store, "demo", stop=stop, runner=runner) == original


def test_cancelling_subprocess_reaps_owned_tree(monkeypatch):
    import subprocess
    import sys
    import threading
    import time

    import psutil

    original = subprocess.Popen
    stop = threading.Event()
    launched = []
    children = []
    code = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); time.sleep(30)"
    )

    def spawn(command, **kwargs):
        process = original([sys.executable, "-c", code], **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(observer.model_backend.shutil, "which", lambda name: sys.executable)
    monkeypatch.setattr(observer.model_backend.subprocess, "Popen", spawn)

    def cancel():
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if launched:
                children.extend(psutil.Process(launched[0].pid).children())
                if children:
                    break
            stop.wait(0.01)
        stop.set()

    thread = threading.Thread(target=cancel)
    thread.start()
    started = time.monotonic()
    with pytest.raises(observer.ObservationCancelled):
        observer._run_model("fictional", stop=stop)
    thread.join(timeout=1)
    assert launched[0].poll() is not None
    assert children, "fixture child must have started to verify tree cleanup"
    _, alive = psutil.wait_procs(children, timeout=2)
    assert not alive
    assert time.monotonic() - started < 6


def test_failed_attempt_coverage_separate_from_last_good(tmp_path, monkeypatch):
    store, path = setup_source(tmp_path, monkeypatch)
    good = observer.refresh(store, "demo", runner=lambda p: output([decision()]))
    path.unlink()
    failed = observer.refresh(store, "demo", runner=lambda p: pytest.fail("unavailable model"))
    assert failed["observed_at"] == good["observed_at"]
    assert failed["observation"] == good["observation"]
    assert failed["attempted_coverage"][0]["status"] == "unavailable"
    assert failed["attempted_at"] >= good["observed_at"]
    text = store.export("demo")["review"].read_text(encoding="utf-8")
    from ai_sessions.desk.store import _when

    assert "unavailable" in text and _when(good["observed_at"]) in text


def test_many_sources_share_serialized_prompt_budget(tmp_path, monkeypatch):
    store = Store(tmp_path / "desk")
    sources = [
        {
            "tool": "claude",
            "session_id": f"fiction-{i}",
            "storage": f"{i}.jsonl",
            "role": "supervisor" if i == 0 else "worker",
        }
        for i in range(32)
    ]
    store.create("demo", sources, "fiction")
    pending = decision()
    pending["context"] = "original pending context " * 200
    store.publish("demo", 0, {}, "old review " * 5000, [pending])

    def source(ref, old, index):
        return dict(
            ref,
            id=f"s{index}",
            status="available",
            changed=True,
            partial=False,
            checkpoint=123,
            start=0,
            source_modified_at=1,
            observed_at=2,
        ), ('\\"\n' * 4000 + f"latest answer {index}")

    monkeypatch.setattr(observer, "_source", source)
    prompts = []
    result = observer.refresh(store, "demo", runner=lambda p: prompts.append(p) or output())
    assert result["error"] is None
    assert len(prompts[0]) <= observer.MAX_PROMPT_CHARS
    payload = json.loads(prompts[0].split("\n", 1)[1])
    assert len(payload["evidence"]) == 32
    assert all(e["text"].endswith(f"latest answer {i}") for i, e in enumerate(payload["evidence"]))
    assert all(s["partial"] and s["prompt_clipped"] for s in payload["coverage"])
    assert payload["previous_decisions"][0] == pending
    assert payload["previous_review_partial"]
    assert payload["primary_source"]["session_id"] == "fiction-0"
    assert result["decisions"] == [pending]
    assert result["observation"]["partial"]


def test_unfit_pending_decisions_fail_without_call(tmp_path, monkeypatch):
    store, _ = setup_source(tmp_path, monkeypatch)
    pending = decision()
    pending["context"] = "x" * 4000
    good = store.publish("demo", 0, {}, "good", [pending])
    monkeypatch.setattr(observer, "MAX_PROMPT_CHARS", 2000)
    result = observer.refresh(store, "demo", runner=lambda p: pytest.fail("oversize prompt"))
    assert result["error"] and result["decisions"] == good["decisions"]
    assert result["observation"] == good["observation"]


def test_observer_prompt_requires_human_action_and_primary_objective():
    assert "HUMAN" in observer.SYSTEM
    assert "already granted" in observer.SYSTEM
    assert "routine" in observer.SYSTEM
    assert "current objective" in observer.SYSTEM


def test_refresh_exports_success_and_failure_automatically(tmp_path, monkeypatch):
    store, path = setup_source(tmp_path, monkeypatch)
    observer.refresh(store, "demo", runner=lambda p: output())
    review = store.folder("demo") / "review.md"
    assert "Recent source activity" in review.read_text(encoding="utf-8")
    path.unlink()
    observer.refresh(store, "demo", runner=lambda p: pytest.fail("no model"))
    assert "Stale / error" in review.read_text(encoding="utf-8")


def test_unrelated_unavailable_source_does_not_block_resolution(tmp_path, monkeypatch):
    store, path = setup_source(tmp_path, monkeypatch)
    good = observer.refresh(store, "demo", runner=lambda p: output([decision()]))
    source = good["sources"][0]
    # Fictional second desk models an additional explicitly chosen unavailable source.
    store.create("team", [source, dict(source, session_id="missing", storage="missing.jsonl")], "x")
    store.publish("team", 0, good["observation"], good["review"], good["decisions"])
    path.write_text("{}\n{}\n", encoding="utf-8")
    result = observer.refresh(store, "team", runner=lambda p: output([decision("resolved")]))
    assert result["decisions"][0]["status"] == "resolved"
    assert result["observation"]["partial"]


def test_unchanged_decision_can_keep_exact_historical_evidence(tmp_path, monkeypatch):
    store, path = setup_source(tmp_path, monkeypatch)
    good = observer.refresh(store, "demo", runner=lambda p: output([decision()]))
    path.write_text("{}\n{}\n", encoding="utf-8")
    result = observer.refresh(store, "demo", runner=lambda p: output(good["decisions"]))
    assert result["error"] is None and result["decisions"] == good["decisions"]
    path.write_text("{}\n{}\n{}\n", encoding="utf-8")
    edited = dict(good["decisions"][0], status="resolved")
    result = observer.refresh(store, "demo", runner=lambda p: output([edited]))
    assert result["error"] and result["decisions"][0]["status"] == "pending"


@pytest.mark.parametrize("permanent", [False, True])
def test_projection_failure_after_publish_records_current_revision(
    tmp_path, monkeypatch, permanent
):
    store, _ = setup_source(tmp_path, monkeypatch)
    original = store.export
    attempts = []

    def broken(name):
        attempts.append(name)
        if permanent or len(attempts) == 1:
            raise OSError("private filesystem details")
        return original(name)

    monkeypatch.setattr(store, "export", broken)
    result = observer.refresh(store, "demo", runner=lambda p: output())
    assert result["revision"] == 2
    assert result == store.get("demo")
    assert result["review"] and result["observed_at"]
    assert "projection" in result["error"].lower()
    assert "private filesystem" not in result["error"]
    assert len(attempts) == 2
    if not permanent:
        assert "Stale / error" in (store.folder("demo") / "review.md").read_text(encoding="utf-8")


def test_projection_failure_after_observation_failure_is_reported(tmp_path, monkeypatch):
    store, _ = setup_source(tmp_path, monkeypatch)

    def broken(name):
        raise OSError("private path")

    monkeypatch.setattr(store, "export", broken)
    result = observer.refresh(store, "demo", runner=lambda p: "invalid model output")
    assert "projection" in result["error"].lower()
    assert result["observed_at"] is None and result == store.get("demo")


def test_explicit_latest_answer_in_clipped_long_tail_resolves(tmp_path, monkeypatch):
    path = tmp_path / "fiction.jsonl"

    def append(text):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "user", "message": {"content": text}}) + "\n")

    append("Which color should we use?")
    store = Store(tmp_path / "desk")
    store.create("demo", [{"tool": "claude", "session_id": "fiction", "storage": str(path)}], "x")
    observer.refresh(store, "demo", runner=lambda p: output([decision()]))
    for _ in range(20):
        append("Earlier routine progress. " * 20)
    append("Andrew's decision: use blue. The color question is resolved.")
    monkeypatch.setattr(observer, "MAX_SOURCE_BYTES", 1000)
    prompts = []
    result = observer.refresh(
        store, "demo", runner=lambda p: prompts.append(p) or output([decision("resolved")])
    )
    assert result["error"] is None and result["decisions"][0]["status"] == "resolved"
    assert result["observation"]["partial"]
    payload = json.loads(prompts[0].split("\n", 1)[1])
    assert "color question is resolved" in payload["evidence"][0]["text"]
    assert result["observation"]["sources"][0]["checkpoint"] == path.stat().st_size


def test_excessive_synthesis_pending_preserves_last_safe_snapshot(tmp_path, monkeypatch):
    store, _ = setup_source(tmp_path, monkeypatch)
    items = [
        dict(decision(), key=f"q{i}", question=f"Question {i}?", context="x" * 7000)
        for i in range(4)
    ]
    result = observer.refresh(store, "demo", runner=lambda p: output(items))
    assert result["error"] and result["review"] == "" and result["decisions"] == []
    assert result["observation"] == {} and result["observed_at"] is None


def test_fenced_legacy_json_is_validated_without_accepting_surrounding_prose(tmp_path, monkeypatch):
    store, _ = setup_source(tmp_path, monkeypatch)
    result = observer.refresh(store, "demo", runner=lambda p: "```json\n" + output() + "\n```")
    assert result["error"] is None


def test_model_command_enforces_briefing_schema(monkeypatch):
    import subprocess
    import sys

    original = subprocess.Popen
    commands = []
    envelope = json.dumps({"structured_output": json.loads(output())})

    def spawn(command, **kwargs):
        commands.append(command)
        return original(
            [sys.executable, "-c", f"import sys; sys.stdin.read(); print({envelope!r})"], **kwargs
        )

    monkeypatch.setattr(observer.model_backend.shutil, "which", lambda name: sys.executable)
    monkeypatch.setattr(observer.model_backend.subprocess, "Popen", spawn)
    prompt = (
        observer.SYSTEM + "\n" + json.dumps({"evidence": [{"citation": "s0", "text": "Fiction"}]})
    )
    assert json.loads(observer._run_model(prompt))["review"]
    command = commands[0]
    schema = json.loads(command[command.index("--json-schema") + 1])
    assert schema["required"] == ["review", "decisions"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["decisions"]["items"]["properties"]["evidence"]["items"][
        "enum"
    ] == ["s0"]
    assert schema["properties"]["decisions"]["items"]["properties"]["status"]["enum"] == [
        "pending",
        "resolved",
        "superseded",
    ]


def test_prose_wrapped_json_is_not_silently_extracted(tmp_path, monkeypatch):
    store, _ = setup_source(tmp_path, monkeypatch)
    result = observer.refresh(store, "demo", runner=lambda p: "Here is the JSON: " + output())
    assert result["error"] and "JSONDecodeError" not in result["error"]
    assert result["observation"] == {}
