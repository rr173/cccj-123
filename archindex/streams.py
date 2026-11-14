"""Verified streaming readers for archive members."""

from __future__ import annotations

import hashlib
import tarfile
import zipfile
import zlib
from types import TracebackType
from typing import Type

from .errors import CorruptArchiveError
from .records import IndexResult, MemberRecord
from .pathsan import normalize_member_name


class VerifiedBinaryStream:
    """Wrap an archive member stream and verify its complete consumption.

    The wrapper hashes only bytes actually delivered to the CSV layer and
    tracks their count.  On close it requires EOF and equality with the
    existing index digest, making truncation/tampering after indexing fatal.
    No extracted CSV is written to disk and no read returns the complete
    member at once.
    """

    __slots__ = (
        "_stream", "_record", "_sha", "_crc", "_read", "_closed",
        "_archive_file", "_verify_on_close",
    )

    def __init__(self, stream, record: MemberRecord, archive_file=None,
                 verify_on_close: bool = True):
        self._stream = stream
        self._record = record
        self._archive_file = archive_file
        self._verify_on_close = verify_on_close
        self._sha = hashlib.sha256()
        self._crc = 0
        self._read = 0
        self._closed = False

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            raise ValueError("read from closed archive member stream")
        try:
            chunk = self._stream.read(size)
        except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as exc:
            raise CorruptArchiveError(
                f"member {self._record.name!r} could not be read: {exc}") \
                from exc
        if chunk:
            if self._read + len(chunk) > self._record.size:
                raise CorruptArchiveError(
                    f"member {self._record.name!r} is longer than indexed size")
            self._read += len(chunk)
            self._sha.update(chunk)
            self._crc = zlib.crc32(chunk, self._crc) & 0xFFFFFFFF
        return chunk

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._stream, "close", None)
            if close is not None:
                close()
        except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as exc:
            raise CorruptArchiveError(
                f"member {self._record.name!r} failed archive check: {exc}") \
                from exc
        finally:
            try:
                if self._verify_on_close:
                    # Detect short/truncated members even if the decompressor did not.
                    if self._read != self._record.size:
                        raise CorruptArchiveError(
                            f"member {self._record.name!r} ended after "
                            f"{self._read} byte(s); index requires "
                            f"{self._record.size}")
            finally:
                if self._archive_file is not None:
                    self._archive_file.close()
        if self._verify_on_close:
            actual = self._sha.hexdigest()
            if self._record.sha256 is not None and actual != self._record.sha256:
                raise CorruptArchiveError(
                    f"member {self._record.name!r} SHA-256 changed since indexing")
            if self._record.crc32 is not None and self._crc != self._record.crc32:
                raise CorruptArchiveError(
                    f"member {self._record.name!r} CRC-32 changed since indexing")

    def __enter__(self) -> "VerifiedBinaryStream":
        return self

    def __exit__(self, exc_type: Type[BaseException] | None,
                 exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()


def resolve_member(index: IndexResult, member: str,
                   occurrence: int | None) -> MemberRecord:
    canonical = normalize_member_name(member)
    records = index.by_name.get(canonical)
    if not records:
        raise CorruptArchiveError(
            f"member {canonical!r} is not present in {index.path!r}")
    if occurrence is None:
        if len(records) > 1:
            raise CorruptArchiveError(
                f"member {canonical!r} has {len(records)} occurrences; "
                "manifest must select an explicit occurrence")
        return records[0]
    if occurrence < 0 or occurrence >= len(records):
        raise CorruptArchiveError(
            f"occurrence {occurrence} does not exist for {canonical!r}")
    return records[occurrence]


def open_verified_member(index: IndexResult, record: MemberRecord,
                         *, verify_on_close: bool = True
                         ) -> VerifiedBinaryStream:
    """Open one regular file member without extracting it."""
    if record.is_dir or record.kind != "file":
        raise CorruptArchiveError(
            f"member {record.name!r} is a {record.kind}, not a CSV data source")
    if index.format == "zip":
        try:
            zf = zipfile.ZipFile(index.path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise CorruptArchiveError(
                f"cannot reopen zip archive {index.path!r}: {exc}") from exc
        try:
            info = None
            seen = 0
            for candidate in zf.infolist():
                if normalize_member_name(candidate.filename) == record.name:
                    if seen == record.occurrence:
                        info = candidate
                        break
                    seen += 1
            if info is None:
                raise CorruptArchiveError(
                    f"member {record.name!r} vanished on archive reopen")
            raw = zf.open(info)
            return VerifiedBinaryStream(
                raw, record, archive_file=zf,
                verify_on_close=verify_on_close)
        except Exception:
            zf.close()
            raise

    try:
        tf = tarfile.open(index.path, mode="r:*")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise CorruptArchiveError(
            f"cannot reopen tar archive {index.path!r}: {exc}") from exc
    try:
        tinfo = None
        seen = 0
        for candidate in tf.getmembers():
            if normalize_member_name(candidate.name) == record.name:
                if seen == record.occurrence:
                    tinfo = candidate
                    break
                seen += 1
        if tinfo is None:
            raise CorruptArchiveError(
                f"member {record.name!r} vanished on archive reopen")
        raw = tf.extractfile(tinfo)
        if raw is None:
            raise CorruptArchiveError(
                f"member {record.name!r} has no readable data")
        return VerifiedBinaryStream(
            raw, record, archive_file=tf,
            verify_on_close=verify_on_close)
    except Exception:
        tf.close()
        raise
