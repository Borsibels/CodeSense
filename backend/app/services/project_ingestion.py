"""Secure ZIP ingestion: validate an untrusted archive without extracting it.

Nothing is written to disk. The archive stays in memory as the bytes the client
sent; member *metadata* is validated up front and member *content* is only
decompressed on demand through a bounded stream (:meth:`ProjectArchive.read`).
Nothing is imported or executed.

Checks, in order (each failure raises :class:`ArchiveError` with a stable code):

1. size of the archive itself, ZIP signature, and the entry count read straight
   from the end-of-central-directory record, *before* the directory is parsed
2. per entry: encryption, compression method (stored/deflate only), special
   files and symlinks (from the Unix mode bits), path safety, per-entry size
3. across entries: duplicate and case-insensitive/Unicode path collisions, a
   file that is also a directory, total declared size, overlapping entries
   (the "quine"/overlap bomb trick), and compression ratios
4. while reading: actual decompressed size must equal the declared size

ZIP metadata is never trusted: sizes are re-measured while streaming and the
read is cut off at the limit, so a lying header cannot inflate memory use.
"""

from __future__ import annotations

import io
import re
import stat
import struct
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass, field

from app.config import ProjectSettings

_LOCAL_HEADER = b"PK\x03\x04"
_EMPTY_ARCHIVE = b"PK\x05\x06"
_EOCD = b"PK\x05\x06"
_EOCD_MIN = 22
_CHUNK = 64 * 1024
_ALLOWED_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
_DRIVE = re.compile(r"^[A-Za-z]:")


class ArchiveError(Exception):
    """The archive was rejected. ``code`` is stable; ``message`` is safe to show."""

    def __init__(self, code: str, message: str, *, status: int = 422) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


def _show(name: str) -> str:
    """Printable, bounded rendering of an untrusted entry name for error messages."""
    text = ascii(name)
    return text if len(text) <= 80 else text[:77] + "...'"


def _mib(value: int) -> str:
    return f"{value / (1024 * 1024):.1f} MiB"


def normalize_member_path(raw: str, limits: ProjectSettings) -> tuple[str, bool]:
    """Validate and normalise an archive member name -> ``(portable_path, is_directory)``.

    Backslashes are treated as separators (Windows tools write them), so the
    result is identical on every platform. Anything that would need "fixing" to
    be safe is rejected rather than silently repaired.
    """
    if not raw:
        raise ArchiveError("UNSAFE_PATH", "The archive contains an entry with an empty name.")
    name = raw.replace("\\", "/")
    shown = _show(raw)

    for ch in name:
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            raise ArchiveError(
                "UNSAFE_PATH", f"Entry {shown} contains a control or invalid character."
            )
    if name.startswith("/"):
        raise ArchiveError("UNSAFE_PATH", f"Entry {shown} is an absolute path.")
    if _DRIVE.match(name):
        raise ArchiveError("UNSAFE_PATH", f"Entry {shown} starts with a Windows drive letter.")
    if ":" in name:
        raise ArchiveError(
            "UNSAFE_PATH", f"Entry {shown} contains ':' (drive or alternate-data-stream syntax)."
        )

    is_dir = name.endswith("/")
    parts = name[:-1].split("/") if is_dir else name.split("/")
    for part in parts:
        if part == "..":
            raise ArchiveError("UNSAFE_PATH", f"Entry {shown} escapes the project (path traversal).")
        if part in ("", "."):
            raise ArchiveError(
                "UNSAFE_PATH", f"Entry {shown} has an empty or '.' path component."
            )
        if part != part.rstrip(" ."):
            raise ArchiveError(
                "UNSAFE_PATH",
                f"Entry {shown} has a component ending in a space or dot "
                "(Windows would silently rename it).",
            )
    if len(parts) > limits.max_path_depth:
        raise ArchiveError(
            "UNSAFE_PATH", f"Entry {shown} is nested deeper than {limits.max_path_depth} levels."
        )
    path = "/".join(parts)
    if len(path) > limits.max_path_chars:
        raise ArchiveError(
            "UNSAFE_PATH", f"Entry {shown} is longer than {limits.max_path_chars} characters."
        )
    return path, is_dir


def _collision_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


@dataclass(frozen=True)
class ArchiveMember:
    """A validated file entry (still compressed)."""

    path: str
    size: int  # declared uncompressed size
    compressed_size: int
    _info: zipfile.ZipInfo = field(repr=False, compare=False)


@dataclass
class ProjectArchive:
    """A validated archive. Members are sorted by path; contents are read lazily."""

    members: tuple[ArchiveMember, ...]
    archive_bytes: int
    entry_count: int  # files + directories
    uncompressed_bytes: int  # declared, all files
    _zip: zipfile.ZipFile = field(repr=False)
    _read_bytes: int = 0  # actually decompressed so far

    @property
    def decompressed_bytes(self) -> int:
        return self._read_bytes

    def read(self, member: ArchiveMember, max_bytes: int) -> bytes:
        """Decompress one member through a bounded stream.

        Raises :class:`ArchiveError` if the real size differs from the declared
        size, exceeds ``max_bytes``, or the data is corrupt (bad CRC, bad stream).
        """
        if member.size > max_bytes:
            raise ArchiveError("ENTRY_TOO_LARGE", f"Entry {_show(member.path)} is too large to read.")
        chunks: list[bytes] = []
        total = 0
        try:
            with self._zip.open(member._info) as stream:
                while True:
                    chunk = stream.read(min(_CHUNK, max_bytes + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes or total > member.size:
                        raise ArchiveError(
                            "SUSPICIOUS_COMPRESSION",
                            f"Entry {_show(member.path)} decompresses to more than its declared size.",
                        )
                    chunks.append(chunk)
        except ArchiveError:
            raise
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError, RuntimeError, NotImplementedError,
                ValueError, struct.error, OverflowError):
            raise ArchiveError(
                "INVALID_ARCHIVE", f"Entry {_show(member.path)} is corrupt and could not be read."
            ) from None
        if total != member.size:
            raise ArchiveError(
                "INVALID_ARCHIVE",
                f"Entry {_show(member.path)} is truncated: expected {member.size} bytes, got {total}.",
            )
        self._read_bytes += total
        return b"".join(chunks)

    def close(self) -> None:
        self._zip.close()


def _declared_entry_count(data: bytes) -> int | None:
    """Entry count from the end-of-central-directory record, or None if not found.

    Read before ``zipfile`` parses the central directory so an archive made of
    hundreds of thousands of empty entries is rejected without building them.
    """
    tail_start = max(0, len(data) - (_EOCD_MIN + 0xFFFF))
    index = data.rfind(_EOCD, tail_start)
    if index == -1 or index + _EOCD_MIN > len(data):
        return None
    return struct.unpack_from("<H", data, index + 10)[0]


def open_archive(data: bytes, limits: ProjectSettings) -> ProjectArchive:
    """Validate ``data`` as a project archive; raise :class:`ArchiveError` if unsafe."""
    if not data:
        raise ArchiveError("INVALID_ARCHIVE", "The upload is empty. Send a ZIP archive as the request body.")
    if len(data) > limits.max_zip_bytes:
        raise ArchiveError(
            "ARCHIVE_TOO_LARGE",
            f"The ZIP archive is {_mib(len(data))}; the maximum is {_mib(limits.max_zip_bytes)}.",
            status=413,
        )
    if not (data.startswith(_LOCAL_HEADER) or data.startswith(_EMPTY_ARCHIVE)):
        raise ArchiveError("INVALID_ARCHIVE", "The upload is not a ZIP archive (missing ZIP signature).")

    declared = _declared_entry_count(data)
    if declared is not None and (
        declared > limits.max_entries or (declared == 0xFFFF and limits.max_entries < 0xFFFF)
    ):
        raise ArchiveError(
            "TOO_MANY_ENTRIES",
            f"The archive has more than {limits.max_entries} entries. "
            "Leave out dependency and build folders (node_modules, .venv, dist, ...) and try again.",
        )

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, EOFError, OSError, ValueError, struct.error, OverflowError, NotImplementedError):
        raise ArchiveError("INVALID_ARCHIVE", "The upload is not a valid ZIP archive.") from None

    try:
        return _validate(archive, data, limits)
    except BaseException:
        archive.close()
        raise


def _validate(archive: zipfile.ZipFile, data: bytes, limits: ProjectSettings) -> ProjectArchive:
    infos = archive.infolist()
    if not infos:
        raise ArchiveError("EMPTY_ARCHIVE", "The archive contains no files.")
    if len(infos) > limits.max_entries:
        raise ArchiveError(
            "TOO_MANY_ENTRIES",
            f"The archive has {len(infos)} entries; the maximum is {limits.max_entries}.",
        )

    members: list[ArchiveMember] = []
    seen: dict[str, str] = {}  # collision key -> first normalised path (files and directories)
    file_keys: set[str] = set()
    dir_keys: set[str] = set()
    total_declared = 0
    total_compressed = 0

    for info in infos:
        raw_name = info.orig_filename  # before zipfile rewrites os.sep on Windows
        path, is_dir = normalize_member_path(raw_name, limits)
        shown = _show(raw_name)

        if info.flag_bits & 0x1:
            raise ArchiveError("ENCRYPTED_ARCHIVE", f"Entry {shown} is encrypted; encrypted archives are not supported.")
        if info.compress_type not in _ALLOWED_METHODS:
            raise ArchiveError(
                "UNSUPPORTED_ENTRY", f"Entry {shown} uses an unsupported compression method."
            )

        mode = info.external_attr >> 16
        if mode:
            kind = stat.S_IFMT(mode)
            if kind == stat.S_IFLNK:
                raise ArchiveError("UNSUPPORTED_ENTRY", f"Entry {shown} is a symbolic link.")
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ArchiveError("UNSUPPORTED_ENTRY", f"Entry {shown} is a special file (device, pipe or socket).")
            if kind == stat.S_IFDIR and not is_dir:
                raise ArchiveError("UNSUPPORTED_ENTRY", f"Entry {shown} is marked as a directory but named like a file.")
            if kind == stat.S_IFREG and is_dir:
                raise ArchiveError("UNSUPPORTED_ENTRY", f"Entry {shown} is named like a directory but marked as a file.")
        if info.external_attr & 0x400 and info.create_system == 0:  # DOS reparse-point attribute
            raise ArchiveError("UNSUPPORTED_ENTRY", f"Entry {shown} is a reparse point or link.")

        if info.header_offset >= len(data):
            raise ArchiveError("INVALID_ARCHIVE", f"Entry {shown} points outside the archive.")
        if is_dir and info.file_size:
            raise ArchiveError("UNSUPPORTED_ENTRY", f"Directory entry {shown} has content.")
        if info.file_size > limits.max_entry_bytes:
            raise ArchiveError(
                "ENTRY_TOO_LARGE",
                f"Entry {shown} is {_mib(info.file_size)}; the per-file maximum is "
                f"{_mib(limits.max_entry_bytes)}.",
            )

        key = _collision_key(path)
        if key in seen:
            exact = seen[key] == path
            raise ArchiveError(
                "DUPLICATE_PATH",
                f"Entry {shown} duplicates another entry"
                + ("." if exact else " when case and Unicode form are ignored."),
            )
        seen[key] = path
        # Every parent directory is implied; remember them to detect file/directory clashes.
        pieces = key.split("/")
        for depth in range(1, len(pieces)):
            dir_keys.add("/".join(pieces[:depth]))
        if is_dir:
            dir_keys.add(key)
            continue
        file_keys.add(key)

        total_declared += info.file_size
        total_compressed += info.compress_size
        if total_declared > limits.max_uncompressed_bytes:
            raise ArchiveError(
                "UNCOMPRESSED_TOO_LARGE",
                f"The project expands to more than {_mib(limits.max_uncompressed_bytes)} uncompressed.",
            )
        if (
            info.file_size >= limits.ratio_floor_bytes
            and info.file_size > max(info.compress_size, 1) * limits.max_compression_ratio
        ):
            raise ArchiveError(
                "SUSPICIOUS_COMPRESSION",
                f"Entry {shown} has a compression ratio above {limits.max_compression_ratio}:1 (possible ZIP bomb).",
            )
        members.append(ArchiveMember(path, info.file_size, info.compress_size, info))

    clash = file_keys & dir_keys
    if clash:
        raise ArchiveError(
            "DUPLICATE_PATH",
            f"The archive uses {_show(sorted(clash)[0])} as both a file and a directory.",
        )
    if total_compressed > len(data):
        raise ArchiveError(
            "SUSPICIOUS_COMPRESSION",
            "Entries claim more compressed data than the archive contains (overlapping entries).",
        )
    if (
        total_declared >= limits.ratio_floor_bytes
        and total_declared > len(data) * limits.max_compression_ratio
    ):
        raise ArchiveError(
            "SUSPICIOUS_COMPRESSION",
            f"The archive's overall compression ratio exceeds {limits.max_compression_ratio}:1 (possible ZIP bomb).",
        )

    members.sort(key=lambda m: m.path)
    return ProjectArchive(
        members=tuple(members),
        archive_bytes=len(data),
        entry_count=len(infos),
        uncompressed_bytes=total_declared,
        _zip=archive,
    )
