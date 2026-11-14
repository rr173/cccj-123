"""Exception hierarchy for the archive indexer."""

from __future__ import annotations


class ArchiveIndexError(Exception):
    """Base class for every error raised by this package."""


class UnsafePathError(ArchiveIndexError):
    """A member name is absolute, escapes the archive root, or is malformed."""


class LimitExceededError(ArchiveIndexError):
    """A configured safety limit (member count, depth, size, ...) was hit."""


class CorruptArchiveError(ArchiveIndexError):
    """The archive structure or a member digest is invalid/truncated."""


class UnsupportedArchiveError(ArchiveIndexError):
    """The archive format or member type cannot be processed."""


class DuplicateMemberError(ArchiveIndexError):
    """Raised when an operation requires a unique member but several match."""

    def __init__(self, path: str, occurrences: int):
        super().__init__(
            f"path {path!r} has {occurrences} occurrences; "
            "disambiguate with occurrence="
        )
        self.path = path
        self.occurrences = occurrences


class AmbiguousMemberError(ArchiveIndexError):
    """A duplicate/ambiguous member was used where uniqueness is required."""
