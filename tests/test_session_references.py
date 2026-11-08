import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ai_sessions.model import Session
from ai_sessions.references import NativeKey, SlugStore, resolve_session


class SlugStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "state.slugs.sqlite3"
        self.store = SlugStore(self.path)

    def key(self, ident, *, tool="engine", home=None):
        return NativeKey(tool, ident, str(home or self.root / "home"))

    def test_duplicate_names_get_distinct_persistent_slugs(self):
        first, second = self.key("one"), self.key("two")
        assigned = self.store.ensure([(first, "Elevate Futures"), (second, "Elevate Futures")])
        self.assertEqual(set(assigned.values()), {"elevate-futures", "elevate-futures-2"})
        self.assertEqual(
            SlugStore(self.path).ensure([(first, "Changed title")])[first], assigned[first]
        )
        self.assertEqual(self.store.lookup(assigned[second]), second)

    def test_concurrent_allocators_cannot_claim_the_same_slug(self):
        def allocate(index):
            key = self.key(str(index))
            return SlugStore(self.path).ensure([(key, "Same title")])[key]

        with ThreadPoolExecutor(max_workers=8) as pool:
            result = list(pool.map(allocate, range(24)))
        self.assertEqual(len(set(result)), 24)
        self.assertIn("same-title", result)

    def test_same_native_session_has_one_slug_under_concurrent_allocation(self):
        key = self.key("one")

        def allocate(index):
            return SlugStore(self.path).ensure([(key, f"Title {index}")])[key]

        with ThreadPoolExecutor(max_workers=8) as pool:
            result = list(pool.map(allocate, range(16)))
        self.assertEqual(len(set(result)), 1)

    def test_harness_and_home_are_part_of_identity(self):
        keys = [
            self.key("one"),
            self.key("one", tool="other"),
            self.key("one", home=self.root / "other-home"),
        ]
        assigned = self.store.ensure([(key, "same") for key in keys])
        self.assertEqual(len(set(assigned.values())), 3)
        for key, slug in assigned.items():
            self.assertEqual(self.store.lookup(slug), key)

    def test_slug_change_is_independent_and_retired_slug_cannot_target_someone_else(self):
        first, second = self.key("one"), self.key("two")
        self.store.ensure([(first, "Original"), (second, "Other")])
        self.assertEqual(self.store.set_slug(first, "NEW-SLUG"), "new-slug")
        self.assertIsNone(self.store.lookup("original"))
        self.assertEqual(self.store.ensure([(first, "Renamed display")])[first], "new-slug")
        with self.assertRaises(ValueError):
            self.store.set_slug(second, "original")
        with self.assertRaises(ValueError):
            self.store.set_slug(second, "NEW-SLUG")
        self.assertEqual(self.store.set_slug(first, "original"), "original")

    def test_invalid_slugs_do_not_change_the_assignment(self):
        key = self.key("one")
        self.store.ensure([(key, "Original")])
        for slug in (
            "",
            "two words",
            "-leading",
            "trailing-",
            "double--dash",
            "with_underscore",
            "a" * 65,
            "../escape",
        ):
            with self.subTest(slug=slug), self.assertRaises(ValueError):
                self.store.set_slug(key, slug)
        self.assertEqual(self.store.slug_for(key), "original")

    def test_slug_cannot_hide_an_existing_native_id(self):
        first, second = self.key("reserved-native-id"), self.key("two")
        assigned = self.store.ensure([(first, "First"), (second, "reserved-native-id")])
        self.assertNotEqual(assigned[second], first.session_id)
        with self.assertRaises(ValueError):
            self.store.set_slug(second, first.session_id)

    def test_unicode_long_and_empty_titles_have_valid_bounded_slugs(self):
        import re

        titles = ["Café / futures", "界", "", "very long " * 30]
        assigned = self.store.ensure(
            [(self.key(str(index)), title) for index, title in enumerate(titles)]
        )
        for slug in assigned.values():
            self.assertLessEqual(len(slug), 64)
            self.assertIsNotNone(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug))
        self.assertIn("cafe-futures", assigned.values())

    def test_unavailable_registry_fails_clearly_without_resetting_it(self):
        bad = SlugStore(self.root)
        with self.assertRaisesRegex(ValueError, "slug registry is unavailable"):
            bad.ensure([(self.key("one"), "Title")])
        self.assertTrue(self.root.is_dir())

    def test_case_insensitive_lookup_and_duplicate_batch_entries(self):
        key = self.key("one")
        assigned = self.store.ensure([(key, "Original"), (key, "Later")])
        self.assertEqual(len(assigned), 1)
        self.assertEqual(self.store.lookup(assigned[key].upper()), key)
        self.assertIsNone(self.store.lookup("does-not-exist"))


class ResolverTests(unittest.TestCase):
    @staticmethod
    def item(ident, slug, title="Same title", tool="engine"):
        return Session(
            tool, ident, title, "/work", 1700000000, 1700000000, "", True, "storage", slug=slug
        )

    def test_id_and_slug_and_qualified_forms_resolve_the_same_record(self):
        item = self.item("native-one", "elevate-futures")
        for ref in (
            "native-one",
            "engine:native-one",
            "elevate-futures",
            "ELEVATE-FUTURES",
            "engine:elevate-futures",
        ):
            with self.subTest(ref=ref):
                self.assertIs(resolve_session(ref, [item]), item)

    def test_duplicate_names_do_not_prevent_exact_identity_resolution(self):
        first, second = self.item("one", "first"), self.item("two", "second")
        self.assertIs(resolve_session("first", [first, second]), first)
        with self.assertRaisesRegex(ValueError, "ambiguous|more than one"):
            resolve_session("Same title", [first, second])

    def test_identifiers_take_precedence_over_legacy_titles(self):
        first = self.item("native-one", "first")
        second = self.item("native-two", "second", title="first")
        self.assertIs(resolve_session("first", [first, second]), first)
        second.title = "native-one"
        self.assertIs(resolve_session("native-one", [first, second]), first)
        self.assertIs(resolve_session("native-two", [first, second]), second)

    def test_exact_legacy_unique_names_remain_accepted(self):
        item = self.item("one", "first", title="A descriptive name")
        self.assertIs(resolve_session("a descriptive name", [item]), item)

    def test_ambiguous_ids_and_cross_identifier_collisions_fail_closed(self):
        first, second = self.item("one", "first"), self.item("one", "second", tool="other")
        with self.assertRaisesRegex(ValueError, "ambiguous|more than one"):
            resolve_session("one", [first, second])
        self.assertIs(resolve_session("other:one", [first, second]), second)
        second.session_id = "first"
        with self.assertRaisesRegex(ValueError, "ambiguous|more than one"):
            resolve_session("first", [first, second])

    def test_partial_and_missing_references_do_not_select_a_record(self):
        item = self.item("native-one", "elevate-futures")
        for ref in ("elevate", "native", "missing", ""):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                resolve_session(ref, [item])


class StateSlugTests(unittest.TestCase):
    def test_state_assigns_and_preserves_slugs_without_catalog_ownership(self):
        from ai_sessions import fastjson
        from ai_sessions.app import UserState, load_session_catalog, save_session_catalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = UserState(root / "state.json")
            item = Session(
                "codex", "native", "My title", str(root), 1700000000, 1700000000, "", True, ""
            )
            state.apply([item], check_copies=False)
            self.assertEqual(item.slug, "my-title")
            from ai_sessions.references import native_key

            state.slugs.set_slug(native_key(item.tool, item.session_id), "chosen-slug")
            item.title = "Renamed title"
            item.slug = "stale-catalog-slug"
            state.apply([item], check_copies=False)
            self.assertEqual(item.slug, "chosen-slug")
            catalog = root / "catalog.json"
            save_session_catalog([item], catalog)
            self.assertNotIn("slug", fastjson.read(catalog)["sessions"][0])
            loaded = load_session_catalog(state, path=catalog)
            self.assertEqual(loaded[0].slug, "chosen-slug")

    def test_retired_slug_cannot_fall_back_to_someone_elses_display_name(self):
        from ai_sessions.references import assign_sessions, native_key

        with tempfile.TemporaryDirectory() as directory:
            store = SlugStore(Path(directory) / "slugs.sqlite3")
            first = Session(
                "codex", "first-id", "Original", "/work", 1700000000, 1700000000, "", True, ""
            )
            second = Session(
                "codex", "second-id", "Original", "/work", 1700000000, 1700000000, "", True, ""
            )
            assign_sessions([first, second], store)
            old = first.slug
            store.set_slug(native_key(first.tool, first.session_id), "new-slug")
            assign_sessions([first, second], store)
            second.title = old
            with self.assertRaisesRegex(ValueError, "retired|unavailable"):
                resolve_session(old, [first, second], store=store)
            self.assertIs(resolve_session("new-slug", [first, second], store=store), first)

    def test_materialized_member_does_not_inherit_another_copys_slug(self):
        from ai_sessions.app import UserState
        from ai_sessions.references import assign_sessions

        with tempfile.TemporaryDirectory() as directory:
            state = UserState(Path(directory) / "state.json")
            first = Session(
                "codex", "first-id", "Same title", "/work", 1700000000, 1700000000, "", True, ""
            )
            assign_sessions([first], state.slugs)
            second = state._session_for_member({"tool": "claude", "session_id": "second-id"}, first)
            self.assertTrue(second.slug)
            self.assertNotEqual(first.slug, second.slug)

    def test_resolver_refreshes_changed_slugs_against_previously_loaded_sessions(self):
        from ai_sessions.references import assign_sessions, native_key

        with tempfile.TemporaryDirectory() as directory:
            store = SlugStore(Path(directory) / "slugs.sqlite3")
            item = Session(
                "codex", "native-id", "Original", "/work", 1700000000, 1700000000, "", True, ""
            )
            assign_sessions([item], store)
            old = item.slug
            store.set_slug(native_key(item.tool, item.session_id), "new-slug")
            with self.assertRaisesRegex(ValueError, "retired"):
                resolve_session(old, [item], store=store)
            self.assertIs(resolve_session("new-slug", [item], store=store), item)

    def test_search_cache_changes_when_slug_changes(self):
        from ai_sessions.app import session_searchable

        item = Session(
            "codex",
            "native",
            "Title",
            "/work",
            1700000000,
            1700000000,
            "",
            True,
            "",
            slug="searchslugone",
        )
        self.assertIn("searchslugone", session_searchable(item))
        item.slug = "searchslugtwo"
        self.assertIn("searchslugtwo", session_searchable(item))
        self.assertNotIn("searchslugone", session_searchable(item))
