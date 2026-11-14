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
    AssemblyDataError,
    AssemblyError,
    AssemblyPlanError,
    ContractValidationError,
    CorruptArchiveError,
    DuplicateMemberError,
    IngestionError,
    LimitExceededError,
    ManifestValidationError,
    SourceStreamError,
    SourceStructureError,
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
from .ingestion import IngestionResult, ingest_sources
from .contracts import Contract, FieldContract, load_contract, parse_contract
from .manifest import SourceSpec, load_manifest, parse_manifest
from .assembly import (
    AssemblyPlan,
    AssemblyResult,
    NodeSpec,
    ScalarSpec,
    assemble_nested,
    load_assembly_plan,
    parse_assembly_plan,
)

__all__ = [
    "AmbiguousMemberError",
    "ArchiveIndexError",
    "AssemblyDataError",
    "AssemblyError",
    "AssemblyPlan",
    "AssemblyPlanError",
    "AssemblyResult",
    "Contract",
    "ContractValidationError",
    "CorruptArchiveError",
    "DuplicateMemberError",
    "FieldContract",
    "IngestionError",
    "IngestionResult",
    "IndexResult",
    "LimitExceededError",
    "Limits",
    "ManifestValidationError",
    "MemberRecord",
    "NodeSpec",
    "ScalarSpec",
    "SourceSpec",
    "SourceStreamError",
    "SourceStructureError",
    "UnsupportedArchiveError",
    "UnsafePathError",
    "assemble_nested",
    "diff_indices",
    "export_member",
    "find_by_prefix",
    "index_archive",
    "index_tar",
    "index_zip",
    "ingest_sources",
    "load_assembly_plan",
    "load_contract",
    "load_manifest",
    "parse_assembly_plan",
    "parse_contract",
    "parse_manifest",
]

__version__ = "1.0.0"
