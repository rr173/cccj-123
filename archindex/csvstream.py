"""Bounded, physical-line aware CSV decoding.

The standard-library :mod:`csv` module implements RFC-style quoting, escaped
quotes, quoted delimiters and records spanning physical lines.  It does not
expose physical-line spans, so this package pairs it with a small streaming
line iterator whose line counter survives multi-line quoted records.

Only comma and tab delimiters are supported by the ingestion contract.
"""

from __future__ import annotations

import codecs
import csv
from dataclasses import dataclass
from typing import BinaryIO, Iterator

from .errors import SourceStreamError


@dataclass(frozen=True, slots=True)
class CsvRecord:
    """One logical CSV record and its inclusive physical-line span."""

    fields: list[str]
    start_line: int
    end_line: int


class DecodingTextReader:
    """Decode bounded binary chunks without buffering a whole member."""

    def __init__(self, binary: BinaryIO, encoding: str, *,
                 chunk_size: int = 1024 * 1024,
                 physical_line_getter=None):
        normalized = encoding.lower().replace("_", "-")
        if normalized not in {"utf-8", "gb18030"}:
            raise ValueError("encoding must be utf-8 or gb18030")
        self._binary = binary
        # ``strict`` ensures bad bytes are reported instead of replaced.
        self._decoder = codecs.getincrementaldecoder(encoding)(
            errors="strict")
        self._chunk_size = chunk_size
        self._physical_line_getter = physical_line_getter or (lambda: 0)
        self._buffer = ""
        self._eof = False
        self._bom_pending = normalized == "utf-8"
        self.bytes_processed = 0

    def _decode(self, raw: bytes, *, final: bool = False) -> str:
        self.bytes_processed += len(raw)
        try:
            return self._decoder.decode(raw, final=final)
        except UnicodeDecodeError as exc:
            raise SourceStreamError(
                f"decoding error: {exc}; processing stopped after "
                f"{self.bytes_processed} attempted byte(s) and physical line "
                f"{self._physical_line_getter()}") from exc

    def _fill(self) -> bool:
        while not self._buffer and not self._eof:
            raw = self._binary.read(self._chunk_size)
            if not raw:
                tail = self._decode(b"", final=True)
                if tail:
                    self._buffer += tail
                self._eof = True
                break
            self._buffer += self._decode(raw)
        if self._bom_pending and self._buffer:
            if self._buffer.startswith("﻿"):
                self._buffer = self._buffer[1:]
            self._bom_pending = False
        return bool(self._buffer)

    def read_line(self) -> str | None:
        """Return one physical line, retaining its line-ending character."""
        while True:
            if not self._fill():
                return None
            lf = self._buffer.find("\n")
            cr = self._buffer.find("\r")
            if lf < 0 and cr < 0:
                if self._eof:
                    line, self._buffer = self._buffer, ""
                    return line
                raw = self._binary.read(self._chunk_size)
                if not raw:
                    tail = self._decode(b"", final=True)
                    self._buffer += tail
                    self._eof = True
                    line, self._buffer = self._buffer, ""
                    return line
                self._buffer += self._decode(raw)
                continue

            if cr >= 0 and (lf < 0 or cr < lf):
                end = cr + 1
                if self._buffer[cr:cr + 2] == "\r\n":
                    end = cr + 2
            else:
                end = lf + 1
            line = self._buffer[:end]
            self._buffer = self._buffer[end:]
            return line


class _PhysicalLineIterator:
    def __init__(self, reader: DecodingTextReader):
        self._reader = reader
        self.physical_line = 0
        self.last_line: str | None = None

    def __iter__(self) -> "_PhysicalLineIterator":
        return self

    def __next__(self) -> str:
        line = self._reader.read_line()
        if line is None:
            raise StopIteration
        self.physical_line += 1
        self.last_line = line
        return line


class PhysicalCsvParser:
    """Streaming parser returning logical records with physical line ranges."""

    def __init__(self, reader: DecodingTextReader, delimiter: str, *,
                 source_id: int | None = None, member: str | None = None,
                 occurrence: int | None = None):
        if delimiter not in {",", "\t"}:
            raise ValueError("CSV delimiter must be comma or tab")
        self._line_iterator = _PhysicalLineIterator(reader)
        reader._physical_line_getter = lambda: self._line_iterator.physical_line
        self._delimiter = delimiter
        self._source_id = source_id
        self._member = member
        self._occurrence = occurrence
        self._reader = csv.reader(
            self._line_iterator,
            delimiter=delimiter,
            quotechar='"',
            doublequote=True,
            skipinitialspace=False,
            strict=True,
            lineterminator="\n")

    def _stream_error(self, exc: BaseException) -> SourceStreamError:
        return SourceStreamError(
            f"source {self._source_id} ({self._member}#"
            f"{self._occurrence}): malformed CSV: {exc}; "
            f"processing stopped after physical line "
            f"{self._line_iterator.physical_line}",
            source_id=self._source_id,
            member=self._member,
            occurrence=self._occurrence,
            records_read=getattr(self, "_records_read", 0),
            physical_lines=self._line_iterator.physical_line)

    def __iter__(self) -> "PhysicalCsvParser":
        return self

    def __next__(self) -> CsvRecord:
        start = self._line_iterator.physical_line + 1
        try:
            fields = next(self._reader)
        except StopIteration:
            raise
        except SourceStreamError as exc:
            raise SourceStreamError(
                f"source {self._source_id} ({self._member}#"
                f"{self._occurrence}): {exc}",
                source_id=self._source_id,
                member=self._member,
                occurrence=self._occurrence,
                records_read=getattr(self, "_records_read", 0),
                physical_lines=self._line_iterator.physical_line,
                bytes_processed=self._line_iterator._reader.bytes_processed,
            ) from exc
        except csv.Error as exc:
            error = self._stream_error(exc)
            error.physical_lines = self._line_iterator.physical_line
            raise error from exc
        end = self._line_iterator.physical_line
        return CsvRecord(fields=fields, start_line=start, end_line=end)


def iter_csv_records(binary: BinaryIO, encoding: str, delimiter: str, *,
                     source_id: int | None = None, member: str | None = None,
                     occurrence: int | None = None) -> PhysicalCsvParser:
    """Yield CSV logical records from a binary archive stream."""
    reader = DecodingTextReader(binary, encoding)
    return PhysicalCsvParser(
        reader, delimiter, source_id=source_id,
        member=member, occurrence=occurrence)
