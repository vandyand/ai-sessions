from unittest.mock import patch

import pytest

from ai_sessions.handles import HandleStore
from ai_sessions.messaging import store as messaging
from ai_sessions.model import Session
from ai_sessions.references import NativeKey, SlugStore, resolve_session


@pytest.fixture
def registry(tmp_path):
    with (
        patch(
            "ai_sessions.handles.native_key",
            side_effect=lambda tool, ident: NativeKey(tool, ident, tmp_path),
        ),
        patch(
            "ai_sessions.references.native_key",
            side_effect=lambda tool, ident, home=None: NativeKey(tool, ident, home or tmp_path),
        ),
    ):
        handles = HandleStore(tmp_path / "registry.sqlite3")
        first = Session("engine", "first", "Worker", str(tmp_path / "elevate"), 1, 1, "", True, "")
        handles.ensure([first])
        yield handles, SlugStore(handles.path), first, tmp_path


def test_slugs_cannot_steal_handle_or_first_alias(registry):
    handles, slugs, first, root = registry
    foreign = NativeKey("engine", "second", root)
    for alias in (first.handle, first.handle.removesuffix("-1")):
        with pytest.raises(ValueError, match="handle"):
            slugs.set_slug(foreign, alias.upper())
        assert slugs.ensure([(foreign, alias)])[foreign].casefold() != alias.casefold()
    with pytest.raises(ValueError, match="handle"):
        slugs.set_slug(NativeKey("engine", "first", root), first.handle)


def test_retired_handles_remain_reserved_from_slugs(registry):
    handles, slugs, first, root = registry
    old = first.handle
    handles.set_session(first, "custom")
    with pytest.raises(ValueError, match="handle"):
        slugs.set_slug(NativeKey("engine", "second", root), old)


def test_central_resolver_and_messaging_do_not_fall_back_to_stale_copy(registry):
    handles, slugs, first, root = registry
    second = Session("engine", "second", "Worker", first.cwd, 1, 1, "", True, "")
    handles.link(first, second)
    with patch(
        "ai_sessions.references.native_key",
        side_effect=lambda tool, ident, home=None: NativeKey(tool, ident, home or root),
    ):
        assert (
            resolve_session(first.handle.removesuffix("-1"), [first, second], store=slugs) is second
        )
        rows = [dict(tool="engine", session_id="first", home=str(root))]
        with pytest.raises(ValueError, match="unavailable"):
            messaging.resolve(first.handle, rows, slugs=slugs)
        rows.append(dict(tool="engine", session_id="second", home=str(root)))
        assert messaging.resolve(first.handle, rows, slugs=slugs) is rows[1]


def test_native_address_survives_legacy_retired_handle_collision(registry):
    import sqlite3

    handles, slugs, first, root = registry
    with sqlite3.connect(handles.path) as connection:
        owner = connection.execute("SELECT logical_id FROM handle_members").fetchone()[0]
        connection.execute("INSERT INTO handle_claims VALUES (?, ?)", (first.session_id, owner))
    assert resolve_session(first.session_id, [first], store=slugs) is first
    rows = [dict(tool="engine", session_id=first.session_id, home=str(root))]
    assert messaging.resolve(first.session_id, rows, slugs=slugs) is rows[0]


def test_per_copy_slug_survives_legacy_overlap_after_bridge(registry):
    import sqlite3

    handles, slugs, first, root = registry
    slugs.set_slug(NativeKey(first.tool, first.session_id, root), "exact-copy")
    with sqlite3.connect(handles.path) as connection:
        owner = connection.execute("SELECT logical_id FROM handle_members").fetchone()[0]
        connection.execute("INSERT INTO handle_claims VALUES (?, ?)", ("exact-copy", owner))
        connection.execute("UPDATE handle_logicals SET handle='exact-copy' WHERE id=?", (owner,))
    second = Session("engine", "second", "Worker", first.cwd, 2, 1, "", True, "")
    handles.link(first, second)
    first.slug = "exact-copy"
    assert resolve_session("exact-copy", [first, second], store=slugs) is first
    rows = [
        dict(tool="engine", session_id=first.session_id, home=str(root), slug="exact-copy"),
        dict(tool="engine", session_id=second.session_id, home=str(root)),
    ]
    assert messaging.resolve("exact-copy", rows, slugs=slugs) is rows[0]
