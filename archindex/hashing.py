"""Bounded streaming digest helpers.

Member data is read in fixed-size chunks; the uncompressed size is enforced
while streaming so a declared size can never force a huge allocation.
"""

from __future__ import annotations

import hashlib
import zlib
from typing import BinaryIO

CHUNK_SIZE = 1024 * 1024  # 1 MiB


def stream_digest(
    fileobj,
    max_size: int,
    expected_size: int | None = None,
) -> tuple[int, int, str]:
    """Read ``fileobj`` to EOF, returning ``(size, crc32, sha256_hex)``.

    Raises ``ValueError`` when more than ``max_size`` bytes are produced, and
    when ``expected_size`` is given but the actual length differs.
    """
    crc = 0
    sha = hashlib.sha256()
    total = 0
    while True:
        chunk = fileobj.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_size:
            raise ValueError(
                f"member exceeds maximum uncompressed size {max_size} bytes"
            )
        crc = zlib.crc32(chunk, crc)
        sha.update(chunk)
    if expected_size is not None and total != expected_size:
        raise ValueError(
            f"declared member size {expected_size} != actual {total}"
        )
    return total, crc & 0xFFFFFFFF, sha.hexdigest()


def stream_digest_and_copy(
    fileobj: BinaryIO,
    out: BinaryIO,
    max_size: int,
    expected_size: int,
) -> tuple[int, int, str]:
    """Stream ``fileobj`` into ``out`` while hashing, with a size ceiling.

    Every chunk is written to ``out`` immediately so the member is never
    buffered whole in memory.  A produced length exceeding ``max_size`` or
    diverging from ``expected_size`` raises :class:`ValueError`.
    """
    crc = 0
    sha = hashlib.sha256()
    total = 0
    while True:
        chunk = fileobj.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_size:
            raise ValueError(
                f"member exceeds maximum uncompressed size {max_size} bytes"
            )
        crc = zlib.crc32(chunk, crc)
        sha.update(chunk)
        out.write(chunk)
    if total != expected_size:
        raise ValueError(
            f"declared member size {expected_size} != actual {total}"
        )
    return total, crc & 0xFFFFFFFF, sha.hexdigest()
