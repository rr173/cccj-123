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


class IngestionError(ArchiveIndexError):
    """Base class for one-off tabular ingestion failures."""


class ContractValidationError(IngestionError):
    """A field contract is internally invalid."""


class ManifestValidationError(IngestionError):
    """An ingestion manifest cannot be satisfied by the supplied archives."""


class SourceStructureError(IngestionError):
    """A CSV source has an invalid header/mapping structure.

    These failures prevent reliable interpretation of subsequent rows and
    terminate the whole ingestion before any business rows are accepted.
    """


class SourceStreamError(IngestionError):
    """A source became undecodable, unparseable or unreadable mid-stream."""

    def __init__(self, message: str, *, source_id: int | None = None,
                 member: str | None = None, occurrence: int | None = None,
                 records_read: int = 0, physical_lines: int = 0,
                 bytes_processed: int = 0):
        super().__init__(message)
        self.source_id = source_id
        self.member = member
        self.occurrence = occurrence
        self.records_read = records_read
        self.physical_lines = physical_lines
        self.bytes_processed = bytes_processed


class AssemblyError(ArchiveIndexError):
    """Base class for nested-delivery assembly failures."""


class AssemblyPlanError(AssemblyError):
    """An assembly plan is internally invalid or references unknown objects."""


class AssemblyDataError(AssemblyError):
    """A source row violates identity, reference or supported-value rules.

    The message always names the plan node and the offending row identity so
    the problem can be located without re-running the export.
    """


class RelGenError(ArchiveIndexError):
    """Base class for relational test-data generation failures."""


class RecipeError(RelGenError):
    """A data-generation recipe is internally invalid.

    Raised before any row is produced: structural problems, unresolvable
    names, illegal generators, key-range overflows, required references to
    empty tables and derived-column dependency cycles.
    """


class GenerationError(RelGenError):
    """A single addressed value or row could not be produced.

    The message always locates the table, row number, column and the
    dependency path that led to the failure.
    """

    def __init__(self, message: str, *, table: str | None = None,
                 row: int | None = None, column: str | None = None,
                 path: tuple[str, ...] = ()):
        self.table = table
        self.row = row
        self.column = column
        self.path = tuple(path)
        super().__init__(message)
