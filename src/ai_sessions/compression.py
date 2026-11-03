"""Compress and restore session transcripts without ever risking their content.

A compressed transcript is ``<name>.jsonl.zst`` beside where ``<name>.jsonl`` was: the form
some harnesses already write themselves.  When both exist the plain file is authoritative.

The original goes away only after the compressed copy has been read back and found
identical, and only while nothing else has it open.  Every failure leaves it in place.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import io
import os
import shutil
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import BinaryIO

import zstandard

ZST_SUFFIX = ".zst"
COMPRESSION_LEVEL = 3
_CHUNK = 1 << 20
_INPUT_CHUNK = 1 << 16
# Room to keep free beyond the file itself, so that restoring one session cannot fill a disk.
_SPARE_BYTES = 256 * 1 << 20

if os.name == "nt":
    import msvcrt
    from ctypes import wintypes

    _GENERIC_READ = 0x8000_0000
    _DELETE = 0x0001_0000
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _FILE_DISPOSITION_INFO = 4
    _ERROR_SHARING_VIOLATION = 32
    _INVALID_HANDLE = wintypes.HANDLE(-1).value
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.SetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
else:
    _ERROR_SHARING_VIOLATION = 0


class CompressionError(Exception):
    """A session could not be compressed or restored; its files are as they were."""


def is_compressed_path(path: Path | str) -> bool:
    return str(path).endswith(ZST_SUFFIX)


def compressed_path(path: Path) -> Path:
    return path if is_compressed_path(path) else path.with_name(path.name + ZST_SUFFIX)


def plain_path(path: Path) -> Path:
    return path.with_name(path.name[: -len(ZST_SUFFIX)]) if is_compressed_path(path) else path


def resolve_storage(path: Path) -> Path | None:
    """The file holding this session now, in either form; the plain file wins."""
    plain = plain_path(path)
    if plain.is_file():
        return plain
    compressed = compressed_path(path)
    return compressed if compressed.is_file() else None


def _temporary_name(path: Path) -> str:
    """A name no session scan matches."""
    return f".{path.name}.{uuid.uuid4().hex}.partial"


def _hash_stream(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(_CHUNK):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _frames(raw: BinaryIO, name: str) -> Iterator[bytes]:
    """Every frame of a compressed stream, refusing one that stops early.

    The stream reader treats a truncated frame as a normal end of file.  Input goes in small
    pieces because the decompressor's cost grows with the square of what one call produces.
    """
    pending = raw.read(_INPUT_CHUNK)
    if not pending:
        raise CompressionError(f"{name} is empty")
    while pending:
        frame = zstandard.ZstdDecompressor().decompressobj()
        while True:
            if restored := frame.decompress(pending):
                yield restored
            if frame.eof:
                pending = frame.unused_data or raw.read(_INPUT_CHUNK)
                break
            pending = raw.read(_INPUT_CHUNK)
            if not pending:
                raise CompressionError(f"{name} is incomplete")


def _decompressed(path: Path) -> Iterator[bytes]:
    with path.open("rb") as raw:
        yield from _frames(raw, path.name)


def _hash_compressed(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in _decompressed(path):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _sync(handle: BinaryIO) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def _is_sharing_violation(error: OSError) -> bool:
    return getattr(error, "winerror", None) == _ERROR_SHARING_VIOLATION


@contextlib.contextmanager
def _exclusive(path: Path) -> Iterator[BinaryIO]:
    """Open ``path`` so that nothing else can have it open, on systems that can promise that.

    Ordinary deletion succeeds on Windows against a file another process is appending to, whose
    next writes then vanish.  Refusing to share is the only reliable way to see such a writer.
    """
    if os.name != "nt":
        with path.open("rb") as handle:
            yield handle
        return
    handle = _kernel32.CreateFileW(
        str(path), _GENERIC_READ | _DELETE, 0, None, _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None
    )
    if handle == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    with os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY), "rb") as stream:
        yield stream


def _delete_open(stream: BinaryIO, path: Path) -> None:
    """Remove the file behind an exclusive handle; it goes when the handle closes."""
    if os.name != "nt":
        path.unlink()
        return
    delete = ctypes.c_ubyte(1)
    handle = msvcrt.get_osfhandle(stream.fileno())
    if not _kernel32.SetFileInformationByHandle(
        handle, _FILE_DISPOSITION_INFO, ctypes.byref(delete), 1
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _extends(stream: BinaryIO, restored: Iterable[bytes]) -> bool:
    """Whether ``stream`` holds, from its start, everything an intact compressed copy yields."""
    stream.seek(0)
    try:
        for chunk in restored:
            if stream.read(len(chunk)) != chunk:
                return False
    except (CompressionError, zstandard.ZstdError):
        return False
    return True


def hides_history(path: Path) -> bool:
    """Whether a compressed copy holds something the plain file that outranks it does not."""
    plain = plain_path(path)
    compressed = compressed_path(path)
    if not (plain.is_file() and compressed.is_file()):
        return False
    try:
        with plain.open("rb") as stream:
            return not _extends(stream, _decompressed(compressed))
    except OSError:
        return False


def _different(path: Path) -> CompressionError:
    return CompressionError(
        f"{path.name} and its compressed copy differ, so both are kept; "
        "the compressed copy may hold history the plain file does not"
    )


def compress_file(path: Path, *, level: int = COMPRESSION_LEVEL) -> Path:
    """Replace ``path`` with a verified ``.zst`` copy that keeps its modification time.

    An existing copy is replaced only by a file that contains all of it.
    """
    target = compressed_path(path)
    temporary = path.with_name(_temporary_name(path))
    try:
        with _exclusive(path) as source:
            if target.is_file() and not _extends(source, _decompressed(target)):
                raise _different(path)
            source.seek(0)
            before = os.fstat(source.fileno())
            compressor = zstandard.ZstdCompressor(
                level=level, write_checksum=True, write_content_size=True
            )
            digest = hashlib.sha256()
            size = 0
            with temporary.open("wb") as output:
                writer = compressor.stream_writer(output, size=before.st_size, closefd=False)
                with writer:
                    while chunk := source.read(_CHUNK):
                        digest.update(chunk)
                        size += len(chunk)
                        writer.write(chunk)
                _sync(output)
            if size != before.st_size:
                raise CompressionError(f"{path.name} changed while it was being compressed")
            if _hash_compressed(temporary) != (digest.hexdigest(), size):
                raise CompressionError(f"could not verify the compressed copy of {path.name}")
            after = path.stat()
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                raise CompressionError(f"{path.name} changed while it was being compressed")
            os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
            os.replace(temporary, target)
            try:
                _delete_open(source, path)
            except OSError as error:
                # Both forms hold the session now, and the plain file wins until a retry.
                if _is_sharing_violation(error) or error.errno == _ERROR_SHARING_VIOLATION:
                    raise CompressionError(f"{path.name} is in use, so it stays plain") from error
                raise CompressionError(f"could not remove {path.name}: {error}") from error
    except CompressionError:
        _discard(temporary)
        raise
    except (OSError, zstandard.ZstdError) as error:
        _discard(temporary)
        if isinstance(error, OSError) and _is_sharing_violation(error):
            raise CompressionError(f"{path.name} is in use, so it stays plain") from error
        raise CompressionError(f"could not compress {path.name}: {error}") from error
    return target


def _publish_new(temporary: Path, target: Path) -> None:
    """Give ``temporary`` its final name, failing if something else got there first."""
    if os.name == "nt":
        os.rename(temporary, target)
    else:
        os.link(temporary, target)
        temporary.unlink()


def decompress_file(path: Path) -> Path:
    """Restore the plain file, keeping the ``.zst`` until the result is verified.

    A plain file that contains the compressed copy is left as it is, along with the copy.  One
    that does not contain it is reported, because the copy then holds history the plain file
    is hiding.
    """
    source = compressed_path(path)
    target = plain_path(path)
    temporary = target.with_name(_temporary_name(target))
    try:
        if target.is_file() and not source.is_file():
            return target
        # Held throughout: a compress finishing beside us must not have its new copy removed.
        with _exclusive(source) as held:
            if target.is_file():
                return _kept(target, held, source)
            before = os.fstat(held.fileno())
            _require_room(held, source, before.st_size)
            digest = hashlib.sha256()
            size = 0
            held.seek(0)
            with temporary.open("wb") as output:
                for chunk in _frames(held, source.name):
                    digest.update(chunk)
                    size += len(chunk)
                    output.write(chunk)
                _sync(output)
            with temporary.open("rb") as written:
                if _hash_stream(written) != (digest.hexdigest(), size):
                    raise CompressionError(f"could not verify the restored {target.name}")
            os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
            try:
                _publish_new(temporary, target)
            except FileExistsError:
                # Something restored it meanwhile and may already have written to it.
                _discard(temporary)
                return _kept(target, held, source)
            try:
                _delete_open(held, source)
            except OSError:
                # The plain file already wins, so a leftover copy does no harm.
                pass
    except CompressionError:
        _discard(temporary)
        raise
    except (OSError, zstandard.ZstdError) as error:
        _discard(temporary)
        if isinstance(error, OSError) and _is_sharing_violation(error):
            raise CompressionError(f"{source.name} is in use; try again shortly") from error
        raise CompressionError(f"could not decompress {source.name}: {error}") from error
    return target


def _kept(target: Path, held: BinaryIO, source: Path) -> Path:
    """Accept a plain file that was already there, unless it hides what the copy holds."""
    held.seek(0)
    with target.open("rb") as existing:
        if not _extends(existing, _frames(held, source.name)):
            raise _different(target)
    return target


def _content_size(handle: BinaryIO) -> int | None:
    handle.seek(0)
    header = handle.read(18)
    try:
        size = zstandard.get_frame_parameters(header).content_size
    except zstandard.ZstdError:
        return None
    return None if size == zstandard.CONTENTSIZE_UNKNOWN else size


def _require_room(handle: BinaryIO, path: Path, compressed_size: int) -> None:
    # A frame without a recorded size is estimated at this ratio, the highest seen in practice.
    needed = _content_size(handle) or compressed_size * 12
    free = shutil.disk_usage(path.parent).free
    if free < needed + _SPARE_BYTES:
        raise CompressionError(
            f"not enough disk space to decompress {path.name}: "
            f"{needed >> 20} MB needed, {free >> 20} MB free"
        )


class _ScanReader:
    """Forward-only reading of a compressed transcript with plain-file positions."""

    def __init__(self, path: Path) -> None:
        self._raw = path.open("rb")
        self._reader = zstandard.ZstdDecompressor().stream_reader(self._raw)
        self._buffered = io.BufferedReader(self._reader, _CHUNK)
        self._position = 0

    def readline(self) -> bytes:
        line = self._buffered.readline()
        self._position += len(line)
        return line

    def read(self, size: int = -1) -> bytes:
        data = self._buffered.read(size)
        self._position += len(data)
        return data

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int) -> int:
        if offset != self._position:
            raise OSError("a compressed transcript can only be read from the start")
        return offset

    def __enter__(self) -> _ScanReader:
        return self

    def __exit__(self, *_details: object) -> None:
        self._buffered.close()
        self._raw.close()


def open_for_scan(path: Path) -> BinaryIO | _ScanReader:
    """Open a transcript of either form for sequential binary reading."""
    return _ScanReader(path) if is_compressed_path(path) else path.open("rb")


def text_lines(path: Path) -> Iterator[str]:
    """Yield a transcript's lines from either form without changing the file."""
    if not is_compressed_path(path):
        with path.open(encoding="utf-8", errors="replace", newline="\n") as handle:
            yield from handle
        return
    decompressor = zstandard.ZstdDecompressor()
    with path.open("rb") as raw, decompressor.stream_reader(raw) as reader:
        buffered = io.BufferedReader(reader, _CHUNK)
        yield from io.TextIOWrapper(buffered, encoding="utf-8", errors="replace", newline="\n")


def read_prefix(path: Path, size: int) -> bytes:
    """The first ``size`` bytes of a compressed transcript, stopping as soon as they are read."""
    decompressor = zstandard.ZstdDecompressor()
    with path.open("rb") as raw, decompressor.stream_reader(raw) as reader:
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = reader.read(min(remaining, _CHUNK))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    return b"".join(chunks)
