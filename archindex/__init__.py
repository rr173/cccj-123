"""Read-only archive indexer for TAR and ZIP member directories.

The indexer never extracts member contents to disk while indexing: every
member is streamed through a hasher in bounded chunks.  It records the
normalised path, directory depth, stored/expanded lengths, in-package offset
(when available) and both a CRC-32 and a SHA-256 digest.  Repeated members
sharing the same logical path keep their occurrence order and are flagged as
ambiguous.

High level entry points:

* :func:`index_archive` -- build an :class:`IndexResult`
* :func:`find_by_prefix` -- prefix query
* :func:`diff_indices` -- compare two indices
* :func:`export_member` -- verify digests then safely export one member
"""

from .errors import (
    AmbiguousMemberError,
    ArchiveIndexError,
    CorruptArchiveError,
    DuplicateMemberError,
    LimitExceededError,
    UnsupportedArchiveError,
    UnsafePathError,
)
from .indexer import (
    IndexResult,
    Limits,
    MemberRecord,
    diff_indices,
    export_member,
    find_by_prefix,
    index_archive,
    index_tar,
    index_zip,
)

__all__ = [
    "AmbiguousMemberError",
    "ArchiveIndexError",
    "CorruptArchiveError",
    "DuplicateMemberError",
    "IndexResult",
    "LimitExceededError",
    "Limits",
    "MemberRecord",
    "UnsupportedArchiveError",
    "UnsafePathError",
    "diff_indices",
    "export_member",
    "find_by_prefix",
    "index_archive",
    "index_tar",
    "index_zip",
]

__version__ = "1.0.0"
