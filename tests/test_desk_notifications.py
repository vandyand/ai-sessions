import concurrent.futures
import json

import pytest

from ai_sessions.desk import notifications, observer
from ai_sessions.desk.store import ConflictError, Store

SOURCE = {"tool": "codex", "session_id": "supervisor", "storage": "fiction.jsonl"}
HUMAN = {"tool": "codex", "session_id": "human", "home": "fiction-home"}


def decision(key="color", status="pending", **changes):
    return dict(
        key=key,
        question="Which color?",
        context="Palette context",
        recommendation="Blue",
        status=status,
        evidence=["fixture"],
        **changes,
    )


def publish(store, decisions):
    before = store.get("demo")
    return store.publish("demo", before["revision"], {"observed_at": 123}, "Review", decisions)


@pytest.fixture
def desk(tmp_path, monkeypatch):
    store = Store(tmp_path / "desk")
    store.create("demo", [SOURCE], "fiction")
    store.bind_agent("demo", "codex", "human", "fiction-home")
    publish(store, [])
    deliveries, receipts = [], []
    monkeypatch.setattr(notifications.messaging, "reachable", lambda: [HUMAN.copy()])
    monkeypatch.setattr(
        notifications.messaging,
        "deliver",
        lambda *args: deliveries.append(args) or {"status": "accepted"},
    )
    monkeypatch.setattr(
        notifications.receipts, "save_receipt", lambda row: receipts.append(row.copy())
    )
    return store, deliveries, receipts


def test_new_only_persistent_dedup_and_system_identity(desk, monkeypatch):
    store, deliveries, receipts = desk
    monkeypatch.setenv("CODEX_THREAD_ID", "supervisor")
    monkeypatch.setenv("AI_SESSIONS_MESSAGE_FROM", "codex:supervisor")
    publish(store, [decision()])
    assert len(notifications.pending(store, "demo")) == 1
    notifications.drain(Store(store.root), "demo")
    row, envelope, mid, sender = deliveries[0]
    assert row == HUMAN
    assert sender["tool"] == "system" and sender["session_id"] == "observer"
    assert "[ai-sessions peer message \u2014 not user consent]" in envelope
    meta = json.loads(envelope.splitlines()[1])
    assert meta["sender_handle"] == "briefing" and meta["sender"] == sender
    assert meta["recipient"] == HUMAN and meta["message_id"] == mid
    assert "Which color?" in envelope and "Palette context" in envelope
    assert "decisions.md" in envelope and "informational" in envelope.lower()
    assert receipts[-1]["status"] == "accepted"
    for status in ["pending", "resolved", "pending"]:
        item = decision(status=status)
        item["question"] = "Edited question"
        publish(store, [item])
        notifications.drain(store, "demo")
    assert len(deliveries) == 1
    publish(Store(store.root), [decision(), decision("new-after-reload")])
    notifications.drain(Store(store.root), "demo")
    assert len(deliveries) == 2


def test_new_pending_on_first_publication_notifies_bound_desk(desk):
    baseline, deliveries, _ = desk
    store = Store(baseline.root / "first-publication")
    store.create("demo", [SOURCE], "fiction")
    store.bind_agent("demo", "codex", "human", "fiction-home")
    assert store.get("demo")["observed_at"] is None
    publish(store, [decision()])
    assert len(notifications.pending(store, "demo")) == 1
    notifications.drain(store, "demo")
    notifications.drain(Store(store.root), "demo")
    assert len(deliveries) == 1


def test_existing_migrated_decisions_seed_silently_even_without_observed_at(tmp_path):
    store = Store(tmp_path)
    store.create("demo", [SOURCE], "fiction")
    store.bind_agent("demo", "codex", "human", "fiction-home")
    # Emulate a pre-feature DB with historical decisions but no key ledger.
    with store._connect() as db:
        record = store._get(db, "demo")
        record["decisions"] = [decision()]
        store._write(db, record)
    publish(store, [decision(), decision("fresh")])
    assert [e["key"] for e in notifications.pending(store, "demo")] == ["fresh"]


def test_offline_exact_binding_and_cancellation(desk, monkeypatch):
    store, deliveries, _ = desk
    publish(store, [decision()])
    for receivers in [[], [{**HUMAN, "home": "wrong-home"}], [SOURCE], [HUMAN, HUMAN]]:
        monkeypatch.setattr(notifications.messaging, "reachable", lambda: receivers)
        notifications.drain(store, "demo")
        assert len(notifications.pending(store, "demo")) == 1
    publish(store, [decision(status="resolved")])
    monkeypatch.setattr(notifications.messaging, "reachable", lambda: [HUMAN])
    notifications.drain(store, "demo")
    publish(store, [decision()])
    notifications.drain(store, "demo")
    assert not deliveries and notifications.pending(store, "demo") == []


@pytest.mark.parametrize("outcome", ["uncertain", "unavailable", "exception", "receipt-failure"])
def test_single_submission_including_uncertain_and_failure(desk, monkeypatch, outcome):
    store, deliveries, _ = desk
    publish(store, [decision()])

    def send(*args):
        deliveries.append(args)
        if outcome == "exception":
            raise RuntimeError("transport lost")
        return {"status": outcome}

    monkeypatch.setattr(notifications.messaging, "deliver", send)
    if outcome == "receipt-failure":
        monkeypatch.setattr(
            notifications.receipts, "save_receipt", lambda row: (_ for _ in ()).throw(OSError())
        )
    notifications.drain(store, "demo")
    notifications.drain(Store(store.root), "demo")
    assert len(deliveries) <= 1
    assert notifications.pending(store, "demo") == []


def test_atomic_conflict_and_concurrent_drains(desk):
    store, deliveries, _ = desk
    before = store.get("demo")
    publish(store, [decision()])
    with pytest.raises(ConflictError):
        store.publish("demo", before["revision"], {}, "stale", [decision("stale")])
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: notifications.drain(Store(store.root), "demo"), range(4)))
    assert len(deliveries) == 1


def test_second_bound_human_and_never_source_identity(desk, monkeypatch):
    store, deliveries, _ = desk
    store.bind_agent("demo", "claude", "human-claude", "other-home")
    human2 = {"tool": "claude", "session_id": "human-claude", "home": "other-home"}
    monkeypatch.setattr(notifications.messaging, "reachable", lambda: [SOURCE, HUMAN, human2])
    publish(store, [decision()])
    notifications.drain(store, "demo")
    assert {item[0]["session_id"] for item in deliveries} == {"human", "human-claude"}


def test_pruning_does_not_forget_seen_keys(desk, monkeypatch):
    store, deliveries, _ = desk
    monkeypatch.setattr(notifications, "MAX_HISTORY", 2)
    for i in range(5):
        publish(store, [decision(str(i))])
        notifications.drain(store, "demo")
        publish(store, [decision(str(i), "resolved")])
    publish(Store(store.root), [decision("0")])
    notifications.drain(store, "demo")
    assert len(deliveries) == 5
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM desk_notification_events").fetchone()[0] <= 2
        assert db.execute("SELECT count(*) FROM desk_notification_keys").fetchone()[0] == 5


def test_notification_failure_does_not_fail_summary(desk, monkeypatch):
    store, _, _ = desk
    good = store.get("demo")
    monkeypatch.setattr(observer, "_refresh", lambda *args, **kw: good)
    monkeypatch.setattr(
        notifications, "drain", lambda *args, **kw: (_ for _ in ()).throw(RuntimeError())
    )
    assert observer.refresh(store, "demo") == good
    assert store.get("demo")["error"] is None


def test_offline_queue_delivers_once_when_exact_recipient_returns(desk, monkeypatch):
    store, deliveries, _ = desk
    publish(store, [decision()])
    monkeypatch.setattr(notifications.messaging, "reachable", lambda: [])
    notifications.drain(store, "demo")
    monkeypatch.setattr(notifications.messaging, "reachable", lambda: [HUMAN])
    notifications.drain(Store(store.root), "demo")
    notifications.drain(Store(store.root), "demo")
    assert len(deliveries) == 1


def test_publish_rolls_back_outbox_and_key_ledger_with_snapshot(desk):
    store, _, _ = desk
    before = store.get("demo")
    with store._connect() as db:
        db.execute(
            "CREATE TRIGGER fixture_abort BEFORE UPDATE ON desks "
            "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"
        )
    with pytest.raises(Exception, match="fixture failure"):
        publish(store, [decision()])
    assert store.get("demo") == before
    assert notifications.pending(store, "demo") == []
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM desk_notification_keys").fetchone()[0] == 0
        db.execute("DROP TRIGGER fixture_abort")
    publish(store, [decision()])
    assert len(notifications.pending(store, "demo")) == 1


def test_crash_after_claim_is_not_retried(desk):
    store, deliveries, _ = desk
    publish(store, [decision()])
    event = notifications.pending(store, "demo")[0]
    assert notifications._claim(store, "demo", event)
    notifications.drain(Store(store.root), "demo")
    assert not deliveries


def test_resolution_during_recipient_discovery_cancels_before_submission(desk, monkeypatch):
    store, deliveries, _ = desk
    publish(store, [decision()])

    def reachable():
        publish(store, [decision(status="resolved")])
        return [HUMAN]

    monkeypatch.setattr(notifications.messaging, "reachable", reachable)
    notifications.drain(store, "demo")
    assert not deliveries


def test_bound_operational_source_and_unbound_questions_never_notify(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.create("demo", [SOURCE], "fiction")
    publish(store, [])
    publish(store, [decision("unbound")])
    store.bind_agent("demo", "codex", "supervisor", "fiction-home")
    publish(store, [decision("unbound"), decision("source-bound")])
    assert notifications.pending(store, "demo") == []


def test_refresh_drains_even_unchanged_successful_observation(desk, monkeypatch):
    store, deliveries, _ = desk
    publish(store, [decision()])
    good = store.get("demo")
    monkeypatch.setattr(observer, "_refresh", lambda *a, **kw: good)
    assert observer.refresh(Store(store.root), "demo") == good
    assert len(deliveries) == 1


def test_resolution_during_receipt_write_prevents_delivery(desk, monkeypatch):
    store, deliveries, _ = desk
    publish(store, [decision()])

    def receipt(row):
        if row["status"] == "submitting":
            publish(store, [decision(status="resolved")])

    monkeypatch.setattr(notifications.receipts, "save_receipt", receipt)
    notifications.drain(store, "demo")
    assert not deliveries
    assert notifications.pending(store, "demo") == []


def test_post_submission_receipt_failure_never_retries(desk, monkeypatch):
    store, deliveries, _ = desk
    publish(store, [decision()])

    def receipt(row):
        if row["status"] == "accepted":
            raise OSError("fixture final receipt failed")

    monkeypatch.setattr(notifications.receipts, "save_receipt", receipt)
    notifications.drain(store, "demo")
    notifications.drain(Store(store.root), "demo")
    assert len(deliveries) == 1


def test_native_codex_wrapper_keeps_full_system_metadata(desk):
    from ai_sessions.harnesses.messaging_codex import _envelope

    store, deliveries, _ = desk
    publish(store, [decision()])
    notifications.drain(store, "demo")
    _, text, mid, sender = deliveries[0]
    native = _envelope(text, mid, sender)
    outer = json.loads(native.splitlines()[2])
    inner = json.loads(native.splitlines()[4])
    assert outer["message_id"] == inner["message_id"] == mid
    assert outer["sender"]["tool"] == inner["sender"]["tool"] == "system"
    assert outer["sender"]["session_id"] == inner["sender"]["session_id"] == "observer"
    assert inner["informational"] and inner["sender_handle"] == "briefing"
