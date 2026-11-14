"""Field-contract parsing and strict cell conversion.

A contract is deliberately independent of any one CSV source.  It defines the
destination table, supported scalar types, nullability/defaults, allowed
values or numeric ranges and the business primary key.  Contract validation
happens before archive data is read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .errors import ContractValidationError

INT_RE = re.compile(r"[+-]?[0-9]+$")
DECIMAL_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$")
INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1
VALID_TYPES = {"text", "integer", "decimal", "timestamp"}


@dataclass(frozen=True, slots=True)
class FieldContract:
    name: str
    type: str
    nullable: bool = True
    default: str | None = None
    allowed: tuple[str, ...] | None = None
    minimum: str | None = None
    maximum: str | None = None
    precision: int | None = None
    scale: int | None = None


@dataclass(frozen=True, slots=True)
class Contract:
    table: str
    fields: tuple[FieldContract, ...]
    primary_key: tuple[str, ...]
    by_name: dict[str, FieldContract] = field(compare=False, repr=False)

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractValidationError(f"{label} must be an object")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractValidationError(f"{label} must be a non-empty string")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ContractValidationError(
            f"{label} {value!r} is not a safe SQL identifier")
    return value


def _as_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractValidationError(f"{label} must be true or false")
    return value


def parse_contract(data: Any) -> Contract:
    """Validate a decoded JSON contract and return an immutable object."""
    root = _require_object(data, "contract")
    table = _identifier(root.get("table"), "contract.table")
    raw_fields = root.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ContractValidationError("contract.fields must be a non-empty list")

    fields: list[FieldContract] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_fields):
        item = _require_object(raw, f"contract.fields[{index}]")
        name = _identifier(item.get("name"), f"fields[{index}].name")
        if name in seen:
            raise ContractValidationError(f"duplicate target column {name!r}")
        seen.add(name)
        typ = item.get("type")
        if typ not in VALID_TYPES:
            raise ContractValidationError(
                f"field {name!r} has unknown type {typ!r}; supported types: "
                + ", ".join(sorted(VALID_TYPES)))
        nullable = _as_bool(item.get("nullable", True),
                            f"field {name!r}.nullable")
        default = item.get("default")
        if default is not None:
            if not isinstance(default, str):
                raise ContractValidationError(
                    f"field {name!r}.default must be a string or null")
            if default == "":
                raise ContractValidationError(
                    f"field {name!r}.default cannot be the empty-string "
                    "missing marker")
        allowed = item.get("allowed")
        if allowed is not None:
            if not isinstance(allowed, list) or not allowed or not all(
                    isinstance(x, str) for x in allowed):
                raise ContractValidationError(
                    f"field {name!r}.allowed must be a non-empty string list")
            if len(set(allowed)) != len(allowed):
                raise ContractValidationError(
                    f"field {name!r}.allowed contains duplicate values")
            allowed = tuple(allowed)
        minimum = item.get("min")
        maximum = item.get("max")
        if minimum is not None and not isinstance(minimum, str):
            raise ContractValidationError(f"field {name!r}.min must be a string")
        if maximum is not None and not isinstance(maximum, str):
            raise ContractValidationError(f"field {name!r}.max must be a string")
        precision = item.get("precision")
        scale = item.get("scale")
        if typ == "decimal":
            if not isinstance(precision, int) or isinstance(precision, bool) \
                    or precision < 1:
                raise ContractValidationError(
                    f"decimal field {name!r} requires positive integer precision")
            if not isinstance(scale, int) or isinstance(scale, bool) \
                    or scale < 0 or scale > precision:
                raise ContractValidationError(
                    f"decimal field {name!r} requires 0 <= scale <= precision")
        elif precision is not None or scale is not None:
            raise ContractValidationError(
                f"precision/scale are only valid for decimal field {name!r}")
        if typ == "text" and (minimum is not None or maximum is not None):
            raise ContractValidationError(
                f"text field {name!r} uses allowed, not min/max")
        if typ in {"integer", "decimal", "timestamp"} and allowed is not None:
            raise ContractValidationError(
                f"allowed value sets are only valid for text field {name!r}")
        if typ in {"text", "timestamp"} and (
                minimum is not None or maximum is not None):
            raise ContractValidationError(
                f"min/max are only valid for integer/decimal field {name!r}")
        fields.append(FieldContract(
            name=name, type=typ, nullable=nullable, default=default,
            allowed=allowed, minimum=minimum, maximum=maximum,
            precision=precision, scale=scale))

    raw_pk = root.get("primary_key")
    if not isinstance(raw_pk, list) or not raw_pk:
        raise ContractValidationError(
            "contract.primary_key must be a non-empty list of columns")
    if not all(isinstance(x, str) for x in raw_pk):
        raise ContractValidationError("primary key columns must be strings")
    if len(set(raw_pk)) != len(raw_pk):
        raise ContractValidationError("primary key contains a duplicate column")
    by_name = {f.name: f for f in fields}
    missing = [x for x in raw_pk if x not in by_name]
    if missing:
        raise ContractValidationError(
            f"primary key references unknown column(s): {missing}")
    for pk_name in raw_pk:
        if by_name[pk_name].nullable:
            raise ContractValidationError(
                f"primary key column {pk_name!r} must declare nullable=false")

    contract = Contract(
        table=table,
        fields=tuple(fields),
        primary_key=tuple(raw_pk),
        by_name=by_name,
    )
    for fld in fields:
        try:
            if fld.default is not None:
                convert_cell(fld, fld.default, is_default=True)
            if fld.minimum is not None:
                convert_cell(fld, fld.minimum.strip())
            if fld.maximum is not None:
                convert_cell(fld, fld.maximum.strip())
            _check_range_endpoints(fld)
        except (ValueError, ArithmeticError) as exc:
            raise ContractValidationError(
                f"invalid contract configuration for field {fld.name!r}: {exc}"
            ) from exc
    return contract


def load_contract(path: str | Path) -> Contract:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractValidationError(f"cannot load contract {path!s}: {exc}") \
            from exc
    return parse_contract(data)


def _parse_integer(text: str) -> int:
    if not INT_RE.fullmatch(text):
        raise ValueError("integer must contain only an optional sign and digits")
    value = int(text)
    if value < INT64_MIN or value > INT64_MAX:
        raise ValueError("integer is outside signed SQLite 64-bit range")
    return value


def _parse_decimal(fld: FieldContract, text: str) -> Decimal:
    if not DECIMAL_RE.fullmatch(text):
        raise ValueError(
            "decimal must use optional sign and decimal digits; scientific "
            "notation and float literals are not accepted")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    # Decimal.as_tuple handles sign, digits and exponent without floating point.
    sign, digits, exponent = value.as_tuple()
    assert exponent <= 0
    used_scale = -exponent
    if used_scale > fld.scale:
        raise ValueError(
            f"decimal has {used_scale} fractional digit(s); contract allows "
            f"{fld.scale}")
    integer_digits = len(digits) - used_scale
    if integer_digits + fld.scale > fld.precision:
        raise ValueError(
            f"decimal precision {integer_digits + used_scale} exceeds contract "
            f"precision {fld.precision}")
    return value


def _parse_timestamp(text: str) -> datetime:
    # Python's ISO parser never applies the local timezone to a naive value.
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp is not a valid ISO-8601 value") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            "timestamp must include a timezone; naive timestamps cannot be "
            "inferred using the local timezone")
    return parsed.astimezone(timezone.utc)


def convert_cell(fld: FieldContract, cell: str, *,
                 is_default: bool = False) -> Any:
    """Convert one cell according to its field contract.

    The empty string is the sole missing marker.  Defaults bypass nullability
    only through the caller's explicit missing-value path; this function still
    validates that a default itself is legal.
    """
    if fld.type == "text":
        if fld.allowed is not None and cell not in fld.allowed:
            raise ValueError("value is not in allowed set")
        return cell

    text = cell.strip()
    if fld.type == "integer":
        value = _parse_integer(text)
    elif fld.type == "decimal":
        value = _parse_decimal(fld, text)
    elif fld.type == "timestamp":
        value = _parse_timestamp(text)
    else:  # pragma: no cover - guarded by contract validation
        raise ValueError(f"unsupported type {fld.type}")

    if fld.allowed is not None:  # pragma: no cover - guarded by validation
        raise ValueError("allowed sets are text-only")
    if fld.minimum is not None:
        minimum = _parse_numeric_bound(fld, fld.minimum.strip())
        if value < minimum:
            raise ValueError(f"value is below minimum {fld.minimum}")
    if fld.maximum is not None:
        maximum = _parse_numeric_bound(fld, fld.maximum.strip())
        if value > maximum:
            raise ValueError(f"value is above maximum {fld.maximum}")
    return value


def _parse_numeric_bound(fld: FieldContract, text: str) -> Any:
    if fld.type == "integer":
        return _parse_integer(text)
    if fld.type == "decimal":
        return _parse_decimal(fld, text)
    raise ValueError(f"range is not supported for {fld.type}")


def _check_range_endpoints(fld: FieldContract) -> None:
    if fld.minimum is None or fld.maximum is None:
        return
    low = convert_cell(fld, fld.minimum.strip())
    high = convert_cell(fld, fld.maximum.strip())
    if low > high:
        raise ContractValidationError(
            f"field {fld.name!r} min is greater than max")


def storage_value(fld: FieldContract, value: Any) -> Any:
    """Return the exact value to bind to SQLite."""
    if fld.type == "decimal":
        # Text storage preserves the accepted lexical representation.  Numeric
        # Decimal comparisons are still used for primary keys/range checks.
        return str(value)
    if fld.type == "timestamp":
        return value.strftime("%Y-%m-%d %H:%M:%S.%f+00:00")
    return value


def key_value(fld: FieldContract, value: Any) -> Any:
    """Normalize a converted value for business-key equality."""
    if fld.type == "decimal":
        # 1.10 and 1.100 are the same numeric business key.
        return value.normalize()
    if fld.type == "timestamp":
        return storage_value(fld, value)
    return value
