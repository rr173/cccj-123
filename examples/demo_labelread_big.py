#!/usr/bin/env python3
"""Large-file (>1 GiB) label-read demonstration with memory/access evidence.

Two ``.npy`` matrices are created whose *file* sizes together exceed one
gibibyte, but only a small rectangle of tail, non-adjacent labels is read.
The script records:

* peak resident memory of the whole process (``ru_maxrss``) and the size
  of the label/coordinate structures,
* every distinct 4 KiB file block actually touched behind the requested
  cells, via a thin instrumented wrapper around the read-only memmaps,
* the independently recomputed expected values at those physical positions.

If the reader copied the arrays (``np.load`` without mmap), enumerated the
full Cartesian grid or built a meshgrid/transposed copy, the peak RSS or
the touched-block count would be on the order of the full 1 GiB payload.
The observed footprint stays within a few hundred MiB at most and the
touched blocks correspond exactly to the pages backing the requested cells.

Runs as its own process so the reported peak RSS is uncontaminated::

    python3 -I examples/demo_labelread_big.py
"""

from __future__ import annotations

import json
import os
import resource
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from archindex._vendor import ensure_numpy  # noqa: E402

np = ensure_numpy()

from archindex import load_descriptor  # noqa: E402

PAGE = 4096
N = 8192                       # labels per axis
MATRIX_BYTES = N * N * 8      # 512 MiB per matrix, 1 GiB total


class AccessTracker:
    """Record distinct file pages behind scalar reads of a memmap."""

    def __init__(self):
        self.pages: dict[str, set[int]] = {}
        self.scalar_reads = 0

    def wrap(self, memmap, file_name: str):
        tracker = self
        base_offset = self._data_offset(file_name)

        class _Tracked:
            def __init__(self, inner):
                self._inner = inner
                self.shape = inner.shape
                self.dtype = inner.dtype
                self.ndim = inner.ndim
                self.flags = inner.flags

            def __getitem__(self, key):
                value = self._inner[key]
                tracker.scalar_reads += 1
                byte_pos = base_offset + self._byte_offset(key)
                tracker.pages.setdefault(file_name, set()).add(
                    byte_pos // PAGE)
                return value

            def _byte_offset(self, key):
                if not isinstance(key, tuple):
                    key = (key,)
                strides = self._inner.strides
                return sum(int(index) * stride
                           for index, stride in zip(key, strides))

        return _Tracked(memmap)

    @staticmethod
    def _data_offset(file_name: str) -> int:
        with open(file_name, "rb") as handle:
            magic = handle.read(6)
            major = magic[4]
            if major == 1:
                hlen = int.from_bytes(handle.read(2), "little")
            else:
                hlen = int.from_bytes(handle.read(4), "little")
        return 6 + (2 if major == 1 else 4) + hlen


def _write_big_npy(path: str, tag: int) -> None:
    """Create an (N, N) float64 .npy and fill it in bounded row chunks."""
    from numpy.lib import format as nf

    # open_memmap with w+ writes a standard .npy header itself.
    payload = nf.open_memmap(path, mode="w+", dtype="<f8", shape=(N, N),
                             fortran_order=False, version=(1, 0))
    batch_steps = np.arange(N, dtype="<f8") * np.float64(0.001)
    for row in range(N):
        payload[row, :] = np.float64(tag + row) + batch_steps
    payload.flush()
    del payload


def build_big_files(directory: str) -> str:
    os.makedirs(directory, exist_ok=True)
    labels = np.arange(N, dtype="<i8")  # 0 .. 8191
    np.save(os.path.join(directory, "device.npy"), labels)
    np.save(os.path.join(directory, "batch.npy"), labels)
    path_a = os.path.join(directory, "matrix_a.npy")
    path_b = os.path.join(directory, "matrix_b.npy")
    _write_big_npy(path_a, 1000)
    _write_big_npy(path_b, 2000)
    descriptor = {
        "format": "archindex-label-read-1",
        "axes": [
            {"name": "device", "type": "integer", "set": "union"},
            {"name": "batch", "type": "integer", "set": "union"},
        ],
        "sources": [
            {"source_id": "A", "coordinates": {
                "device": {"path": "device.npy"},
                "batch": {"path": "batch.npy"}}},
            {"source_id": "B", "coordinates": {
                "device": {"path": "device.npy"},
                "batch": {"path": "batch.npy"}}},
        ],
        "variables": [
            {"name": "m", "source_id": "A", "path": "matrix_a.npy",
             "dimensions": ["device", "batch"]},
            {"name": "m", "source_id": "B", "path": "matrix_b.npy",
             "dimensions": ["device", "batch"]},
        ],
    }
    desc_path = os.path.join(directory, "big_desc.json")
    with open(desc_path, "w", encoding="utf-8") as handle:
        json.dump(descriptor, handle)
    return desc_path


def main() -> int:
    workdir = os.path.join(ROOT, "examples", "data", "big")
    if not os.path.exists(os.path.join(workdir, "big_desc.json")):
        print("building 1 GiB of matrices (one-time)...")
        started = time.time()
        build_big_files(workdir)
        print(f"build took {time.time() - started:.1f}s")
    total_bytes = sum(
        os.path.getsize(os.path.join(workdir, name))
        for name in ("matrix_a.npy", "matrix_b.npy"))
    print(f"matrix file bytes: {total_bytes} "
          f"({total_bytes / (1024 ** 3):.3f} GiB)")
    assert total_bytes > 1024 ** 3

    store = load_descriptor(os.path.join(workdir, "big_desc.json"))
    # Instrument the opened memmaps without changing read semantics.
    tracker = AccessTracker()
    slots = store.variables["m"].slots
    wrapped = {}
    for slot in slots:
        wrapped[slot.source_id] = tracker.wrap(
            slot.array, str(slot.file_path))
        slot.array = wrapped[slot.source_id]

    # Tail, non-adjacent labels: two rectangles of 4 x 4 cells.  The two
    # matrices hold deliberately different values, so priority selects B
    # while both sources are still located, read and retained as evidence.
    selection = {
        "device": [8189, 8191, 8100, 8175],
        "batch": [8190, 8188, 8111, 8160],
    }
    started = time.time()
    cells = list(store.iter_cells(
        ["m"], selection=selection,
        mode="priority", priority=["B", "A"]))
    elapsed = time.time() - started
    assert len(cells) == 16

    # Independent recomputation of every value and physical position.
    for cell in cells:
        coord = {x["axis"]: x["label"] for x in cell["coordinates"]}
        d, b = coord["device"], coord["batch"]
        result = cell["variables"]["m"]
        assert result["state"] == "value"
        assert result["selected_source"] == "B"
        # both sources located the full coordinate and were read
        assert sorted(ev["source_id"] for ev in result["evidence"]) == [
            "A", "B"]
        for ev in result["evidence"]:
            tag = 1000 if ev["source_id"] == "A" else 2000
            expected = float(np.float64(tag + d) + np.float64(b) * 0.001)
            assert ev["indices"] == [d, b], ev
            assert abs(ev["value"] - expected) < 1e-12, (ev, expected)
        tag = 2000
        expected = float(np.float64(tag + d) + np.float64(b) * 0.001)
        assert abs(result["value"] - expected) < 1e-12

    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    touched_pages = sum(len(pages) for pages in tracker.pages.values())
    touched_bytes = touched_pages * PAGE
    report = {
        "matrix_bytes_total": total_bytes,
        "grid_cells_read": len(cells),
        "scalar_reads": tracker.scalar_reads,
        "touched_4kib_pages": touched_pages,
        "touched_bytes_estimate": touched_bytes,
        "touched_fraction_of_payload": touched_bytes / total_bytes,
        "peak_rss_bytes": rss_kb * 1024,
        "peak_rss_mib": rss_kb / 1024,
        "elapsed_seconds": round(elapsed, 4),
        "coordinate_label_count": 2 * N,
    }
    print(json.dumps(report, indent=2))
    out = os.path.join(ROOT, "examples", "data", "big_access_report.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    # The hard claims: nothing close to the full payload is resident, and
    # only pages backing 32 scalar reads (16 cells x 2 sources) are touched.
    assert touched_bytes < 8 * 1024 * 1024, touched_bytes
    assert rss_kb * 1024 < 512 * 1024 * 1024, rss_kb
    print("OK: tail rectangle read without enumerating the global grid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
