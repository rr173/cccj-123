"""Ingestion manifests and validated source descriptions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import Contract
from .errors import ManifestValidationError
from .pathsan import normalize_member_name


@dataclass(frozen=True, slots=True)
class SourceSpec:
    archive: str
    member: str
    occurrence: int | None
    encoding: str
    delimiter: str
    mapping: dict[str, str]
    ignore_extra_columns: bool = False


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestValidationError(f"{label} must be an object")
    return value


def parse_manifest(data: Any, contract: Contract) -> list[SourceSpec]:
    """Validate manifest syntax and cross-reference contract target columns."""
    if not isinstance(data, list) or not data:
        raise ManifestValidationError(
            "ingestion manifest must be a non-empty list of sources")

    targets = set(contract.field_names)
    specs: list[SourceSpec] = []
    for index, raw in enumerate(data):
        label = f"manifest[{index}]"
        item = _require_object(raw, label)
        archive = item.get("archive")
        if not isinstance(archive, str) or not archive:
            raise ManifestValidationError(f"{label}.archive is required")
        member = item.get("member")
        if not isinstance(member, str) or not member:
            raise ManifestValidationError(f"{label}.member is required")
        canonical_member = normalize_member_name(member)
        occurrence = item.get("occurrence")
        if occurrence is not None and (
                not isinstance(occurrence, int) or isinstance(occurrence, bool)
                or occurrence < 0):
            raise ManifestValidationError(
                f"{label}.occurrence must be a non-negative integer or null")
        encoding = item.get("encoding", "utf-8")
        if not isinstance(encoding, str):
            raise ManifestValidationError(f"{label}.encoding must be a string")
        normalized_encoding = encoding.lower().replace("_", "-")
        if normalized_encoding not in {"utf-8", "gb18030"}:
            raise ManifestValidationError(
                f"{label}.encoding must be utf-8 or gb18030")
        delimiter = item.get("delimiter", ",")
        if delimiter not in {",", "\t"}:
            raise ManifestValidationError(
                f"{label}.delimiter must be ',' or a tab")
        mapping = item.get("mapping")
        if not isinstance(mapping, dict) or not mapping or not all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in mapping.items()):
            raise ManifestValidationError(
                f"{label}.mapping must be a non-empty source-to-target object")
        target_values = list(mapping.values())
        if len(set(target_values)) != len(target_values):
            raise ManifestValidationError(
                f"{label}.mapping maps two source columns to one target")
        unknown = [v for v in target_values if v not in targets]
        if unknown:
            raise ManifestValidationError(
                f"{label}.mapping references unknown contract columns: {unknown}")
        ignore_extra = item.get("ignore_extra_columns", False)
        if not isinstance(ignore_extra, bool):
            raise ManifestValidationError(
                f"{label}.ignore_extra_columns must be true or false")
        specs.append(SourceSpec(
            archive=archive,
            member=canonical_member,
            occurrence=occurrence,
            encoding=normalized_encoding,
            delimiter=delimiter,
            mapping=dict(mapping),
            ignore_extra_columns=ignore_extra,
        ))
    return specs


def load_manifest(path: str | Path, contract: Contract) -> list[SourceSpec]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestValidationError(
            f"cannot load manifest {path!s}: {exc}") from exc
    return parse_manifest(data, contract)
