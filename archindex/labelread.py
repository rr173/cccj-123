"""Label-coordinate joint reading over read-only NumPy matrix sources.

A *descriptor* (a plain JSON document) names the global axes, lists the
read-only sources and the numeric variables they carry.  Each source stores
one-dimensional coordinate vectors for the axes it actually uses; the order
of those vectors, the physical axis order of every ``.npy`` array and the
registration order of the sources are all free.  Reading aligns cells by
their real labels (integer value or text code points), never by equal array
subscripts, and never converts the matrices into a relational table or into
one big aligned copy.

Only storage and aligned access live here: no interpolation, resampling,
aggregation or physical calculation is performed.

Semantics summary
-----------------
* 1 to 4 global axes, each ``integer`` (signed 64-bit, never bool) or
  ``text`` (compared code point by code point).  Different axes may contain
  the same label text; axis identity is always carried explicitly.
* A source may not repeat a label on one of its axes.
* A variable uses any subset of the global axes; every source carrying that
  variable must declare exactly the same logical dimension set, though the
  physical order may differ.  Variables using fewer axes broadcast along the
  remaining grid axes by their own declaration -- a source missing a
  dimension is never silently broadcast.
* Per axis the visible labels are the union or intersection of the sources
  that actually use that axis; sources not using an axis contribute neither
  an empty set nor anything else.  Integers sort numerically, text by code
  points.  An empty intersection yields a normal empty grid.
* Each requested cell has exactly one of three states per variable:
  ``absent`` (no source locates all of the variable's coordinates),
  ``masked`` (located everywhere, all masks false) and ``value`` (at least
  one located, unmasked source).  Masked bit patterns are never read as
  business values; unmasked float64 NaN/infinity is an error, never a
  missing value.
* Overlapping valid values are resolved per read: ``strict`` requires
  bit-exact equality (signed zeroes compare equal, floats have no
  tolerance), ``priority`` takes the first valid source in a caller-given
  source order.  Masked evidence never participates in a comparison and
  conflicts are never averaged away.

Resource profile
----------------
Arrays are opened with read-only ``mmap`` (object arrays cannot be mapped
and are rejected at descriptor time).  Coordinate vectors and the
label-to-local-position dictionaries are held in memory: their size scales
with the number of *labels*, not with the product of array shapes.  Reading
one cell touches only the scalar positions behind it; projecting a variable
that was not requested never opens its data, and no global meshgrid,
transposed copy, full Cartesian materialisation or ``tolist`` is built.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ._vendor import ensure_numpy

np = ensure_numpy()
from numpy.lib import format as _npy_format  # noqa: E402

from .errors import (  # noqa: E402
    CellDataError,
    DescriptorError,
    ReadRequestError,
)

# ---------------------------------------------------------------------------
# Constants and small typed helpers
# ---------------------------------------------------------------------------

MAX_AXES = 4
MIN_AXES = 1
AXIS_TYPES = ("integer", "text")
SET_STRATEGIES = ("union", "intersection")
CONFLICT_MODES = ("strict", "priority")
_SUPPORTED_KINDS = {"i": np.int64, "f": np.float64}
_INT64_MIN = -(2 ** 63)
_INT64_MAX = 2 ** 63 - 1


def _json_scalar(value):
    """Turn a NumPy/py scalar into a JSON-native scalar.

    Integer labels and values go through a true Python ``int`` so a 64-bit
    integer is never routed through a float; text stays ``str``.
    """
    if isinstance(value, np.generic):
        if np.issubdtype(value.dtype, np.integer):
            return int(value)
        if np.issubdtype(value.dtype, np.floating):
            return float(value)
        if value.dtype.kind in ("U", "S"):
            return str(value)
        if value.dtype.kind == "b":
            return bool(value)
    return value


def _label_to_json(label):
    if isinstance(label, np.generic):
        if np.issubdtype(label.dtype, np.integer):
            return int(label)
        return str(label)
    return label


# ---------------------------------------------------------------------------
# Descriptor-level data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Axis:
    """A global axis declaration."""

    name: str
    kind: str  # "integer" | "text"
    set_strategy: str  # "union" | "intersection"


@dataclass
class VariableInfo:
    """The logical definition of one numeric variable."""

    name: str
    dimensions: tuple[str, ...]          # global axis names, logical order
    dtype: np.dtype                      # canonical int64/float64
    slots: list["VariableSlot"] = field(default_factory=list)


@dataclass
class VariableSlot:
    """One source's physical storage for one variable."""

    source_id: str
    variable: str
    file_path: Path
    physical_axes: tuple[str, ...]       # axis name per array dimension
    array: np.ndarray                    # read-only memmap (or small array)
    mask: np.ndarray | None              # read-only bool memmap, optional
    mask_path: Path | None
    # label -> physical index on each physical axis, built from coordinates
    local_maps: tuple[dict, ...]


@dataclass
class _SourceCoordinates:
    source_id: str
    # axis name -> (1-D read-only array, label -> index map)
    axes: dict[str, tuple[np.ndarray, dict]]


# ---------------------------------------------------------------------------
# Descriptor loading and validation
# ---------------------------------------------------------------------------

def _resolve(base_dir: Path, raw_path: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise DescriptorError(f"path must be a non-empty string, got {raw_path!r}")
    candidate = (base_dir / raw_path) if not os.path.isabs(raw_path) else Path(raw_path)
    return candidate


def _load_coordinate_vector(spec, base_dir: Path, axis_name: str,
                            source_id: str) -> np.ndarray:
    """Load a 1-D coordinate vector, inline or from a read-only .npy file."""
    where = f"source {source_id!r} axis {axis_name!r}"
    if isinstance(spec, dict):
        if "values" in spec:
            raw = spec["values"]
            inline = True
        elif "path" in spec:
            path = _resolve(base_dir, spec["path"])
            inline = False
            raw = None
        else:
            raise DescriptorError(f"{where} coordinate needs 'values' or 'path'")
    elif isinstance(spec, list):
        raw = spec
        inline = True
    else:
        raise DescriptorError(f"{where} coordinate must be a list or an object")

    if inline:
        try:
            arr = np.asarray(raw)
        except Exception as exc:  # pragma: no cover - defensive
            raise DescriptorError(f"{where} coordinate not array-like: {exc}") from exc
    else:
        try:
            arr = np.load(path, mmap_mode="r")
        except ValueError as exc:
            # NumPy refuses to mmap object arrays; report the supported rule.
            raise DescriptorError(
                f"{where} coordinate file {str(path)!r} cannot be opened "
                f"read-only as a numeric array: {exc}") from exc
        except OSError as exc:
            raise DescriptorError(
                f"{where} coordinate file {str(path)!r} unreadable: {exc}") from exc
    if arr.ndim != 1:
        raise DescriptorError(f"{where} coordinate vector must be 1-D, got shape {arr.shape}")
    if arr.dtype.kind == "b":
        raise DescriptorError(f"{where} coordinate vector must not be boolean")
    if arr.dtype.kind not in ("i", "u", "U", "S"):
        raise DescriptorError(
            f"{where} coordinate vector must hold integers or text, "
            f"got dtype {arr.dtype!r}")
    return arr

def _check_label_type(axis: Axis, value, source_id: str) -> object:
    """Validate one coordinate label against the declared axis type."""
    if axis.kind == "integer":
        if isinstance(value, np.generic):
            if not np.issubdtype(value.dtype, np.integer) or value.dtype.kind == "b":
                raise DescriptorError(
                    f"source {source_id!r} axis {axis.name!r} expects integer "
                    f"labels, got {value.dtype!r} value {_label_to_json(value)!r}")
            py_value = int(value)
        elif isinstance(value, bool) or not isinstance(value, int):
            raise DescriptorError(
                f"source {source_id!r} axis {axis.name!r} expects integer "
                f"labels, got {type(value).__name__} value {value!r}")
        else:
            py_value = value
        if not _INT64_MIN <= py_value <= _INT64_MAX:
            raise DescriptorError(
                f"source {source_id!r} axis {axis.name!r} label {py_value} "
                "is outside signed 64-bit range")
        return py_value
    # text
    if isinstance(value, np.generic):
        if value.dtype.kind not in ("U", "S"):
            raise DescriptorError(
                f"source {source_id!r} axis {axis.name!r} expects text "
                f"labels, got dtype {value.dtype!r}")
        py_value = str(value)
    elif not isinstance(value, str):
        raise DescriptorError(
            f"source {source_id!r} axis {axis.name!r} expects text labels, "
            f"got {type(value).__name__} value {value!r}")
    else:
        py_value = value
    return py_value


def _build_label_map(arr: np.ndarray, axis: Axis, source_id: str) -> dict:
    if axis.kind == "integer":
        # Integer coordinates must be signed 64-bit, never bool or unsigned.
        if arr.dtype.kind != "i" or arr.dtype.itemsize != 8:
            raise DescriptorError(
                f"source {source_id!r} axis {axis.name!r} integer coordinates "
                f"must be signed int64, got dtype {arr.dtype!r}")
    else:
        if arr.dtype.kind not in ("U", "S"):
            raise DescriptorError(
                f"source {source_id!r} axis {axis.name!r} text coordinates "
                f"must hold strings, got dtype {arr.dtype!r}")
    labels: dict = {}
    for index, raw in enumerate(arr):
        label = _check_label_type(axis, raw, source_id)
        if label in labels:
            raise DescriptorError(
                f"source {source_id!r} repeats label {label!r} on axis "
                f"{axis.name!r} at local positions {labels[label]} and {index}; "
                "duplicate coordinates are rejected instead of taking the first")
        labels[label] = index
    return labels


def _read_npy_header(path: Path):
    """Read only the .npy header (shape/dtype), never the payload."""
    try:
        with open(path, "rb") as handle:
            version = _npy_format.read_magic(handle)
            try:
                shape, fortran, dtype = _npy_format._read_array_header(handle, version)
            except AttributeError:  # pragma: no cover - older NumPy
                shape, fortran, dtype = _npy_format.read_array_header_1_0(handle)
    except OSError as exc:
        raise DescriptorError(f"cannot open array file {str(path)!r}: {exc}") from exc
    except ValueError as exc:
        raise DescriptorError(f"not a valid .npy file {str(path)!r}: {exc}") from exc
    return tuple(shape), bool(fortran), np.dtype(dtype)


def _open_value_array(path: Path, variable: str, source_id: str):
    shape, fortran, dtype = _read_npy_header(path)
    if dtype.kind == "O":
        raise DescriptorError(
            f"variable {variable!r} in source {source_id!r} ({str(path)!r}) "
            "is an object array; only int64 and float64 are supported")
    if dtype.kind not in _SUPPORTED_KINDS:
        raise DescriptorError(
            f"variable {variable!r} in source {source_id!r} has unsupported "
            f"dtype {dtype!r}; only signed int64 and float64 (either byte "
            "order) are allowed")
    if dtype.itemsize != 8:
        raise DescriptorError(
            f"variable {variable!r} in source {source_id!r} has {dtype.itemsize}"
            "-byte items; only 64-bit int64/float64 are allowed")
    canonical = np.dtype("<i8" if dtype.kind == "i" else "<f8")
    try:
        array = np.load(path, mmap_mode="r")
    except ValueError as exc:
        raise DescriptorError(
            f"variable {variable!r} in source {source_id!r} cannot be "
            f"memory-mapped read-only: {exc}") from exc
    except OSError as exc:
        raise DescriptorError(
            f"variable {variable!r} file {str(path)!r} unreadable: {exc}") from exc
    # np.load(mmap_mode='r') returns a non-writeable view; assert it so a
    # future NumPy change could never expose a mutable source.
    if array.flags.writeable:
        array.setflags(write=False)
    return array, canonical, shape, fortran


def _open_mask_array(path: Path, variable: str, source_id: str,
                     expected_shape: tuple[int, ...]):
    shape, _fortran, dtype = _read_npy_header(path)
    if dtype.kind != "b":
        raise DescriptorError(
            f"mask for variable {variable!r} in source {source_id!r} "
            f"({str(path)!r}) must be boolean, got {dtype!r}")
    if shape != expected_shape:
        raise DescriptorError(
            f"mask shape {shape} for variable {variable!r} in source "
            f"{source_id!r} does not match value shape {expected_shape}")
    try:
        mask = np.load(path, mmap_mode="r")
    except (ValueError, OSError) as exc:
        raise DescriptorError(
            f"mask file {str(path)!r} for variable {variable!r} unreadable: "
            f"{exc}") from exc
    return mask


# ---------------------------------------------------------------------------
# The opened descriptor
# ---------------------------------------------------------------------------

class LabelStore:
    """An opened, validated label-coordinate descriptor.

    Instances are produced by :func:`open_descriptor` (or
    :func:`load_descriptor`) and are read-only.  They hold coordinate
    vectors and label maps in memory plus read-only memory-mapped arrays;
    :meth:`visible_labels`, :meth:`inspect` and :meth:`iter_cells` perform
    the aligned access without building any aligned data copy.
    """

    def __init__(self, descriptor: dict, base_dir: Path,
                 descriptor_path: Path | None):
        self._raw = descriptor
        self.base_dir = Path(base_dir)
        self.descriptor_path = descriptor_path
        self.axes: tuple[Axis, ...] = ()
        self._axis_by_name: dict[str, Axis] = {}
        self.sources: dict[str, _SourceCoordinates] = {}
        self.variables: dict[str, VariableInfo] = {}
        self._open_arrays: list[np.ndarray] = []
        self._closed = False
        self._build()

    def close(self) -> None:
        """Release every memory-mapped array; the store is unusable after."""
        if self._closed:
            return
        for array in self._open_arrays:
            close = getattr(array, "_mmap", None)
            if close is not None:
                try:
                    close.close()
                except (BufferError, ValueError):
                    pass
        self._open_arrays.clear()
        self._closed = True

    def __enter__(self) -> "LabelStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- construction -----------------------------------------------------

    def _build(self) -> None:
        desc = self._raw
        if not isinstance(desc, dict):
            raise DescriptorError("descriptor root must be a JSON object")
        if desc.get("format") != "archindex-label-read-1":
            raise DescriptorError(
                "descriptor 'format' must be 'archindex-label-read-1'")
        raw_axes = desc.get("axes")
        if not isinstance(raw_axes, list) or not (MIN_AXES <= len(raw_axes) <= MAX_AXES):
            raise DescriptorError(
                f"'axes' must list between {MIN_AXES} and {MAX_AXES} axes")
        axes: list[Axis] = []
        seen_axis_names: set[str] = set()
        for item in raw_axes:
            if not isinstance(item, dict):
                raise DescriptorError("each axis declaration must be an object")
            name = item.get("name")
            kind = item.get("type")
            strategy = item.get("set", "union")
            if not isinstance(name, str) or not name:
                raise DescriptorError(f"axis name must be a non-empty string, got {name!r}")
            if name in seen_axis_names:
                raise DescriptorError(f"axis name {name!r} is declared more than once")
            if kind not in AXIS_TYPES:
                raise DescriptorError(
                    f"axis {name!r} type must be one of {AXIS_TYPES}, got {kind!r}")
            if strategy not in SET_STRATEGIES:
                raise DescriptorError(
                    f"axis {name!r} set strategy must be one of {SET_STRATEGIES}, "
                    f"got {strategy!r}")
            seen_axis_names.add(name)
            axes.append(Axis(name=name, kind=kind, set_strategy=strategy))
        self.axes = tuple(axes)
        self._axis_by_name = {axis.name: axis for axis in axes}

        raw_sources = desc.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise DescriptorError("'sources' must be a non-empty list")
        seen_source_ids: set[str] = set()
        for item in raw_sources:
            source = self._build_source(item)
            if source.source_id in seen_source_ids:
                raise DescriptorError(f"source_id {source.source_id!r} is repeated")
            seen_source_ids.add(source.source_id)
            self.sources[source.source_id] = source

        raw_variables = desc.get("variables")
        if not isinstance(raw_variables, list) or not raw_variables:
            raise DescriptorError("'variables' must be a non-empty list")
        seen_var_names: set[str] = set()
        for item in raw_variables:
            self._build_variable(item, seen_var_names)

    def _build_source(self, item) -> _SourceCoordinates:
        if not isinstance(item, dict):
            raise DescriptorError("each source must be an object")
        source_id = item.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            raise DescriptorError(f"source_id must be a non-empty string, got {source_id!r}")
        raw_coords = item.get("coordinates")
        if not isinstance(raw_coords, dict) or not raw_coords:
            raise DescriptorError(
                f"source {source_id!r} must declare a non-empty 'coordinates' object")
        axes_map: dict[str, tuple[np.ndarray, dict]] = {}
        for axis_name, coord_spec in raw_coords.items():
            if axis_name not in self._axis_by_name:
                raise DescriptorError(
                    f"source {source_id!r} declares coordinates for unknown axis "
                    f"{axis_name!r}; known axes: {[a.name for a in self.axes]}")
            axis = self._axis_by_name[axis_name]
            vector = _load_coordinate_vector(coord_spec, self.base_dir,
                                             axis_name, source_id)
            label_map = _build_label_map(vector, axis, source_id)
            axes_map[axis_name] = (vector, label_map)
        return _SourceCoordinates(source_id=source_id, axes=axes_map)

    def _build_variable(self, item, seen_var_names: set[str]) -> None:
        if not isinstance(item, dict):
            raise DescriptorError("each variable must be an object")
        name = item.get("name")
        source_id = item.get("source_id")
        if not isinstance(name, str) or not name:
            raise DescriptorError(f"variable name must be a non-empty string, got {name!r}")
        if not isinstance(source_id, str) or not source_id:
            raise DescriptorError(f"variable {name!r} needs a non-empty source_id")
        if source_id not in self.sources:
            raise DescriptorError(
                f"variable {name!r} references unknown source_id {source_id!r}")
        raw_dims = item.get("dimensions")
        if not isinstance(raw_dims, list) or not raw_dims:
            raise DescriptorError(
                f"variable {name!r} needs a non-empty 'dimensions' list")
        if len(raw_dims) > MAX_AXES:
            raise DescriptorError(
                f"variable {name!r} uses {len(raw_dims)} dimensions; max is {MAX_AXES}")
        physical_axes: list[str] = []
        for dim in raw_dims:
            if not isinstance(dim, str) or dim not in self._axis_by_name:
                raise DescriptorError(
                    f"variable {name!r} dimension {dim!r} is not a declared axis")
            if dim in physical_axes:
                raise DescriptorError(
                    f"variable {name!r} repeats dimension {dim!r}; dimension "
                    "names of one array must be unique")
            physical_axes.append(dim)
        physical_axes_tuple = tuple(physical_axes)

        # Logical dimension-set consistency is a semantic requirement and is
        # checked before touching the payload or comparing physical shapes,
        # so a missing dimension is never silently treated as broadcasting.
        if name in self.variables:
            existing = self.variables[name]
            if set(existing.dimensions) != set(physical_axes_tuple):
                raise DescriptorError(
                    f"variable {name!r} must keep one logical dimension set "
                    f"across sources; found {list(existing.dimensions)} and "
                    f"{list(physical_axes_tuple)} in source {source_id!r}")
            if any(s.source_id == source_id for s in existing.slots):
                raise DescriptorError(
                    f"variable {name!r} is registered twice in source "
                    f"{source_id!r}")

        file_path = _resolve(self.base_dir, item.get("path", ""))
        array, canonical, _header_shape, _fortran = _open_value_array(
            file_path, name, source_id)
        self._open_arrays.append(array)
        if array.ndim != len(physical_axes_tuple):
            raise DescriptorError(
                f"variable {name!r} in source {source_id!r}: array is "
                f"{array.ndim}-D but {len(physical_axes_tuple)} dimensions "
                f"{physical_axes_tuple} were declared")
        if name in self.variables and self.variables[name].dtype != canonical:
            raise DescriptorError(
                f"variable {name!r} has conflicting storage types: "
                f"{self.variables[name].dtype!r} versus {canonical!r} in "
                f"source {source_id!r}")
        source = self.sources[source_id]
        local_maps: list[dict] = []
        for dim_index, axis_name in enumerate(physical_axes_tuple):
            if axis_name not in source.axes:
                raise DescriptorError(
                    f"variable {name!r} in source {source_id!r} uses axis "
                    f"{axis_name!r} but the source declares no coordinate "
                    "vector for it")
            coord_vector, label_map = source.axes[axis_name]
            if array.shape[dim_index] != coord_vector.shape[0]:
                raise DescriptorError(
                    f"variable {name!r} in source {source_id!r}: physical "
                    f"dimension {dim_index} (axis {axis_name!r}) has length "
                    f"{array.shape[dim_index]} but the source's coordinate "
                    f"vector has {coord_vector.shape[0]} labels")
            local_maps.append(label_map)

        mask = None
        mask_path = None
        if item.get("mask_path") is not None:
            mask_path = _resolve(self.base_dir, item["mask_path"])
            mask = _open_mask_array(mask_path, name, source_id,
                                    tuple(array.shape))
            self._open_arrays.append(mask)

        slot = VariableSlot(
            source_id=source_id,
            variable=name,
            file_path=file_path,
            physical_axes=physical_axes_tuple,
            array=array,
            mask=mask,
            mask_path=mask_path,
            local_maps=tuple(local_maps),
        )

        if name in seen_var_names:
            self.variables[name].slots.append(slot)
        else:
            seen_var_names.add(name)
            # Logical dimension order follows global axis order, regardless
            # of the physical order used by this first source.
            logical_dims = tuple(a.name for a in self.axes
                                 if a.name in set(physical_axes_tuple))
            self.variables[name] = VariableInfo(
                name=name,
                dimensions=logical_dims,
                dtype=canonical,
                slots=[slot],
            )

    # -- inspection -------------------------------------------------------

    @property
    def axis_names(self) -> tuple[str, ...]:
        return tuple(a.name for a in self.axes)

    def axis(self, name: str) -> Axis:
        try:
            return self._axis_by_name[name]
        except KeyError:
            raise ReadRequestError(f"unknown axis {name!r}") from None

    def sources_using_axis(self, axis_name: str) -> list[str]:
        return [sid for sid, src in self.sources.items() if axis_name in src.axes]

    def visible_labels(self, axis_name: str) -> list:
        """Visible labels of one axis, sorted (numeric / code point)."""
        axis = self.axis(axis_name)
        contributing = [src.axes[axis_name][1].keys()
                        for src in self.sources.values()
                        if axis_name in src.axes]
        if not contributing:
            return []
        if axis.set_strategy == "union":
            labels = set()
            for keys in contributing:
                labels.update(keys)
        else:
            labels = set(contributing[0])
            for keys in contributing[1:]:
                labels &= set(keys)
        return sorted(labels)

    def inspect(self) -> dict:
        """A JSON-serialisable description of what this store can serve."""
        return {
            "format": self._raw.get("format"),
            "descriptor_path": str(self.descriptor_path)
            if self.descriptor_path is not None else None,
            "base_dir": str(self.base_dir),
            "axes": [
                {
                    "name": axis.name,
                    "type": axis.kind,
                    "set": axis.set_strategy,
                    "visible_labels": [_label_to_json(x)
                                       for x in self.visible_labels(axis.name)],
                    "sources_using": self.sources_using_axis(axis.name),
                }
                for axis in self.axes
            ],
            "sources": [
                {
                    "source_id": sid,
                    "axes": sorted(src.axes.keys(),
                                   key=lambda n: self.axis_names.index(n)),
                    "coordinate_lengths": {
                        name: int(vec.shape[0]) for name, (vec, _m) in src.axes.items()
                    },
                }
                for sid, src in self.sources.items()
            ],
            "variables": [
                {
                    "name": info.name,
                    "dimensions": list(info.dimensions),
                    "numpy_dtype": str(info.dtype),
                    "sources": [
                        {
                            "source_id": slot.source_id,
                            "path": str(slot.file_path),
                            "physical_axes": list(slot.physical_axes),
                            "shape": list(slot.array.shape),
                            "mask_path": str(slot.mask_path)
                            if slot.mask_path is not None else None,
                        }
                        for slot in info.slots
                    ],
                }
                for info in self.variables.values()
            ],
        }

    # -- request validation ----------------------------------------------

    def resolve_selection(self, selection: dict | None) -> dict[str, list]:
        """Validate requested labels per axis and return ordered label lists."""
        if selection is None:
            selection = {}
        if not isinstance(selection, dict):
            raise ReadRequestError("axis selection must be an object axis->labels")
        resolved: dict[str, list] = {}
        for axis in self.axes:
            visible = self.visible_labels(axis.name)
            if axis.name in selection:
                wanted = selection[axis.name]
                if not isinstance(wanted, list) or not wanted:
                    raise ReadRequestError(
                        f"selection for axis {axis.name!r} must be a non-empty list")
                ordered = []
                seen: set = set()
                for raw in wanted:
                    label = _request_label(axis, raw)
                    if label in seen:
                        raise ReadRequestError(
                            f"label {label!r} is repeated in the request for "
                            f"axis {axis.name!r}")
                    if label not in visible:
                        raise ReadRequestError(
                            f"label {label!r} is not visible on axis "
                            f"{axis.name!r} (strategy {axis.set_strategy}); "
                            f"visible count is {len(visible)}")
                    seen.add(label)
                    ordered.append(label)
                resolved[axis.name] = ordered
            else:
                # An empty visible set (e.g. an empty intersection) is a
                # legitimate empty grid, not a request error.
                resolved[axis.name] = visible
        unknown = [name for name in selection if name not in self._axis_by_name]
        if unknown:
            raise ReadRequestError(f"selection references unknown axes: {unknown}")
        return resolved

    # -- cell iteration ---------------------------------------------------

    def iter_batches(self, variables, selection=None, *,
                     mode: str = "strict", priority=None,
                     batch_size: int | None = None):
        """Yield lists of cells, each list capped at ``batch_size``.

        This is the batch-limited form of :meth:`iter_cells`: batches only
        cap how many cells are materialised between yields, they never change
        cell order or alignment, and no batch builds aligned array copies.
        ``None`` (or a non-positive value) means one batch holding the whole
        stream.
        """
        info_list, selection_resolved, mode, priority = self._prepare_request(
            variables, selection, mode, priority)
        axis_order = [axis.name for axis in self.axes]
        label_lists = [selection_resolved[name] for name in axis_order]
        grid_shape = tuple(len(lst) for lst in label_lists)
        total = 1
        for size in grid_shape:
            total *= size

        batch: list[dict] = []
        for flat in range(total):
            indices = _unravel(flat, grid_shape)
            logical = {axis_order[k]: label_lists[k][indices[k]]
                       for k in range(len(axis_order))}
            cell = {
                "cell": flat,
                "coordinates": [
                    {"axis": axis_order[k],
                     "type": self.axis(axis_order[k]).kind,
                     "label": _label_to_json(label_lists[k][indices[k]])}
                    for k in range(len(axis_order))
                ],
                "variables": {},
            }
            for info in info_list:
                cell["variables"][info.name] = self._read_variable_cell(
                    info, logical, mode, priority)
            batch.append(cell)
            if batch_size and batch_size > 0 and len(batch) >= batch_size:
                yield batch
                batch = []
        if batch or total == 0:
            yield batch

    def iter_cells(self, variables, selection=None, *,
                   mode: str = "strict", priority=None,
                   batch_size: int | None = None):
        """Yield aligned grid cells lazily.

        See :func:`read_cells` for the parameter contract; this method is the
        generator behind both the Python API and the JSONL command.  Request
        errors are raised before the first cell; cell data errors (conflict,
        bad float) are raised at the offending cell during iteration.
        """
        if batch_size is not None:
            for batch in self.iter_batches(variables, selection, mode=mode,
                                           priority=priority,
                                           batch_size=batch_size):
                yield from batch
            return
        info_list, selection_resolved, mode, priority = self._prepare_request(
            variables, selection, mode, priority)
        # Grid ordering: global axis order, last axis fastest.  The product
        # is generated one index tuple at a time -- never a meshgrid.
        axis_order = [axis.name for axis in self.axes]
        label_lists = [selection_resolved[name] for name in axis_order]
        grid_shape = tuple(len(lst) for lst in label_lists)
        total = 1
        for size in grid_shape:
            total *= size

        emitted = 0
        for flat in range(total):
            indices = _unravel(flat, grid_shape)
            logical = {axis_order[k]: label_lists[k][indices[k]]
                       for k in range(len(axis_order))}
            cell = {
                "cell": emitted,
                "coordinates": [
                    {"axis": axis_order[k],
                     "type": self.axis(axis_order[k]).kind,
                     "label": _label_to_json(label_lists[k][indices[k]])}
                    for k in range(len(axis_order))
                ],
                "variables": {},
            }
            for info in info_list:
                cell["variables"][info.name] = self._read_variable_cell(
                    info, logical, mode, priority)
            yield cell
            emitted += 1

    def _prepare_request(self, variables, selection, mode, priority):
        if mode not in CONFLICT_MODES:
            raise ReadRequestError(
                f"mode must be one of {CONFLICT_MODES}, got {mode!r}")
        if isinstance(variables, str):
            variables = [variables]
        if not isinstance(variables, (list, tuple)) or not variables:
            raise ReadRequestError("at least one variable must be requested")
        info_list: list[VariableInfo] = []
        seen: set[str] = set()
        for name in variables:
            if not isinstance(name, str):
                raise ReadRequestError(f"variable names must be strings, got {name!r}")
            if name in seen:
                raise ReadRequestError(f"variable {name!r} is requested twice")
            if name not in self.variables:
                raise ReadRequestError(
                    f"unknown variable {name!r}; known: {sorted(self.variables)}")
            seen.add(name)
            info_list.append(self.variables[name])
        selection_resolved = self.resolve_selection(selection)
        priority_resolved = None
        if mode == "priority":
            if not isinstance(priority, list) or not priority:
                raise ReadRequestError(
                    "priority mode needs a non-empty 'priority' list of source ids")
            if len(set(priority)) != len(priority):
                raise ReadRequestError("priority source ids must be unique")
            unknown = [sid for sid in priority if sid not in self.sources]
            if unknown:
                raise ReadRequestError(f"priority names unknown sources: {unknown}")
            needed: set[str] = set()
            for info in info_list:
                needed.update(slot.source_id for slot in info.slots)
            missing = sorted(needed - set(priority))
            if missing:
                raise ReadRequestError(
                    f"priority list omits sources that carry requested "
                    f"variables: {missing}")
            priority_resolved = list(priority)
        elif priority is not None:
            raise ReadRequestError("priority list is only valid in priority mode")
        return info_list, selection_resolved, mode, priority_resolved

    def _read_variable_cell(self, info: VariableInfo, logical: dict,
                            mode: str, priority) -> dict:
        evidence: list[dict] = []
        for slot in info.slots:
            physical_indices = []
            located = True
            for axis_name in slot.physical_axes:
                label_map = slot.local_maps[len(physical_indices)]
                label = logical[axis_name]
                if label not in label_map:
                    located = False
                    break
                physical_indices.append(label_map[label])
            if not located:
                # This source cannot serve a value that never existed there.
                continue
            index_tuple = tuple(physical_indices)
            if slot.mask is not None and not bool(slot.mask[index_tuple]):
                # Masked: the underlying bit pattern is never read.
                evidence.append({
                    "state": "masked",
                    "source_id": slot.source_id,
                    "file": str(slot.file_path),
                    "physical_axes": list(slot.physical_axes),
                    "indices": list(index_tuple),
                })
                continue
            raw_value = slot.array[index_tuple]
            if info.dtype.kind == "f" and not np.isfinite(raw_value):
                kind = "NaN" if np.isnan(raw_value) else "infinity"
                raise CellDataError(
                    f"variable {info.name!r} has an unmasked {kind} at logical "
                    f"coordinate {_logical_text(logical)}; source "
                    f"{slot.source_id!r} file {str(slot.file_path)!r} physical "
                    f"indices {list(index_tuple)} (axes {list(slot.physical_axes)})",
                    variable=info.name, logical=_logical_json(logical),
                    evidence=[{
                        "state": "bad_float",
                        "source_id": slot.source_id,
                        "file": str(slot.file_path),
                        "physical_axes": list(slot.physical_axes),
                        "indices": list(index_tuple),
                        "value": float(raw_value),
                    }])
            value = _json_scalar(raw_value)
            evidence.append({
                "state": "value",
                "source_id": slot.source_id,
                "file": str(slot.file_path),
                "physical_axes": list(slot.physical_axes),
                "indices": list(index_tuple),
                "value": value,
            })

        valid = [ev for ev in evidence if ev["state"] == "value"]
        if not valid:
            if evidence:
                return {"state": "masked", "evidence": evidence}
            return {"state": "absent", "evidence": []}

        if mode == "strict":
            first = valid[0]
            for other in valid[1:]:
                if not _exact_equal(first["value"], other["value"]):
                    raise CellDataError(
                        f"strict conflict for variable {info.name!r} at logical "
                        f"coordinate {_logical_text(logical)}: "
                        f"{first['source_id']!r} value {first['value']!r} != "
                        f"{other['source_id']!r} value {other['value']!r}",
                        variable=info.name, logical=_logical_json(logical),
                        evidence=[first, other])
            return {"state": "value", "value": first["value"], "evidence": evidence}
        # priority: first valid source in the caller's source order
        rank = {sid: i for i, sid in enumerate(priority)}
        chosen = sorted(valid, key=lambda ev: rank[ev["source_id"]])[0]
        return {"state": "value", "value": chosen["value"],
                "selected_source": chosen["source_id"], "evidence": evidence}

    # -- convenience materialisation (small requests only; streaming users
    #    should call iter_cells directly) -------------------------------

    def read_cells(self, variables, selection=None, *,
                   mode: str = "strict", priority=None):
        """Materialise a small request and return ``(cells, summary)``."""
        info_list, _sel, _mode, _prio = self._prepare_request(
            variables, selection, mode, priority)
        cells = list(self.iter_cells(variables, selection,
                                     mode=mode, priority=priority))
        summary = summarize(cells, [info.name for info in info_list])
        return cells, summary


# ---------------------------------------------------------------------------
# Module-level helpers and public entry points
# ---------------------------------------------------------------------------

def _request_label(axis: Axis, raw):
    if axis.kind == "integer":
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ReadRequestError(
                f"axis {axis.name!r} expects integer labels, got {raw!r}")
        if not _INT64_MIN <= raw <= _INT64_MAX:
            raise ReadRequestError(
                f"axis {axis.name!r} label {raw} outside signed 64-bit range")
        return raw
    if not isinstance(raw, str):
        raise ReadRequestError(
            f"axis {axis.name!r} expects text labels, got {raw!r}")
    return raw


def _unravel(flat: int, shape: tuple[int, ...]) -> tuple[int, ...]:
    indices = []
    remainder = flat
    for size in reversed(shape):
        indices.append(remainder % size)
        remainder //= size
    return tuple(reversed(indices))


def _exact_equal(a, b) -> bool:
    # Both values are JSON-native ints/floats at this point.  int vs float
    # is never equal (e.g. 1 != 1.0), floats compare exactly with no
    # tolerance; Python's == already treats -0.0 == +0.0 as true.
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if type(a) is not type(b):
        return False
    return a == b


def _logical_text(logical: dict) -> str:
    return "{" + ", ".join(f"{k}={_label_to_json(v)!r}"
                           for k, v in logical.items()) + "}"


def _logical_json(logical: dict) -> dict:
    return {k: _label_to_json(v) for k, v in logical.items()}


def summarize(cells, variable_names) -> dict:
    """Count value/masked/absent states per variable over emitted cells."""
    counts = {name: {"value": 0, "masked": 0, "absent": 0}
              for name in variable_names}
    for cell in cells:
        for name in variable_names:
            counts[name][cell["variables"][name]["state"]] += 1
    return {"cells": len(cells), "variables": counts}


def load_descriptor(path: str | os.PathLike) -> LabelStore:
    """Parse a descriptor JSON file and open its arrays read-only."""
    descriptor_path = Path(path)
    try:
        with open(descriptor_path, "r", encoding="utf-8") as handle:
            descriptor = json.load(handle)
    except OSError as exc:
        raise DescriptorError(
            f"cannot read descriptor {str(descriptor_path)!r}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DescriptorError(
            f"descriptor {str(descriptor_path)!r} is not valid JSON: {exc}") from exc
    return open_descriptor(descriptor, base_dir=descriptor_path.parent,
                           descriptor_path=descriptor_path)


# Kept under the name the task brief uses ("open/inspect a descriptor").
def open_descriptor(descriptor: dict, *, base_dir: str | os.PathLike | None = None,
                    descriptor_path: str | os.PathLike | None = None) -> LabelStore:
    """Open an in-memory descriptor dict; relative paths resolve at base_dir."""
    if base_dir is None:
        if descriptor_path is not None:
            base_dir = Path(descriptor_path).parent
        else:
            base_dir = Path.cwd()
    return LabelStore(descriptor, Path(base_dir),
                      Path(descriptor_path) if descriptor_path is not None else None)
