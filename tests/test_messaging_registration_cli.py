"""Explicit self-registration and title fallback never invent reachable peers."""

import json

import pytest

from ai_sessions import app
from ai_sessions.messaging import cli, hook, store
from ai_sessions.references import SlugStore
from ai_sessions.registry import REGISTRY


def test_cli_registers_only_the_invoking_session(monkeypatch, capsys):
    monkeypatch.setattr(
        hook, "register_current", lambda: dict(status="registered", tool="claude", session_id="own")
    )
    monkeypatch.setattr(cli, "deliver", lambda *a: pytest.fail("registration must not deliver"))
    assert cli.main(["messaging", "register", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == dict(
        status="registered", tool="claude", session_id="own"
    )


def test_cli_reports_unavailable_registration_without_success(monkeypatch, capsys):
    monkeypatch.setattr(
        hook,
        "register_current",
        lambda: dict(status="unavailable", reason="Native endpoint not ready."),
    )
    assert cli.main(["messaging", "register", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"


def test_cli_self_registration_rejects_foreign_target(monkeypatch, capsys):
    monkeypatch.setattr(
        hook, "register_current", lambda: pytest.fail("cannot register a selected peer")
    )
    assert cli.main(["messaging", "register", "someone-else", "--json"]) == 2
    assert "no target" in json.loads(capsys.readouterr().out)["reason"]


@pytest.fixture
def catalog_receiver(tmp_path, monkeypatch):
    home = str(REGISTRY.get("claude").home)
    receiver = dict(
        tool="claude",
        session_id="own",
        name="own",
        home=home,
        generation="generation",
        pid=42,
        started=1,
    )
    item = app.Session(
        "claude",
        "own",
        "top-lego-pwa-implementation-worker-2",
        str(tmp_path),
        2,
        1,
        "",
        True,
        "current.jsonl",
    )
    cached = [item]
    monkeypatch.setattr(store, "receivers", lambda: [receiver.copy()])
    monkeypatch.setattr(REGISTRY, "adapters", lambda: [])
    monkeypatch.setattr(cli, "_slug_store", lambda: SlugStore(tmp_path / "slugs.sqlite3"))
    monkeypatch.setattr(app, "load_session_catalog", lambda: cached)
    monkeypatch.setattr(
        app, "load_sessions", lambda *a, **kw: pytest.fail("no native discovery sweep")
    )
    return receiver, cached


def test_cached_native_name_resolves_only_an_already_reachable_receiver(catalog_receiver):
    receiver, cached = catalog_receiver
    other = app.Session("claude", "offline", "Offline worker", ".", 2, 1, "", True, "offline.jsonl")
    cached.append(other)
    rows = cli.reachable()
    assert len(rows) == 1
    assert rows[0]["name"] == "top-lego-pwa-implementation-worker-2"
    assert store.resolve("top-lego-pwa-implementation-worker-2", rows)["session_id"] == "own"
    assert cli._address(rows[0]) == cli._address(receiver)
    assert receiver["name"] == "own"  # Display enrichment does not rewrite registrations.


@pytest.mark.parametrize("mismatch", ["home", "duplicate", "native-id"])
def test_name_fallback_refuses_other_home_or_ambiguous_native_copy(
    catalog_receiver, tmp_path, mismatch
):
    receiver, cached = catalog_receiver
    if mismatch == "home":
        receiver["home"] = str(tmp_path / "foreign-home")
    elif mismatch == "native-id":
        cached[0].session_id = "different"
    else:
        cached.append(
            app.Session("claude", "own", "Another copy", ".", 2, 1, "", True, "copy.jsonl")
        )
    assert cli.reachable()[0]["name"] == "own"


def test_existing_native_receiver_name_takes_precedence_over_cached_title(catalog_receiver):
    receiver, _ = catalog_receiver
    receiver["name"] = "Live native name"
    assert cli.reachable()[0]["name"] == "Live native name"
