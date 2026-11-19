from concurrent.futures import ThreadPoolExecutor

import pytest

from ai_sessions.handles import HandleStore
from ai_sessions.model import Session
from ai_sessions.references import NativeKey, SlugStore


def item(ident, title="Worker", **kwargs):
    return Session("test", ident, title, "/projects/elevator", 1, 1, "", True, "", **kwargs)


@pytest.fixture
def store(tmp_path, monkeypatch):
    import ai_sessions.handles as handles

    monkeypatch.setattr(handles, "native_key", lambda tool, ident: NativeKey(tool, ident, tmp_path))
    return HandleStore(tmp_path / "registry.sqlite3")


def test_defaults_stable_and_aliases(store):
    a, b = item("a", "Worker 11"), item("b", "Worker 12")
    store.ensure([a, b])
    assert (a.handle, b.handle) == ("elev-wrk-11", "elev-wrk-12")
    a.title = "Manager"
    a.conversation_id = "new-conversation"
    store.ensure([a])
    assert a.handle == "elev-wrk-11"
    c = item("c", "ordinary title")
    store.ensure([c])
    assert c.handle == "elev-ses-1"
    assert store.resolve("ELEV-ses", [c]) is c
    assert store.lookup_members(c.handle) == store.lookup_members("elev-ses")


def test_rename_edits_and_reservations(store):
    a = item("a")
    store.ensure([a])
    store.set_project("elev", "EL")
    store.ensure([a])
    assert a.handle == "el-wrk-1"
    with pytest.raises(ValueError, match="retired"):
        store.resolve("elev-wrk", [a])
    store.set_session(a, "chosen")
    assert store.claimed("CHOSEN-1")
    assert store.reserved_names()["chosen"] == set(store.lookup_members("chosen"))
    assert store.project_for(a)["handle"] == "el"
    with pytest.raises(ValueError):
        store.add_project("EL", "/other")


def test_shared_slug_and_native_namespace(store):
    a, b = item("a"), item("elev-wrk-2")
    key = NativeKey("test", "foreign", store.path.parent)
    SlugStore(store.path).set_slug(key, "elev-wrk")
    store.ensure([a, b])
    assert a.handle == "elev-wrk-3"
    with pytest.raises(ValueError):
        store.set_session(a, "elev-wrk-2")


def test_bridge_migration_fork_and_tie(store):
    a, b = item("a"), item("b")
    store.ensure([a, b])
    previous = b.handle
    a.conversation_id = b.conversation_id = "linked"
    store.link(a, b)
    store.ensure([a, b])
    assert a.handle == b.handle
    assert store.resolve(a.handle, [a, b]) is b
    with pytest.raises(ValueError, match="retired"):
        store.resolve(previous, [a, b])
    a.updated = 2
    store.ensure([a, b])
    assert store.resolve(a.handle, [a, b]) is a
    c = item("c", parent_id="a")
    store.ensure([c])
    assert c.handle != a.handle
    d = item("d", conversation_id="linked")
    store.ensure([d])
    assert d.handle == a.handle


def test_concurrent_allocation(store):
    rows = [item(str(n)) for n in range(12)]
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(lambda row: store.ensure([row]), rows))
    assert len({row.handle for row in rows}) == len(rows)
    assert {int(row.handle.rsplit("-", 1)[1]) for row in rows} == set(range(1, 13))


def test_unavailable_and_roundtrip(store):
    a = item("a")
    store.ensure([a])
    reopened = HandleStore(store.path)
    assert reopened.lookup_members(a.handle) == store.lookup_members(a.handle)
    with pytest.raises(ValueError, match="unavailable"):
        reopened.resolve(a.handle, [])
    assert reopened.resolve("unknown", []) is None


def test_preferred_unavailable_never_falls_back(store):
    a, b = item("a"), item("b")
    store.link(a, b)
    assert store.current_key(a.handle).session_id == "b"
    with pytest.raises(ValueError, match="unavailable"):
        store.resolve(a.handle, [a])
    a.updated = 10
    store.ensure([a])
    assert store.current_key(a.handle).session_id == "a"


def test_git_worktrees_share_project(store, tmp_path):
    root = tmp_path / "elevate-futures"
    (root / ".git" / "worktrees" / "one").mkdir(parents=True)
    first = tmp_path / "wt-one"
    first.mkdir()
    gitdir = root / ".git" / "worktrees" / "one"
    (first / ".git").write_text(f"gitdir: {gitdir}")
    (gitdir / "commondir").write_text("../..")
    second = tmp_path / "wt-two"
    second.mkdir()
    (second / ".git").write_text(f"gitdir: {gitdir}")
    a, b, c = item("a"), item("b"), item("c")
    a.cwd, b.cwd, c.cwd = str(root), str(first), str(second)
    store.ensure([a, b, c])
    assert {x.project_handle for x in (a, b, c)} == {"elev"}
    assert len(store.projects()) == 1


def test_non_git_directories_distinct_and_override(store, tmp_path):
    a, b = item("a"), item("b")
    a.cwd, b.cwd = str(tmp_path / "alpha"), str(tmp_path / "alpine")
    store.ensure([a, b])
    assert a.project_handle != b.project_handle
    store.add_project("Q", tmp_path / "explicit")
    store.assign(a, "q", role="mgr", number=12)
    a.cwd = b.cwd
    a.title = "Worker 1"
    store.ensure([a])
    assert a.handle == "q-mgr-12"
    with pytest.raises(ValueError):
        store.assign(b, "q", role="mgr", number=12)


def test_same_owner_slug_is_reserved_and_alias_collision_refused(store):
    a, b = item("a"), item("b")
    key = NativeKey(a.tool, a.session_id, store.path.parent)
    SlugStore(store.path).set_slug(key, "elev-wrk")
    store.ensure([a, b])
    assert a.handle == "elev-wrk-2"
    with pytest.raises(ValueError):
        store.set_session(a, "ELEV-WRK")
    with pytest.raises(ValueError):
        store.set_session(b, "ELEV-WRK")
    assert not store.claimed("elev-wrk-2-1")


def test_foreign_home_cannot_resolve(store, monkeypatch, tmp_path):
    a = item("a")
    store.ensure([a])
    import ai_sessions.handles as handles

    monkeypatch.setattr(
        handles, "native_key", lambda tool, ident: NativeKey(tool, ident, tmp_path / "different")
    )
    with pytest.raises(ValueError, match="home"):
        store.resolve(a.handle, [a])
    store.ensure([a])
    assert a.handle == "elev-wrk-2"


def test_reservations_existing_connection(store):
    import sqlite3

    a = item("a")
    store.ensure([a])
    expected = set(store.lookup_members(a.handle))
    with sqlite3.connect(store.path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert store.reserved_names(connection)["elev-wrk"] == expected


def test_qualified_handle_selects_explicit_copy(store):
    a, b = item("a"), item("b")
    b.tool = "other"
    store.link(a, b)
    assert store.current_key(a.handle).tool == "other"
    assert store.resolve("test:" + a.handle, [a]) is a
    assert store.claimed("test:" + a.handle)
    assert {key.tool for key in store.lookup_members("test:" + a.handle)} == {"test"}
    with pytest.raises(ValueError):
        store.current_key("missing:" + a.handle)


def test_project_rename_keeps_old_name_reserved(store):
    store.add_project("OLD", "/first")
    store.set_project("old", "NEW")
    with pytest.raises(ValueError, match="reserved|assigned"):
        store.add_project("old", "/second")
    store.add_project("other", "/third")
    with pytest.raises(ValueError, match="reserved|assigned"):
        store.set_project("other", "OLD")


@pytest.mark.parametrize(
    "operation",
    [
        lambda s: s.resolve("unknown", []),
        lambda s: s.lookup_members("unknown"),
        lambda s: s.claimed("unknown"),
        lambda s: s.reserved_names(),
        lambda s: s.projects(),
    ],
)
def test_read_only_missing_registry_does_not_create(store, operation):
    operation(store)
    assert not store.path.exists()


def test_reads_do_not_initialize_slug_only_database(store):
    import sqlite3

    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    assert store.resolve("unknown", []) is None
    assert store.reserved_names() == {}
    assert store.projects() == []
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("unrelated",)]


def test_read_lookup_does_not_take_write_lock(store):
    import sqlite3

    a = item("a")
    store.ensure([a])
    with sqlite3.connect(store.path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        assert store.current_key(a.handle).session_id == "a"
        assert store.lookup_members(a.handle)
        assert store.projects()[0]["handle"] == "elev"


@pytest.mark.parametrize(
    "operation",
    [
        lambda s: s.ensure([item("a")]),
        lambda s: s.resolve("unknown", []),
        lambda s: s.reserved_names(),
    ],
)
def test_corrupt_registry_errors_are_value_errors(store, operation):
    store.path.write_bytes(b"not a sqlite database")
    with pytest.raises(ValueError, match="registry"):
        operation(store)


def test_unchanged_ensure_does_not_write_activity(store, monkeypatch):
    import sqlite3

    import ai_sessions.handles as handles

    a = item("a", conversation_id="conversation")
    store.ensure([a])
    changes = []
    original = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        # Observe durable row changes without relying on filesystem timestamps.
        connection.create_function("record_change", 0, lambda: changes.append(True) or 0)
        return connection

    monkeypatch.setattr(handles.sqlite3, "connect", connect)
    with original(store.path) as connection:
        connection.execute(
            "CREATE TRIGGER activity_changed AFTER UPDATE ON handle_activity BEGIN SELECT record_change(); END"
        )
    store.ensure([a])
    assert changes == []
    a.updated = 5
    store.ensure([a])
    assert changes == [True]


def test_default_allocation_has_linear_candidate_checks(store, monkeypatch):
    calls = 0
    original = store._available

    def available(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_available", available)
    rows = [item(str(n), "ordinary") for n in range(6000)]
    store.ensure(rows)
    assert len({row.handle for row in rows}) == 6000
    assert calls <= 18000


def test_casefold_native_id_collision(store):
    a, b = item("a"), item("\u212a")
    store.ensure([a, b])
    with pytest.raises(ValueError):
        store.set_session(a, "k")


def test_bulk_canonical_matches_single_selection(store):
    a, b, c = item("a"), item("b"), item("c", "Manager")
    b.tool = "other"
    store.ensure([a, b, c])
    retired = b.handle
    store.link(a, b)
    assert store.current_keys() == {
        a.handle: store.current_key(a.handle),
        c.handle: store.current_key(c.handle),
    }
    assert store.current_keys([a.handle.upper(), "unknown"]) == {
        a.handle: store.current_key(a.handle)
    }
    assert store.current_keys(["test:" + a.handle]) == {
        a.handle: store.current_key("test:" + a.handle)
    }
    b.superseded = True
    b.updated = 100
    store.ensure([b])
    assert store.current_keys()[a.handle].session_id == "a"
    b.superseded = False
    store.ensure([b])
    assert store.current_keys()[a.handle].session_id == "b"
    with pytest.raises(ValueError, match="retired"):
        store.current_keys([retired])


def test_bulk_canonical_never_uses_foreign_home_fallback(store, monkeypatch, tmp_path):
    import ai_sessions.handles as handles

    a, b = item("a"), item("b")
    store.link(a, b)
    monkeypatch.setattr(
        handles,
        "native_key",
        lambda tool, ident: NativeKey(
            tool, ident, tmp_path if ident == "a" else tmp_path / "foreign"
        ),
    )
    with pytest.raises(ValueError, match="home"):
        store.current_keys()


def test_metadata_batch_only_reads_known_exact_members(store):
    a, b = item("a"), item("b")
    store.link(a, b)
    store.set_session(a, "edited")
    store.set_project("elev", "el")
    keys = [NativeKey(x.tool, x.session_id, store.path.parent) for x in (a, b)]
    unknown = NativeKey("test", "a", store.path.parent / "foreign")
    expected = dict(handle="edited", project_handle="el", session_role="wrk")
    assert store.metadata_for([*keys, unknown]) == {key: expected for key in keys}
    assert store.metadata_for([]) == {}


def test_bulk_reads_missing_registry_do_not_create(store):
    assert store.current_keys() == {}
    assert store.metadata_for([NativeKey("test", "unknown", store.path.parent)]) == {}
    assert not store.path.exists()


def test_bulk_reads_use_one_connection_and_constant_queries(store, monkeypatch):
    import sqlite3

    import ai_sessions.handles as handles

    rows = [item(str(n), "ordinary") for n in range(100)]
    store.ensure(rows)
    original = sqlite3.connect
    connections, queries = [], []

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connections.append(connection)
        connection.set_trace_callback(queries.append)
        return connection

    monkeypatch.setattr(handles.sqlite3, "connect", connect)
    assert len(store.current_keys()) == 100
    assert len(connections) == 1
    assert len(queries) <= 5
    connections.clear()
    queries.clear()
    assert (
        len(store.metadata_for([NativeKey(x.tool, x.session_id, store.path.parent) for x in rows]))
        == 100
    )
    assert len(connections) == 1
    assert len(queries) <= 5


def test_fork_with_shared_conversation_keeps_separate_handle(store):
    a = item("a", conversation_id="shared")
    store.ensure([a])
    fork = item("fork", parent_id="a", conversation_id="shared")
    store.ensure([fork])
    assert fork.handle != a.handle


def test_unchanged_ensure_is_readonly(store, monkeypatch):
    import sqlite3

    import ai_sessions.handles as handles

    a = item("a", conversation_id="conversation")
    store.ensure([a])
    queries = []
    original = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_trace_callback(queries.append)
        return connection

    monkeypatch.setattr(handles.sqlite3, "connect", connect)
    # Read-only refresh must also restore a caller's stale annotations.
    a.handle = ""
    store.ensure([a])
    assert a.handle == "elev-wrk-1"
    assert not any(
        query.startswith(("INSERT", "UPDATE", "CREATE", "BEGIN IMMEDIATE")) for query in queries
    )
    a.conversation_id = "new-conversation"
    store.ensure([a])
    assert any(query.startswith("INSERT") for query in queries)
    assert store.current_key(a.handle).session_id == "a"


def test_reserved_names_external_connection_wraps_sqlite_errors(store):
    import sqlite3

    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TABLE handle_claims (wrong TEXT)")
        with pytest.raises(ValueError, match="registry"):
            store.reserved_names(connection)


def test_plain_and_extended_windows_directories_share_project(store, tmp_path):
    import os

    if os.name != "nt":
        pytest.skip("Windows path normalization")
    root = tmp_path / "plain-project"
    root.mkdir()
    a, b = item("a"), item("b")
    a.cwd = str(root)
    b.cwd = "\\\\?\\" + str(root)
    store.ensure([a, b])
    assert a.project_handle == b.project_handle
    assert len(store.projects()) == 1


def test_known_conversation_uses_latest_named_role_on_first_allocation(store, tmp_path):
    a = item("a", "autogenerated", conversation_id="known")
    a.named = False
    b = item("b", "Manager 1", conversation_id="known")
    b.updated = 10
    b.cwd = str(tmp_path / "elevator")
    a.cwd = str(tmp_path / "other")
    store.ensure([a, b])
    assert a.handle == b.handle == "elev-mgr-1"
    assert a.session_role == "mgr"
    # Persisted membership always takes precedence over later title changes.
    b.title = "Worker 12"
    b.updated = 20
    store.ensure([a, b])
    assert b.handle == "elev-mgr-1"


@pytest.mark.parametrize("reference", ["OWN-NATIVE", "own-native-1"])
def test_handle_cannot_claim_own_native_id(store, reference):
    a = item("own-native")
    store.ensure([a])
    original = a.handle
    with pytest.raises(ValueError, match="reserved"):
        store.set_session(a, reference)
    assert store.current_key(original).session_id == "own-native"
    assert not store.claimed("own-native")


def test_default_allocation_skips_own_native_id(store):
    a = item("elev-wrk-1")
    store.ensure([a])
    assert a.handle == "elev-wrk-2"
    assert not store.claimed("elev-wrk")


def test_retired_own_slug_cannot_become_handle(store):
    a = item("a")
    key = NativeKey(a.tool, a.session_id, store.path.parent)
    slugs = SlugStore(store.path)
    slugs.set_slug(key, "retired-copy")
    slugs.set_slug(key, "current-copy")
    store.ensure([a])
    with pytest.raises(ValueError, match="reserved"):
        store.set_session(a, "RETIRED-COPY-1")
    assert slugs.claim_owner("retired-copy") == key
    assert slugs.lookup("current-copy") == key


def test_bridge_members_cannot_capture_per_copy_slug_names(store):
    a, b = item("a"), item("b")
    slugs = SlugStore(store.path)
    source = NativeKey(a.tool, a.session_id, store.path.parent)
    target = NativeKey(b.tool, b.session_id, store.path.parent)
    slugs.set_slug(source, "source-copy")
    slugs.set_slug(target, "target-copy")
    store.link(a, b)
    for name in ("SOURCE-COPY", "target-copy-1"):
        with pytest.raises(ValueError, match="reserved"):
            store.set_session(a, name)
    assert slugs.lookup("source-copy") == source
    assert slugs.lookup("target-copy") == target
    assert store.current_key(a.handle) == target


def test_inherit_target_project_all_copies_and_retire_default(store, tmp_path):
    target = item("target")
    sup = item("supervisor", supervises="target", conversation_id="supervision")
    copy = item("copy", supervises="target", conversation_id="supervision")
    unrelated = item("unrelated", "Supervisor of target")
    sup.cwd = copy.cwd = unrelated.cwd = str(tmp_path / "a123-uuid")
    store.ensure([target, sup, copy, unrelated])
    old, untouched = sup.handle, unrelated.handle
    store.inherit_projects([(sup, target), (copy, target)])
    assert sup.handle == copy.handle == "elev-sup-1"
    assert sup.project_handle == "elev"
    assert unrelated.handle == untouched
    with pytest.raises(ValueError, match="retired"):
        store.resolve(old, [sup, copy])
    assert store.lookup_members("elev-sup") == store.lookup_members(old)


def test_inherit_actual_manually_assigned_target_project(store, tmp_path):
    target, sup = item("target"), item("supervisor", supervises="target")
    sup.cwd = str(tmp_path / "uuid")
    store.ensure([target, sup])
    store.add_project("pick", tmp_path / "chosen")
    store.assign(target, "pick")
    store.inherit_projects([(sup, target)])
    assert sup.handle == "pick-sup-1"


def test_inherit_directory_fallback_uses_git_root_and_unique_alias(store, tmp_path):
    root = tmp_path / "target"
    (root / ".git").mkdir(parents=True)
    sup, occupied = item("sup", supervises="missing"), item("occupied", "Supervisor")
    occupied.cwd = str(root)
    sup.cwd = str(tmp_path / "uuid")
    store.ensure([sup, occupied])
    store.inherit_projects([(sup, str(root / "subdirectory"))])
    assert sup.handle == "targ-sup-2"
    assert sup.project_handle == occupied.project_handle


@pytest.mark.parametrize("manual", ["custom", "assign", "same_handle"])
def test_inherit_preserves_durable_manual_choices(store, tmp_path, manual):
    target, sup = item("target"), item("sup", supervises="target")
    sup.cwd = str(tmp_path / "uuid")
    store.ensure([target, sup])
    if manual == "assign":
        store.assign(sup, sup.project_handle, role="sup", number=1)
    else:
        store.set_session(sup, "custom" if manual == "custom" else sup.handle)
    previous = sup.handle, sup.project_handle
    reopened = HandleStore(store.path)
    reopened.inherit_projects([(sup, target)])
    assert (sup.handle, sup.project_handle) == previous


def test_inherit_legacy_defaults_and_custom_handles(store, tmp_path):
    import sqlite3

    target = item("target")
    sup, custom = item("sup", supervises="target"), item("custom", supervises="target")
    sup.cwd = custom.cwd = str(tmp_path / "uuid")
    store.ensure([target, sup, custom])
    store.set_session(custom, "chosen")
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER IF EXISTS handle_project_assignment_explicit")
        connection.execute("ALTER TABLE handle_logicals DROP COLUMN project_explicit")
    reopened = HandleStore(store.path)
    reopened.inherit_projects([(sup, target), (custom, target)])
    assert sup.handle == "elev-sup-2"
    assert custom.handle == "chosen"
    assert custom.project_handle == "uuid"
    # Migration must persist a safe classification for future calls.
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            'SELECT project_explicit FROM handle_logicals WHERE handle="chosen"'
        ).fetchone() == (1,)


def test_inherit_unchanged_is_readonly_and_restores_annotations(store, tmp_path, monkeypatch):
    import sqlite3

    import ai_sessions.handles as handles

    target, sup = item("target"), item("sup", supervises="target")
    sup.cwd = str(tmp_path / "uuid")
    store.ensure([target, sup])
    store.inherit_projects([(sup, target)])
    queries = []
    original = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_trace_callback(queries.append)
        return connection

    monkeypatch.setattr(handles.sqlite3, "connect", connect)
    sup.handle = ""
    with original(store.path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        store.inherit_projects([(sup, target)])
    assert sup.handle == "elev-sup-1"
    assert not any(
        q.startswith(("INSERT", "UPDATE", "CREATE", "ALTER", "BEGIN IMMEDIATE")) for q in queries
    )


def test_inherit_new_fallback_project_and_follow_target_until_manual_override(store, tmp_path):
    sup = item("sup", supervises="missing")
    store.ensure([sup])
    store.inherit_projects([(sup, str(tmp_path / "fallback"))])
    assert sup.handle == "fall-sup-1"
    target = item("target")
    store.ensure([target])
    store.inherit_projects([(sup, target)])
    assert sup.handle == "elev-sup-1"
    store.assign(sup, "fall", role="mgr", number=3)
    store.inherit_projects([(sup, target)])
    assert sup.handle == "fall-mgr-3"


def test_inherit_preserves_native_and_slug_namespace(store, tmp_path):
    target, sup = item("target"), item("sup", supervises="target")
    native = item("elev-sup-1")
    sup.cwd = str(tmp_path / "uuid")
    slug_key = NativeKey("test", "foreign", store.path.parent)
    SlugStore(store.path).set_slug(slug_key, "elev-sup-2")
    store.ensure([target, sup, native])
    store.inherit_projects([(sup, target)])
    assert sup.handle == "elev-sup-3"
    assert SlugStore(store.path).lookup("elev-sup-2") == slug_key


def test_inherit_empty_batch_does_not_create_registry(store):
    assert store.inherit_projects([]) == {}
    assert not store.path.exists()


def test_legacy_writer_project_assignment_survives_inheritance(store, tmp_path):
    import sqlite3

    target, sup = item("target"), item("sup", supervises="target")
    sup.cwd = str(tmp_path / "uuid")
    store.ensure([target, sup])
    project = store.add_project("pick", tmp_path / "chosen")
    # 3.19 assign updates these columns without knowing project_explicit exists.
    with sqlite3.connect(store.path) as connection:
        owner = connection.execute(
            "SELECT logical_id FROM handle_members WHERE session_id='sup'"
        ).fetchone()[0]
        connection.execute("INSERT INTO handle_claims VALUES (?,?)", ("pick-sup-7", owner))
        connection.execute(
            "UPDATE handle_logicals SET project_id=?,role=?,number=?,handle=? WHERE id=?",
            (project["id"], "sup", 7, "pick-sup-7", owner),
        )
    store.inherit_projects([(sup, target)])
    assert sup.handle == "pick-sup-7"
    assert sup.project_handle == "pick"


@pytest.mark.parametrize("first_write", ["ensure", "add_project", "set_project"])
def test_legacy_schema_preserves_assignment_away_from_native_project(store, tmp_path, first_write):
    import sqlite3

    target, sup = item("target"), item("sup", supervises="target")
    sup.cwd = str(tmp_path / "uuid")
    store.ensure([target, sup])
    store.add_project("pick", tmp_path / "chosen")
    store.assign(sup, "pick", role="sup", number=7)
    with sqlite3.connect(store.path) as connection:
        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'"):
            connection.execute('DROP TRIGGER "' + name + '"')
        connection.execute("ALTER TABLE handle_logicals DROP COLUMN project_explicit")
    reopened = HandleStore(store.path)
    if first_write == "add_project":
        reopened.add_project("extra", tmp_path / "extra")
    elif first_write == "set_project":
        reopened.set_project("pick", "kept")
    reopened.ensure([target, sup])
    reopened.inherit_projects([(sup, target)])
    assert sup.handle == ("kept-sup-7" if first_write == "set_project" else "pick-sup-7")


def test_schema_writes_do_not_reclassify_already_inherited_supervisors(store, tmp_path):
    target, sup = item("target"), item("sup", supervises="target")
    sup.cwd = str(tmp_path / "uuid")
    store.ensure([target, sup])
    store.inherit_projects([(sup, target)])
    reopened = HandleStore(store.path)
    reopened.add_project("extra", tmp_path / "extra")
    reopened.set_project(target.project_handle, "ren")
    reopened.ensure([target, sup])
    reopened.assign(target, "extra")
    reopened.inherit_projects([(sup, target)])
    assert sup.project_handle == "extra"


def test_automatic_inheritance_follows_repeated_target_assignments(store, tmp_path):
    import sqlite3

    target, sup = item("target"), item("sup", supervises="target")
    sup.cwd = str(tmp_path / "uuid")
    store.ensure([target, sup])
    for project in ("one", "two", "three"):
        store.add_project(project, tmp_path / project)
        store.assign(target, project)
        store.inherit_projects([(sup, target)])
        assert sup.project_handle == project
        with sqlite3.connect(store.path) as connection:
            assert connection.execute(
                "SELECT project_explicit FROM handle_logicals WHERE handle=?", (sup.handle,)
            ).fetchone() == (0,)


def test_generic_role_uses_ses_for_assign_rename_and_inherit(store, tmp_path):
    generic, target = item("generic", "ordinary title"), item("target")
    generic.cwd = str(tmp_path / "uuid")
    store.ensure([generic, target])
    assert generic.handle == "uuid-ses-1"
    assert generic.session_role == "session"
    store.inherit_projects([(generic, target)])
    assert generic.handle == "elev-ses-1"
    store.set_project("elev", "el")
    store.ensure([generic])
    assert generic.handle == "el-ses-1"
    store.assign(generic, "el", role="ses", number=4)
    assert generic.handle == "el-ses-4"
    assert generic.session_role == "session"


@pytest.mark.parametrize("legacy_schema", [False, True])
def test_migrate_generated_long_generic_handles_preserving_custom_and_claims(store, legacy_schema):
    import sqlite3

    generic = item("generic", "ordinary")
    custom = item("custom", "ordinary")
    explicit = item("explicit", "ordinary")
    store.ensure([generic, custom, explicit])
    store.set_session(custom, "chosen-session-9")
    store.set_session(explicit, explicit.handle)
    # Reconstruct old generated spellings and reservations without depending
    # on the new allocator to manufacture the legacy database under test.
    with sqlite3.connect(store.path) as connection:
        for row in connection.execute(
            "SELECT id,number FROM handle_logicals WHERE handle<>?", (custom.handle,)
        ).fetchall():
            name = f"elev-session-{row[1]}"
            connection.execute("UPDATE handle_logicals SET handle=? WHERE id=?", (name, row[0]))
            connection.execute("INSERT OR IGNORE INTO handle_claims VALUES (?,?)", (name, row[0]))
            if row[1] == 1:
                connection.execute(
                    "INSERT INTO handle_claims VALUES (?,?)", ("elev-session", row[0])
                )
        if legacy_schema:
            connection.execute("DROP TRIGGER IF EXISTS handle_project_assignment_explicit")
            connection.execute("ALTER TABLE handle_logicals DROP COLUMN project_explicit")
    reopened = HandleStore(store.path)
    reopened.ensure([generic, custom, explicit])
    assert generic.handle == "elev-ses-3"
    assert custom.handle == "chosen-session-9"
    assert explicit.handle == ("elev-ses-2" if legacy_schema else "elev-session-2")
    assert reopened.claimed("elev-session-3")
    with pytest.raises(ValueError, match="retired"):
        reopened.resolve("elev-session-3", [generic])
    with pytest.raises(ValueError, match="reserved"):
        reopened.set_session(custom, "elev-session-3")
