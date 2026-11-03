import contextlib
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import zstandard

from ai_sessions import compression
from ai_sessions.compression import (
    CompressionError,
    compress_file,
    compressed_path,
    decompress_file,
    is_compressed_path,
    plain_path,
    read_prefix,
    resolve_storage,
    text_lines,
)

BODY = "".join(f'{{"n":{n},"text":"é ⟦x⟧ 😀 {n}"}}\n' for n in range(5000)).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "session.jsonl"
        self.path.write_bytes(BODY)
        os.utime(self.path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        self.zst = self.root / "session.jsonl.zst"

    def names(self) -> list[str]:
        return sorted(item.name for item in self.root.iterdir())


class PathTests(Fixture):
    def test_path_helpers(self) -> None:
        self.assertEqual(compressed_path(self.path), self.zst)
        self.assertEqual(compressed_path(self.zst), self.zst)
        self.assertEqual(plain_path(self.zst), self.path)
        self.assertEqual(plain_path(self.path), self.path)
        self.assertTrue(is_compressed_path(self.zst))
        self.assertFalse(is_compressed_path(self.path))

    def test_resolve_storage_prefers_the_plain_file(self) -> None:
        self.assertEqual(resolve_storage(self.path), self.path)
        self.assertEqual(resolve_storage(self.zst), self.path)
        compress_file(self.path)
        self.assertEqual(resolve_storage(self.path), self.zst)
        self.assertEqual(resolve_storage(self.zst), self.zst)
        self.assertIsNone(resolve_storage(self.root / "missing.jsonl"))


class RoundTripTests(Fixture):
    def test_compress_replaces_the_file_and_keeps_the_time(self) -> None:
        self.assertEqual(compress_file(self.path), self.zst)
        self.assertEqual(self.names(), ["session.jsonl.zst"])
        self.assertLess(self.zst.stat().st_size, len(BODY))
        self.assertEqual(self.zst.stat().st_mtime_ns, 1_700_000_000_000_000_000)

    def test_round_trip_is_byte_identical_and_keeps_the_time(self) -> None:
        compress_file(self.path)
        self.assertEqual(decompress_file(self.zst), self.path)
        self.assertEqual(self.names(), ["session.jsonl"])
        self.assertEqual(digest(self.path.read_bytes()), digest(BODY))
        self.assertEqual(self.path.stat().st_mtime_ns, 1_700_000_000_000_000_000)

    def test_a_frame_without_size_or_checksum_decompresses(self) -> None:
        # Files written by a harness's own compressor can lack both.
        compressor = zstandard.ZstdCompressor(write_content_size=False, write_checksum=False)
        with self.zst.open("wb") as handle, compressor.stream_writer(handle) as writer:
            writer.write(BODY)
        self.path.unlink()
        decompress_file(self.zst)
        self.assertEqual(self.path.read_bytes(), BODY)

    def test_empty_file_round_trips(self) -> None:
        self.path.write_bytes(b"")
        compress_file(self.path)
        decompress_file(self.zst)
        self.assertEqual(self.path.read_bytes(), b"")

    def test_recompressing_replaces_a_stale_compressed_copy(self) -> None:
        compress_file(self.path)
        decompress_file(self.zst)
        with self.path.open("ab") as handle:
            handle.write(b'{"new":true}\n')
        stale = zstandard.ZstdCompressor().compress(BODY[:50])
        self.zst.write_bytes(stale)
        compress_file(self.path)
        decompress_file(self.zst)
        self.assertEqual(self.path.read_bytes(), BODY + b'{"new":true}\n')

    def test_decompress_with_plain_present_changes_nothing(self) -> None:
        self.zst.write_bytes(zstandard.ZstdCompressor().compress(BODY[:100]))
        self.assertEqual(decompress_file(self.zst), self.path)
        self.assertEqual(self.names(), ["session.jsonl", "session.jsonl.zst"])
        self.assertEqual(self.path.read_bytes(), BODY)


class FailureTests(Fixture):
    def assert_untouched(self) -> None:
        self.assertEqual(self.names(), ["session.jsonl"])
        self.assertEqual(self.path.read_bytes(), BODY)

    @unittest.skipIf(os.name == "nt", "Windows deletes through the held handle itself")
    def test_a_name_that_now_belongs_to_a_newer_file_is_not_deleted(self) -> None:
        with self.path.open("rb") as held:
            replacement = self.path.with_name("replacement")
            replacement.write_bytes(b"newer\n")
            os.replace(replacement, self.path)
            with self.assertRaises(FileExistsError):
                compression._delete_open(held, self.path)
        self.assertEqual(self.path.read_bytes(), b"newer\n")

    def test_write_during_compression_keeps_the_original(self) -> None:
        real = compression._hash_compressed

        def appended(*args, **kwargs):
            with self.path.open("ab") as handle:
                handle.write(b'{"late":true}\n')
            return real(*args, **kwargs)

        @contextlib.contextmanager
        def shared(path):
            with path.open("rb") as handle:
                yield handle

        with (
            patch.object(compression, "_exclusive", shared),
            patch.object(compression, "_hash_compressed", appended),
            self.assertRaisesRegex(CompressionError, "changed"),
        ):
            compress_file(self.path)
        self.assertEqual(self.names(), ["session.jsonl"])
        self.assertEqual(self.path.read_bytes(), BODY + b'{"late":true}\n')

    def test_verification_failure_keeps_the_original(self) -> None:
        with (
            patch.object(compression, "_hash_compressed", return_value=("bad", 0)),
            self.assertRaisesRegex(CompressionError, "verif"),
        ):
            compress_file(self.path)
        self.assert_untouched()

    @unittest.skipUnless(os.name == "nt", "share modes exist only on Windows")
    def test_a_file_held_open_by_anyone_is_left_alone(self) -> None:
        with (
            self.path.open("ab"),
            self.assertRaisesRegex(CompressionError, "in use"),
        ):
            compress_file(self.path)
        self.assert_untouched()

    def test_failing_to_remove_the_original_keeps_both_and_can_be_retried(self) -> None:
        with (
            patch.object(compression, "_delete_open", side_effect=PermissionError(32, "in use")),
            self.assertRaisesRegex(CompressionError, "in use"),
        ):
            compress_file(self.path)
        self.assertEqual(self.names(), ["session.jsonl", "session.jsonl.zst"])
        self.assertEqual(resolve_storage(self.path), self.path)
        compress_file(self.path)
        self.assertEqual(self.names(), ["session.jsonl.zst"])
        decompress_file(self.zst)
        self.assertEqual(self.path.read_bytes(), BODY)

    def test_crash_after_publishing_leaves_the_plain_file_authoritative(self) -> None:
        with (
            patch.object(compression, "_delete_open", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            compress_file(self.path)
        self.assertEqual(resolve_storage(self.path), self.path)
        self.assertEqual(self.path.read_bytes(), BODY)

    def test_a_short_plain_file_never_replaces_the_full_copy(self) -> None:
        compress_file(self.path)
        packed = self.zst.read_bytes()
        self.path.write_bytes(b'{"short":true}\n')
        with self.assertRaisesRegex(CompressionError, "differ"):
            compress_file(self.path)
        self.assertEqual(self.zst.read_bytes(), packed)
        self.assertEqual(self.path.read_bytes(), b'{"short":true}\n')

    def test_a_plain_file_that_is_not_the_same_history_is_refused(self) -> None:
        compress_file(self.path)
        self.path.write_bytes(BODY[:-1] + b"X" + b"more\n")
        with self.assertRaisesRegex(CompressionError, "differ"):
            compress_file(self.path)
        self.assertEqual(self.names(), ["session.jsonl", "session.jsonl.zst"])

    def test_a_plain_file_that_extends_its_old_copy_replaces_it(self) -> None:
        compress_file(self.path)
        self.path.write_bytes(BODY + b'{"new":true}\n')
        compress_file(self.path)
        self.assertEqual(self.names(), ["session.jsonl.zst"])
        decompress_file(self.zst)
        self.assertEqual(self.path.read_bytes(), BODY + b'{"new":true}\n')

    def test_a_plain_file_equal_to_its_copy_replaces_it(self) -> None:
        compress_file(self.path)
        self.path.write_bytes(BODY)
        compress_file(self.path)
        self.assertEqual(self.names(), ["session.jsonl.zst"])

    def test_decompressing_over_a_different_plain_file_is_refused(self) -> None:
        compress_file(self.path)
        self.path.write_bytes(b'{"short":true}\n')
        with self.assertRaisesRegex(CompressionError, "differ"):
            decompress_file(self.zst)
        self.assertEqual(self.names(), ["session.jsonl", "session.jsonl.zst"])

    def test_a_truncated_copy_is_refused_and_kept(self) -> None:
        compress_file(self.path)
        data = self.zst.read_bytes()
        for size in (0, 10, len(data) * 3 // 4, len(data) - 1):
            self.zst.write_bytes(data[:size])
            with self.assertRaises(CompressionError):
                decompress_file(self.zst)
            self.assertEqual(self.names(), ["session.jsonl.zst"])
            self.assertEqual(self.zst.read_bytes(), data[:size])

    def test_every_frame_of_a_multi_frame_copy_is_restored(self) -> None:
        self.path.unlink()
        packer = zstandard.ZstdCompressor(write_content_size=True)
        self.zst.write_bytes(packer.compress(BODY[:900]) + packer.compress(BODY[900:]))
        decompress_file(self.zst)
        self.assertEqual(self.path.read_bytes(), BODY)

    def test_an_empty_transcript_round_trips(self) -> None:
        self.path.write_bytes(b"")
        compress_file(self.path)
        decompress_file(self.zst)
        self.assertEqual(self.path.read_bytes(), b"")

    def test_a_file_restored_meanwhile_is_not_overwritten(self) -> None:
        compress_file(self.path)
        real = compression._sync

        def restored_elsewhere(handle):
            real(handle)
            self.path.write_bytes(BODY + b'{"new":true}\n')

        with patch.object(compression, "_sync", restored_elsewhere):
            decompress_file(self.zst)
        self.assertEqual(self.path.read_bytes(), BODY + b'{"new":true}\n')
        self.assertEqual(self.names(), ["session.jsonl", "session.jsonl.zst"])

    def test_a_compress_racing_a_restore_cannot_destroy_both_copies(self) -> None:
        compress_file(self.path)
        real = compression._publish_new

        def compress_meanwhile(temporary, target):
            real(temporary, target)
            with contextlib.suppress(CompressionError):
                compress_file(self.path)

        with patch.object(compression, "_publish_new", compress_meanwhile):
            decompress_file(self.zst)
        self.assertEqual(resolve_storage(self.path), self.path)
        self.assertEqual(self.path.read_bytes(), BODY)

    def test_a_short_file_appearing_during_a_restore_is_reported(self) -> None:
        compress_file(self.path)
        real = compression._sync

        def appeared(handle):
            real(handle)
            self.path.write_bytes(b'{"tail":true}\n')

        with (
            patch.object(compression, "_sync", appeared),
            self.assertRaisesRegex(CompressionError, "differ"),
        ):
            decompress_file(self.zst)
        self.assertEqual(self.names(), ["session.jsonl", "session.jsonl.zst"])

    def test_hidden_history_is_noticed(self) -> None:
        self.assertFalse(compression.hides_history(self.path))
        compress_file(self.path)
        self.assertFalse(compression.hides_history(self.path))
        self.path.write_bytes(BODY + b'{"new":true}\n')
        self.assertFalse(compression.hides_history(self.path))
        self.path.write_bytes(b'{"tail":true}\n')
        self.assertTrue(compression.hides_history(self.path))

    def test_failing_to_remove_for_another_reason_says_so(self) -> None:
        with (
            patch.object(compression, "_delete_open", side_effect=PermissionError(5, "denied")),
            self.assertRaisesRegex(CompressionError, "could not remove"),
        ):
            compress_file(self.path)

    def test_decompression_feeds_small_pieces(self) -> None:
        self.assertLessEqual(compression._INPUT_CHUNK, 1 << 16)

    def test_corrupt_compressed_file_is_kept(self) -> None:
        compress_file(self.path)
        data = bytearray(self.zst.read_bytes())
        data[len(data) // 2] ^= 0xFF
        self.zst.write_bytes(bytes(data))
        with self.assertRaises(CompressionError):
            decompress_file(self.zst)
        self.assertEqual(self.names(), ["session.jsonl.zst"])
        self.assertEqual(self.zst.read_bytes(), bytes(data))

    def test_not_enough_disk_space_is_reported_before_writing(self) -> None:
        compress_file(self.path)
        usage = compression.shutil.disk_usage(self.root)
        with (
            patch.object(compression.shutil, "disk_usage", return_value=usage._replace(free=1)),
            self.assertRaisesRegex(CompressionError, "space"),
        ):
            decompress_file(self.zst)
        self.assertEqual(self.names(), ["session.jsonl.zst"])

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(CompressionError):
            compress_file(self.root / "missing.jsonl")
        with self.assertRaises(CompressionError):
            decompress_file(self.root / "missing.jsonl.zst")

    def test_temporary_files_never_look_like_sessions(self) -> None:
        name = compression._temporary_name(self.path)
        self.assertFalse(name.endswith((".jsonl", ".zst")))


class ReadingTests(Fixture):
    def test_text_lines_reads_either_form_without_changing_it(self) -> None:
        plain = list(text_lines(self.path))
        compress_file(self.path)
        before = self.zst.stat().st_mtime_ns
        self.assertEqual(list(text_lines(self.zst)), plain)
        self.assertEqual(self.names(), ["session.jsonl.zst"])
        self.assertEqual(self.zst.stat().st_mtime_ns, before)

    def test_open_for_scan_matches_a_plain_file_line_by_line(self) -> None:
        with compression.open_for_scan(self.path) as handle:
            handle.seek(0)
            plain = [(handle.readline(), handle.tell()) for _ in range(3)]
        compress_file(self.path)
        with compression.open_for_scan(self.zst) as handle:
            handle.seek(0)
            scanned = [(handle.readline(), handle.tell()) for _ in range(3)]
            with self.assertRaises(OSError):
                handle.seek(0)
        self.assertEqual(scanned, plain)

    def test_read_prefix_stops_early(self) -> None:
        compress_file(self.path)
        self.assertEqual(read_prefix(self.zst, 100), BODY[:100])
        self.assertEqual(read_prefix(self.zst, 10 * len(BODY)), BODY)


if __name__ == "__main__":
    unittest.main()
