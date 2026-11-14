"""Data records and configurable safety limits."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from .errors import DuplicateMemberError


@dataclass(frozen=True, slots=True)
class Limits:
    """Safety limits applied while indexing.

    Any limit breach raises :class:`~archindex.errors.LimitExceededError`.
    """

    max_members: int = 100_000
    max_depth: int = 100
    #: Maximum uncompressed size of a single member.
    max_member_size: int = 4 * 1024 ** 3
    #: Maximum sum of uncompressed member sizes.
    max_total_size: int = 16 * 1024 ** 3
    #: Maximum uncompressed/stored size ratio across the whole archive.
    max_compression_ratio: float = 1000.0
    #: Maximum stored (compressed) size of a single member.
    max_member_compressed_size: int = 1024 ** 3
    #: Reject symlink/hardlink members instead of recording them.
    reject_links: bool = False
    #: Reject device/FIFO members (they cannot be represented as plain files).
    reject_special_files: bool = True


@dataclass(frozen=True, slots=True)
class MemberRecord:
    """One member entry, in order of occurrence inside the archive."""

    #: Canonical, root-relative POSIX path.
    name: str
    #: 0-based occurrence number for members sharing :attr:`name`.
    occurrence: int
    kind: str  # "file" | "dir" | "symlink" | "hardlink" | "special"
    is_dir: bool
    depth: int
    #: Uncompressed length in bytes (0 for links/special entries).
    size: int
    #: Stored length when the format exposes it, else None.
    compressed_size: int | None
    #: Header offset from the start of the package, when available.
    offset: int | None
    #: Data offset from the start of the package, when available.
    data_offset: int | None
    #: CRC-32 (IEEE) of uncompressed content, when computable.
    crc32: int | None
    sha256: str | None
    #: Raw link target for symlink/hardlink members.
    link_target: str | None
    #: Normalised in-archive target the link resolves to, if safe.
    resolved_target: str | None
    #: True when another member uses the same canonical name.
    ambiguous: bool
    #: How an undecodable metadata name was recovered, if applicable.
    encoding_note: str | None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.crc32 is not None:
            data["crc32"] = f"{self.crc32:08x}"
        return data


@dataclass(slots=True)
class IndexResult:
    """Full index of one archive."""

    path: str | None
    format: str  # "zip" | "tar"
    members: list[MemberRecord] = field(default_factory=list)
    by_name: dict[str, list[MemberRecord]] = field(default_factory=dict)
    ambiguous_names: set[str] = field(default_factory=set)
    #: Compression wrapper for TAR ("gz", "bz2", "xz", None), etc.
    metadata: dict[str, Any] = field(default_factory=dict)
    #: The effective safety limits used while building this index.
    limits: "Limits | None" = None

    def __len__(self) -> int:
        return len(self.members)

    def __iter__(self):
        return iter(self.members)

    @property
    def total_uncompressed_size(self) -> int:
        return sum(m.size for m in self.members)

    def get(self, name: str, occurrence: int | None = None) -> MemberRecord:
        """Return a member by canonical name.

        With several occurrences and no explicit ``occurrence`` a
        :class:`DuplicateMemberError` is raised.
        """
        records = self.by_name.get(name)
        if not records:
            raise KeyError(name)
        if occurrence is None:
            if len(records) > 1:
                raise DuplicateMemberError(name, len(records))
            return records[0]
        if occurrence < 0 or occurrence >= len(records):
            raise IndexError(
                f"occurrence {occurrence} out of range for {name!r} "
                f"(have {len(records)})"
            )
        return records[occurrence]

    def occurrences(self, name: str) -> list[MemberRecord]:
        return list(self.by_name.get(name, ()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "format": self.format,
            "member_count": len(self.members),
            "ambiguous_names": sorted(self.ambiguous_names),
            "metadata": self.metadata,
            "members": [m.to_dict() for m in self.members],
        }
