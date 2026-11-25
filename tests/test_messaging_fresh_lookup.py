"""Fresh name lookup must preserve durable claims and exact receiver ownership."""

from dataclasses import replace

import pytest

from ai_sessions import app
from ai_sessions.handles import HandleStore
from ai_sessions.messaging import cli, store
from ai_sessions.references import SlugStore, native_key
from ai_sessions.registry import REGISTRY


@pytest.fixture
def fresh_lookup(tmp_path, monkeypatch):
    home = str(REGISTRY.get("claude").home)
    item = app.Session(
        "claude", "new-session", "New worker title", str(tmp_path), 2, 1, "", True, "current.jsonl"
    )
    receiver = dict(
        tool="claude", session_id="new-session", home=home, generation="current", name="new-session"
    )
    receivers = [receiver]
    discovered = [item]
    calls = []
    registry = SlugStore(tmp_path / "slugs.sqlite3")
    monkeypatch.setattr(cli, "_slug_store", lambda: registry)

    def reachable(*, enrich=True):
        calls.append("receivers")
        return list(receivers)

    def discover(*, use_cache):
        assert use_cache is False
        calls.append("discovery")
        return list(discovered)

    monkeypatch.setattr(cli, "reachable", reachable)
    monkeypatch.setattr(app, "load_sessions", discover)
    monkeypatch.setattr(store, "ROOT", tmp_path / "messages")
    monkeypatch.setattr(
        store, "save_receiver", lambda *a: pytest.fail("no registration side effect")
    )
    deliveries = []
    monkeypatch.setattr(cli, "deliver", lambda *a: deliveries.append(a) or {"status": "submitted"})
    monkeypatch.delenv("AI_SESSIONS_MESSAGE_FROM", raising=False)
    for adapter in REGISTRY.adapters():
        if adapter.messaging and adapter.messaging.sender_environment:
            monkeypatch.delenv(adapter.messaging.sender_environment, raising=False)
    return item, receiver, receivers, discovered, registry, calls, deliveries


def test_send_refreshes_once_for_uncached_title_and_rechecks_verified_receivers(fresh_lookup):
    item, receiver, _, _, _, calls, deliveries = fresh_lookup
    code, receipt = cli.send_message(item.title, "offline test fixture", None)
    assert code == 0
    assert receipt["recipient"] == cli._address(receiver)
    assert deliveries[0][0] is receiver
    assert calls == ["receivers", "discovery", "receivers"]


@pytest.mark.parametrize("damage", ["offline", "foreign-home", "missing-home", "duplicate"])
def test_known_session_without_unique_exact_home_receiver_is_not_sent(
    fresh_lookup, tmp_path, damage
):
    item, receiver, receivers, _, _, calls, deliveries = fresh_lookup
    if damage == "offline":
        receivers.clear()
    elif damage == "foreign-home":
        receiver["home"] = str(tmp_path / "other-home")
    elif damage == "missing-home":
        receiver.pop("home")
    else:
        receivers.append(receiver.copy())
    with pytest.raises(ValueError, match="registered|ambiguous") as error:
        cli.send_message(item.title, "offline test fixture", None)
    if damage != "duplicate":
        assert "ses messaging register" in str(error.value)
    assert calls.count("discovery") == 1
    assert not deliveries
    assert not (store.ROOT / "receipts").exists()


def test_fresh_title_ambiguity_never_selects_first_or_newest_session(fresh_lookup):
    item, _, _, discovered, _, calls, deliveries = fresh_lookup
    discovered.append(replace(item, session_id="another", storage="another.jsonl", updated=99))
    with pytest.raises(ValueError, match="ambiguous"):
        cli.send_message(item.title, "offline test fixture", None)
    assert calls.count("discovery") == 1 and not deliveries


@pytest.mark.parametrize("kind", ["slug", "retired-slug", "handle", "retired-handle"])
def test_offline_durable_claim_never_falls_through_to_live_same_title(fresh_lookup, kind):
    item, _, _, discovered, registry, calls, deliveries = fresh_lookup
    old = replace(item, session_id="offline-owner", title="Old owner", storage="old.jsonl")
    target = "claimed-worker"
    if "slug" in kind:
        owner = native_key(old.tool, old.session_id)
        registry.ensure([(owner, target)])
        if kind == "retired-slug":
            registry.set_slug(owner, "renamed-owner")
    else:
        handles = HandleStore(registry.path)
        handles.ensure([old])
        handles.set_session(old, target)
        if kind == "retired-handle":
            handles.set_session(old, "renamed-owner")
    discovered[:] = [replace(item, title=target)]
    with pytest.raises(ValueError):
        cli.send_message(target, "offline test fixture", None)
    assert calls.count("discovery") == 0 and not deliveries


def test_existing_ambiguous_receiver_name_does_not_trigger_discovery(fresh_lookup):
    item, receiver, receivers, _, _, calls, deliveries = fresh_lookup
    receiver["name"] = item.title
    receivers.append({**receiver, "session_id": "other"})
    with pytest.raises(ValueError, match="ambiguous"):
        cli.send_message(item.title, "offline test fixture", None)
    assert calls == ["receivers"] and not deliveries


def test_receiver_disappearing_during_discovery_is_not_used(fresh_lookup, monkeypatch):
    item, _, receivers, _, _, _, deliveries = fresh_lookup

    def discover(*, use_cache):
        receivers.clear()
        return [item]

    monkeypatch.setattr(app, "load_sessions", discover)
    with pytest.raises(ValueError, match="registered"):
        cli.send_message(item.title, "offline test fixture", None)
    assert not deliveries


def test_unknown_reference_only_gets_one_discovery_attempt(fresh_lookup):
    _, _, _, _, _, calls, deliveries = fresh_lookup
    with pytest.raises(ValueError, match="no session"):
        cli.send_message("nonexistent title", "offline test fixture", None)
    assert calls.count("discovery") == 1 and not deliveries


def test_known_offline_slug_error_explains_own_session_registration(fresh_lookup):
    item, _, receivers, _, registry, calls, deliveries = fresh_lookup
    receivers.clear()
    registry.ensure([(native_key(item.tool, item.session_id), "offline-worker")])
    with pytest.raises(ValueError, match="ses messaging register"):
        cli.send_message("offline-worker", "offline test fixture", None)
    assert "discovery" not in calls and not deliveries
