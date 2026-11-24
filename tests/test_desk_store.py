import concurrent.futures

import pytest

from ai_sessions.desk.store import ConflictError, Store

SOURCE = {"tool": "claude", "session_id": "fiction", "storage": "fiction.jsonl"}


def test_store_cas_persistence_failure_export(tmp_path):
    store = Store(tmp_path)
    first = store.create("demo", [SOURCE], "fiction")
    assert first["revision"] == 0 and first["observed_at"] is None
    current = store.publish("demo", 0, {"observed_at": 123, "sources": []}, "Review", [])
    assert current["revision"] == 1
    with pytest.raises(ConflictError):
        store.publish("demo", 0, {}, "stale", [])
    failed = store.fail("demo", "unavailable")
    assert failed["review"] == "Review" and failed["observed_at"] == 123
    assert Store(tmp_path).get("demo") == failed
    paths = store.export("demo")
    assert "unavailable" in paths["review"].read_text(encoding="utf-8")
    assert store.list()[0]["name"] == "demo"
    assert store.seen("demo")["seen_at"]


def test_concurrent_publish_only_one_wins(tmp_path):
    Store(tmp_path).create("demo", [SOURCE], "fiction")

    def publish(i):
        try:
            Store(tmp_path).publish("demo", 0, {}, str(i), [])
            return True
        except ConflictError:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(publish, range(6))) == 1
    assert Store(tmp_path).get("demo")["revision"] == 1


@pytest.mark.parametrize("name", ["../bad", "", "a/b", "CON", "a."])
def test_invalid_names(tmp_path, name):
    with pytest.raises(ValueError):
        Store(tmp_path).create(name, [SOURCE], "fiction")


def test_duplicates_missing_and_invalid_publication(tmp_path):
    store = Store(tmp_path)
    store.create("demo", [SOURCE], "fiction")
    with pytest.raises(ValueError):
        store.create("demo", [SOURCE], "fiction")
    with pytest.raises(KeyError):
        store.get("missing")
    with pytest.raises(ValueError):
        store.publish("demo", 0, {}, "", [{"status": "invented"}])
    assert store.get("demo")["revision"] == 0


def test_agent_binding_persists_and_refuses_conflicts(tmp_path):
    store = Store(tmp_path)
    store.create("demo", [SOURCE], "fiction")
    assert store.get("demo")["agents"] == {}
    bound = store.bind_agent("demo", "claude", "human-session", "fiction-home")
    assert bound["agents"]["claude"] == {"session_id": "human-session", "home": "fiction-home"}
    assert Store(tmp_path).get("demo")["agents"] == bound["agents"]
    assert store.bind_agent("demo", "claude", "human-session", "fiction-home") == bound
    with pytest.raises(ConflictError):
        store.bind_agent("demo", "claude", "different", "fiction-home")
    assert store.folder("demo") == tmp_path / "demo"
    assert store.folder("demo").is_dir()
    with pytest.raises(ValueError):
        store.folder("../escape")


def test_concurrent_agent_binding_has_one_winner(tmp_path):
    Store(tmp_path).create("demo", [SOURCE], "fiction")

    def bind(i):
        try:
            Store(tmp_path).bind_agent("demo", "codex", str(i), "fiction-home")
            return True
        except ConflictError:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(bind, range(6))) == 1


def test_case_variants_cannot_share_projection_directory(tmp_path):
    store = Store(tmp_path)
    store.create("Demo", [SOURCE], "fiction")
    with pytest.raises(ValueError):
        store.create("demo", [SOURCE], "fiction")


@pytest.mark.parametrize("revision", [True, -1, "0"])
def test_invalid_revision_rejected_without_mutation(tmp_path, revision):
    store = Store(tmp_path)
    original = store.create("demo", [SOURCE], "fiction")
    with pytest.raises(ValueError):
        store.publish("demo", revision, {}, "changed", [])
    assert store.get("demo") == original


def test_source_display_metadata_and_never_observed_export(tmp_path):
    store = Store(tmp_path)
    created = store.create("demo", [dict(SOURCE, role="supervisor", title="Fictional team")], "x")
    assert created["sources"][0]["role"] == "supervisor"
    assert created["sources"][0]["title"] == "Fictional team"
    assert "not observed" in store.export("demo")["review"].read_text(encoding="utf-8").lower()


def _pending_item(index):
    return {
        "key": f"pending-{index}",
        "question": f"Question {index}?",
        "context": "Context",
        "recommendation": "Consider",
        "status": "pending",
        "evidence": ["fiction"],
    }


@pytest.mark.parametrize("kind", ["count", "characters"])
def test_pending_budget_rejection_preserves_store(tmp_path, kind):
    store = Store(tmp_path)
    good = store.create("demo", [SOURCE], "fiction")
    decisions = [_pending_item(i) for i in range(81 if kind == "count" else 10)]
    if kind == "characters":
        for item in decisions:
            item["context"] = "x" * 7000
    with pytest.raises(ValueError, match="pending"):
        store.publish("demo", 0, {}, "oversized", decisions)
    assert store.get("demo") == good


def test_terminal_history_bounded_without_dropping_pending(tmp_path):
    store = Store(tmp_path)
    store.create("demo", [SOURCE], "fiction")
    terminal = [dict(_pending_item(i), key=f"closed-{i}", status="resolved") for i in range(105)]
    pending = _pending_item(999)
    result = store.publish("demo", 0, {}, "current", [pending] + terminal)
    assert result["decisions"] == [pending] + terminal[-20:]
    result = store.publish("demo", 1, {}, "next", result["decisions"] + [_pending_item(1000)])
    assert len(result["decisions"]) == 22
    assert [d["key"] for d in result["decisions"] if d["status"] == "pending"] == [
        "pending-999",
        "pending-1000",
    ]
