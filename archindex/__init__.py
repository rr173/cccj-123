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
    CellDataError,
    ContractValidationError,
    CorruptArchiveError,
    DescriptorError,
    DuplicateMemberError,
    GenerationError,
    IngestionError,
    LabelReadError,
    LimitExceededError,
    ManifestValidationError,
    ReadRequestError,
    RecipeError,
    RelGenError,
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
from .relgen import (
    GenDbResult,
    Recipe,
    RowFactory,
    compile_recipe,
    generate_database,
    generate_row,
    generate_rows,
    load_recipe,
)
from .labelread import (
    Axis,
    LabelStore,
    VariableInfo,
    VariableSlot,
    load_descriptor,
    open_descriptor,
    summarize,
)

__all__ = [
    "AmbiguousMemberError",
    "ArchiveIndexError",
    "AssemblyDataError",
    "AssemblyError",
    "AssemblyPlan",
    "AssemblyPlanError",
    "AssemblyResult",
    "Axis",
    "CellDataError",
    "Contract",
    "ContractValidationError",
    "CorruptArchiveError",
    "DescriptorError",
    "DuplicateMemberError",
    "FieldContract",
    "GenDbResult",
    "GenerationError",
    "IngestionError",
    "IngestionResult",
    "IndexResult",
    "LabelReadError",
    "LabelStore",
    "LimitExceededError",
    "Limits",
    "ManifestValidationError",
    "MemberRecord",
    "NodeSpec",
    "ReadRequestError",
    "Recipe",
    "RecipeError",
    "RelGenError",
    "RowFactory",
    "ScalarSpec",
    "SourceSpec",
    "SourceStreamError",
    "SourceStructureError",
    "UnsupportedArchiveError",
    "UnsafePathError",
    "VariableInfo",
    "VariableSlot",
    "assemble_nested",
    "compile_recipe",
    "diff_indices",
    "export_member",
    "find_by_prefix",
    "generate_database",
    "generate_row",
    "generate_rows",
    "index_archive",
    "index_tar",
    "index_zip",
    "ingest_sources",
    "load_assembly_plan",
    "load_contract",
    "load_descriptor",
    "load_manifest",
    "load_recipe",
    "open_descriptor",
    "parse_assembly_plan",
    "parse_contract",
    "parse_manifest",
    "summarize",
]

__version__ = "1.0.0"
