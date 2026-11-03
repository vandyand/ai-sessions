import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from ai_sessions import app, conversion
from ai_sessions.compression import compress_file
from ai_sessions.conversion import BridgeError, native_session_availability
from ai_sessions.model import Availability, NativeRef, NativeSession, ReadSnapshot, Transcript
from ai_sessions.registry import REGISTRY


class ModelTests(unittest.TestCase):
    def test_compressed_is_carried_from_discovery(self) -> None:
        native = NativeSession("codex", "id", "t", "/w", 1.0, 1.0, "p", True, "s", compressed=True)
        self.assertTrue(app.session_from_native(native).compressed)

    def test_catalog_without_the_field_still_loads(self) -> None:
        native = NativeSession("codex", "id", "t", "/w", 1.0, 1.0, "p", True, "s")
        row = asdict(app.session_from_native(native))
        row.pop("compressed")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "catalog.json"
        path.write_text(
            app.json.dumps(
                {
                    "version": app.CATALOG_CACHE_VERSION,
                    "registry": app._catalog_signature(),
                    "sessions": [row],
                }
            ),
            encoding="utf-8",
        )
        loaded = app.load_session_catalog(path=path)
        self.assertEqual([item.compressed for item in loaded], [False])


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "s.jsonl"
        self.path.write_text('{"a":1}\n', encoding="utf-8")
        self.seen: list[NativeRef] = []

        def read(ref, *, latest_window=True):
            self.seen.append(ref)
            return ReadSnapshot(Transcript([]), 1)

        def since(ref, checkpoint):
            self.seen.append(ref)
            return ReadSnapshot(Transcript([]), 1)

        def probe(ref):
            from ai_sessions.compression import is_compressed_path, resolve_storage

            found = resolve_storage(Path(ref.storage))
            if found is None:
                return Availability.UNAVAILABLE
            return Availability.COMPRESSED if is_compressed_path(found) else Availability.AVAILABLE

        adapter = replace(REGISTRY.get("codex"), availability=probe, read=read, read_since=since)
        self.context = REGISTRY.temporary(adapter)
        self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)

    def test_availability_reports_compressed(self) -> None:
        compressed = compress_file(self.path)
        self.assertEqual(
            native_session_availability("codex", NativeRef("id", str(self.path))), "compressed"
        )
        self.assertEqual(
            native_session_availability("codex", NativeRef("id", str(compressed))), "compressed"
        )

    def test_reads_refuse_a_compressed_session(self) -> None:
        compress_file(self.path)
        ref = NativeRef("id", str(self.path))
        with self.assertRaisesRegex(BridgeError, "compressed; decompress it first"):
            conversion.read_snapshot("codex", ref)
        with self.assertRaisesRegex(BridgeError, "compressed; decompress it first"):
            conversion.read_since("codex", ref, 0)
        self.assertEqual(self.seen, [])

    def test_a_reference_to_the_old_compressed_path_reads_the_plain_file(self) -> None:
        stale = NativeRef("id", str(self.path) + ".zst")
        conversion.read_snapshot("codex", stale)
        conversion.read_since("codex", stale, 0)
        self.assertEqual({ref.storage for ref in self.seen}, {str(self.path)})


if __name__ == "__main__":
    unittest.main()
