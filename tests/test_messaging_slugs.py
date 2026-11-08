import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ai_sessions.messaging import cli, store


class MessagingSlugTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch.object(store, "ROOT", self.root / "messaging"))
        from ai_sessions.references import SlugStore

        self.slugs = SlugStore(self.root / "slugs.sqlite3")
        self.enterContext(patch.object(cli, "_slug_store", return_value=self.slugs))
        self.a = dict(
            tool="sample",
            session_id="same",
            home=str(self.root / "one"),
            generation="first",
            name="Worker",
            slug="worker",
        )
        self.b = dict(
            tool="sample",
            session_id="same",
            home=str(self.root / "two"),
            generation="second",
            name="Worker",
            slug="worker-2",
        )

    def test_slug_exact_matches_precede_names_and_refuse_identity_collisions(self):
        named = dict(tool="other", session_id="different", name="worker")
        self.assertIs(store.resolve("worker", [self.a, named]), self.a)
        self.assertIs(store.resolve("worker-2", [self.a, self.b]), self.b)
        self.assertIs(store.resolve(store.endpoint_id(self.b), [self.a, self.b]), self.b)
        for target in ("same", "sample:same"):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "ambiguous"):
                store.resolve(target, [self.a, self.b])
        conflicting = {**named, "session_id": "worker"}
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            store.resolve("worker", [self.a, conflicting])

    def test_claimed_slugs_never_fall_back_to_other_live_names_or_ids(self):
        from ai_sessions.references import native_key

        key = native_key(self.a["tool"], self.a["session_id"], self.a["home"])
        self.slugs.ensure([(key, "foo")])
        named = {**self.b, "name": "foo", "slug": "other"}
        for retired in (False, True):
            if retired:
                self.slugs.set_slug(key, "bar")
            qualified = store.endpoint_id({**self.a, "session_id": "foo"})
            for target in ("foo", "sample:FOO", qualified.upper()):
                with self.subTest(retired=retired, target=target):
                    with self.assertRaises(ValueError):
                        store.resolve(target, [named], slugs=self.slugs)
            collision = {**named, "session_id": "foo"}
            with self.assertRaises(ValueError):
                store.resolve("foo", [collision], slugs=self.slugs)
        current = {**self.a, "slug": "bar"}
        self.assertIs(store.resolve("bar", [current, named], slugs=self.slugs), current)

    def test_send_and_sender_reject_retired_slug_before_transport(self):
        from ai_sessions.references import native_key

        key = native_key(self.a["tool"], self.a["session_id"], self.a["home"])
        self.slugs.ensure([(key, "foo")])
        self.slugs.set_slug(key, "bar")
        rows = [{**self.a, "slug": "bar"}, {**self.b, "name": "foo"}]
        with (
            patch.object(cli, "reachable", return_value=rows),
            patch.object(cli, "deliver") as delivery,
        ):
            for target, sender in (("foo", "bar"), ("bar", "foo")):
                with self.subTest(target=target, sender=sender), self.assertRaises(ValueError):
                    cli.send_message(target, "hello", sender)
            delivery.assert_not_called()
        self.assertFalse((store.ROOT / "receipts").exists())

    def test_registry_rename_overrides_cached_slugs_without_retargeting_homes(self):
        from ai_sessions.references import native_key

        key = native_key(self.a["tool"], self.a["session_id"], self.a["home"])
        self.slugs.ensure([(key, "old")])
        cached = [{**self.a, "slug": "old"}, self.b]
        self.slugs.set_slug(key, "new")
        for token in ("old", "sample:OLD", store.endpoint_id({**self.a, "session_id": "old"})):
            with self.subTest(token=token), self.assertRaises(ValueError):
                store.resolve(token, cached, slugs=self.slugs)
        for token in ("new", "sample:NEW", store.endpoint_id({**self.a, "session_id": "new"})):
            with self.subTest(token=token):
                self.assertIs(store.resolve(token, cached, slugs=self.slugs), cached[0])
                with self.assertRaises(ValueError):
                    store.resolve(token, [self.b], slugs=self.slugs)
        wrong_home = store.endpoint_id({**self.b, "session_id": "new"})
        with self.assertRaises(ValueError):
            store.resolve(wrong_home, cached, slugs=self.slugs)
        with (
            patch.object(cli, "reachable", return_value=cached),
            patch.object(cli, "deliver", return_value={"status": "accepted"}) as delivery,
        ):
            with self.assertRaises(ValueError):
                cli.send_message("old", "hello", "worker-2")
            delivery.assert_not_called()
            code, receipt = cli.send_message("new", "hello", "worker-2")
            self.assertEqual(code, 0)
            self.assertEqual(receipt["recipient"], cli._address(self.a))
            code, receipt = cli.send_message("worker-2", "hello", "sample:NEW")
            self.assertEqual(code, 0)
            self.assertEqual(receipt["sender"], cli._address(self.a))

    def test_slug_variants_ignore_case_and_keep_native_ids_case_sensitive(self):
        qualified = store.endpoint_id({**self.b, "session_id": self.b["slug"]})
        for target in ("WORKER-2", "sample:WORKER-2", qualified.upper()):
            with self.subTest(target=target):
                self.assertIs(store.resolve(target, [self.a, self.b]), self.b)
        for target in ("SAME", "sample:SAME"):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "unavailable"):
                store.resolve(target, [self.a, self.b])
        names = [{**row, "slug": "distinct-" + str(i)} for i, row in enumerate((self.a, self.b))]
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            store.resolve("Worker", names)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            store.resolve("WORKER", names)

    def test_qualified_slug_sender_preserves_native_reply_correlation(self):
        source = store.endpoint_id({**self.a, "session_id": self.a["slug"]}).upper()
        with (
            patch.object(cli, "reachable", return_value=[self.a, self.b]),
            patch.object(cli, "deliver", return_value={"status": "accepted"}),
        ):
            _, first = cli.send_message("sample:WORKER-2", "hello", source)
            self.assertEqual(first["sender"], cli._address(self.a))
            _, reply = cli.reply_message(first["message_id"], "received", "sample:WORKER-2")
            self.assertEqual(reply["recipient"], cli._address(self.a))
            self.assertEqual(
                store.receipt(first["message_id"])["acknowledged_by"], reply["message_id"]
            )

    def test_send_and_reply_from_slugs_keep_native_addresses_and_message_ids(self):
        with (
            patch.object(cli, "reachable", return_value=[self.a, self.b]),
            patch.object(cli, "deliver", return_value={"status": "accepted"}) as delivery,
        ):
            code, first = cli.send_message("worker-2", "hello", "worker")
            self.assertEqual(code, 0)
            self.assertEqual(first["sender"], cli._address(self.a))
            self.assertEqual(first["recipient"], cli._address(self.b))
            metadata = json.loads(delivery.call_args.args[1].splitlines()[1])
            self.assertEqual(metadata["sender"], first["sender"])
            self.assertEqual(metadata["recipient"], first["recipient"])
            code, reply = cli.reply_message(first["message_id"], "received", "worker-2")
            self.assertEqual(code, 0)
            self.assertEqual(reply["reply_to"], first["message_id"])
            self.assertEqual(reply["recipient"], first["sender"])
            receipt = store.receipt(first["message_id"])
            self.assertEqual(receipt["acknowledged_by"], reply["message_id"])
            self.assertEqual(receipt["status"], "acknowledged")
            with self.assertRaisesRegex(ValueError, "no live reply address"):
                cli.reply_message("worker", "not a message ID", "worker-2")

    def test_offline_slug_refuses_delivery(self):
        with (
            patch.object(cli, "reachable", return_value=[self.a]),
            patch.object(cli, "deliver") as delivery,
        ):
            with self.assertRaisesRegex(ValueError, "unavailable"):
                cli.send_message("worker-2", "hello", "worker")
            delivery.assert_not_called()
        self.assertFalse((store.ROOT / "receipts").exists())

    def test_cli_send_and_reply_accept_explicit_from_slug(self):
        with (
            patch.object(cli, "reachable", return_value=[self.a, self.b]),
            patch.object(cli, "deliver", return_value={"status": "accepted"}),
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    cli.main(
                        ["send", "worker-2", "--from", "worker", "--message", "hello", "--json"]
                    ),
                    0,
                )
            first = json.loads(output.getvalue())
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    cli.main(
                        [
                            "reply",
                            first["message_id"],
                            "--from",
                            "worker-2",
                            "--message",
                            "received",
                            "--json",
                        ]
                    ),
                    0,
                )
            reply = json.loads(output.getvalue())
            self.assertEqual(reply["sender"], first["recipient"])
            self.assertEqual(reply["reply_to"], first["message_id"])

    def test_reachable_allocates_in_shared_registry_without_catalog_or_identity_changes(self):
        from ai_sessions.references import SlugStore, native_key

        registry = SlugStore(self.root / "state.slugs.sqlite3")
        rows = [{k: v for k, v in row.items() if k != "slug"} for row in (self.a, self.b)]
        with (
            patch.object(cli, "_slug_store", return_value=registry),
            patch.object(store, "receivers", return_value=rows),
            patch("ai_sessions.registry.REGISTRY.adapters", return_value=()),
        ):
            live = cli.reachable()
            self.assertEqual(len({r["slug"] for r in live}), 2)
            for original, augmented in zip(rows, live):
                self.assertEqual(cli._address(original), cli._address(augmented))
                key = native_key(original["tool"], original["session_id"], original["home"])
                self.assertEqual(registry.lookup(augmented["slug"]), key)
            self.assertTrue(all("slug" not in row for row in rows))
            renamed = [{**row, "name": "Renamed"} for row in rows]
            with patch.object(store, "receivers", return_value=renamed):
                self.assertEqual([r["slug"] for r in cli.reachable()], [r["slug"] for r in live])
            registry.ensure([(native_key("sample", "offline", str(self.root)), "Offline")])
            with self.assertRaisesRegex(ValueError, "unavailable"):
                store.resolve("offline", live)

    def test_slug_sender_cannot_acknowledge_replacement_generation(self):
        with (
            patch.object(cli, "reachable", return_value=[self.a, self.b]),
            patch.object(cli, "deliver", return_value={"status": "accepted"}),
        ):
            _, first = cli.send_message("worker-2", "hello", "worker")
        with (
            patch.object(cli, "reachable", return_value=[self.a, {**self.b, "generation": "new"}]),
            patch.object(cli, "deliver") as delivery,
        ):
            with self.assertRaisesRegex(ValueError, "original recipient is unavailable"):
                cli.reply_message(first["message_id"], "hello", "worker-2")
            delivery.assert_not_called()

    def test_missing_or_native_id_only_names_use_session_fallback(self):
        from ai_sessions.references import SlugStore

        registry = SlugStore(self.root / "fallback.slugs.sqlite3")
        rows = [{**self.a, "name": None}, {**self.b, "name": self.b["session_id"]}]
        with (
            patch.object(cli, "_slug_store", return_value=registry),
            patch.object(store, "receivers", return_value=rows),
            patch("ai_sessions.registry.REGISTRY.adapters", return_value=()),
        ):
            self.assertEqual({r["slug"] for r in cli.reachable()}, {"session", "session-2"})

    def test_status_human_and_json_expose_slugs(self):
        with (
            patch.object(cli, "reachable", return_value=[self.a, self.b]),
            patch.object(cli.install, "settings", return_value={}),
        ):
            for flags in ([], ["--json"]):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.main(["messaging", "status", *flags]), 0)
                self.assertIn("worker-2", output.getvalue())
                self.assertIn("Worker", output.getvalue())


if __name__ == "__main__":
    unittest.main()
