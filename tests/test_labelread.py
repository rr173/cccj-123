"""Tests for label-coordinate joint reading (archindex.labelread).

All arrays are synthesised in temporary directories; nothing is read from a
global location and every test runs under ``python -I`` via run_tests.py.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from archindex._vendor import ensure_numpy  # noqa: E402

np = ensure_numpy()

from archindex import (  # noqa: E402
    CellDataError,
    DescriptorError,
    ReadRequestError,
    load_descriptor,
    open_descriptor,
)


def save_npy(directory: str, name: str, array) -> str:
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        np.save(handle, array)
    return path


class DescriptorBuilder:
    """Small helper assembling descriptors over a temp directory."""

    def __init__(self, directory: str, axes, sources, variables):
        self.directory = directory
        self.descriptor = {
            "format": "archindex-label-read-1",
            "axes": axes,
            "sources": sources,
            "variables": variables,
        }

    def path(self) -> str:
        path = os.path.join(self.directory, "desc.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.descriptor, handle)
        return path

    def open(self):
        return load_descriptor(self.path())

    def mutate(self, **changes):
        desc = json.loads(json.dumps(self.descriptor))
        desc.update(changes)
        return open_descriptor(desc, base_dir=self.directory)


def coord_inline(values):
    return {"values": values}


class HandExampleTest(unittest.TestCase):
    """The exact worked example from the task brief."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        save_npy(d, "jia_dev.npy", np.array(["D2", "D1"], dtype="U8"))
        save_npy(d, "jia_batch.npy", np.array([20, 10], dtype="<i8"))
        save_npy(d, "jia_v.npy",
                 np.array([[220, 210], [120, 110]], dtype="<i8"))
        save_npy(d, "yi_dev.npy", np.array(["D3", "D2"], dtype="U8"))
        save_npy(d, "yi_batch.npy", np.array([10, 30], dtype="<i8"))
        save_npy(d, "yi_v.npy",
                 np.array([[310, 210], [330, 230]], dtype="<i8"))
        save_npy(d, "bing_dev.npy", np.array(["D3"], dtype="U8"))
        save_npy(d, "bing_batch.npy", np.array([20], dtype="<i8"))
        save_npy(d, "bing_v.npy", np.array([[320]], dtype="<i8"))
        save_npy(d, "bing_mask.npy", np.array([[False]], dtype=np.bool_))
        save_npy(d, "cal_jia.npy", np.array([2002, 1001], dtype="<i8"))
        save_npy(d, "cal_yi.npy", np.array([3003, 2002], dtype="<i8"))
        axes = [
            {"name": "device", "type": "text", "set": "union"},
            {"name": "batch", "type": "integer", "set": "union"},
        ]
        sources = [
            {"source_id": "jia", "coordinates": {
                "device": {"path": "jia_dev.npy"},
                "batch": {"path": "jia_batch.npy"}}},
            {"source_id": "yi", "coordinates": {
                "device": {"path": "yi_dev.npy"},
                "batch": {"path": "yi_batch.npy"}}},
            {"source_id": "bing", "coordinates": {
                "device": {"path": "bing_dev.npy"},
                "batch": {"path": "bing_batch.npy"}}},
        ]
        variables = [
            {"name": "value", "source_id": "jia", "path": "jia_v.npy",
             "dimensions": ["device", "batch"]},
            {"name": "value", "source_id": "yi", "path": "yi_v.npy",
             "dimensions": ["batch", "device"]},
            {"name": "value", "source_id": "bing", "path": "bing_v.npy",
             "mask_path": "bing_mask.npy",
             "dimensions": ["device", "batch"]},
            {"name": "calibration", "source_id": "jia", "path": "cal_jia.npy",
             "dimensions": ["device"]},
            {"name": "calibration", "source_id": "yi", "path": "cal_yi.npy",
             "dimensions": ["device"]},
        ]
        self.builder = DescriptorBuilder(d, axes, sources, variables)
        self.store = self.builder.open()

    def tearDown(self):
        self.tmp.cleanup()

    def test_visible_labels_sorted(self):
        self.assertEqual(self.store.visible_labels("device"),
                         ["D1", "D2", "D3"])
        self.assertEqual(self.store.visible_labels("batch"), [10, 20, 30])

    def test_expected_grid(self):
        cells, summary = self.store.read_cells(["value", "calibration"])
        grid = {tuple(c["coordinates"][i]["label"]
                      for i in range(2)): c for c in cells}
        expected = {
            ("D1", 10): ("value", 110), ("D1", 20): ("value", 120),
            ("D1", 30): ("absent", None),
            ("D2", 10): ("value", 210), ("D2", 20): ("value", 220),
            ("D2", 30): ("value", 230),
            ("D3", 10): ("value", 310), ("D3", 20): ("masked", None),
            ("D3", 30): ("value", 330),
        }
        for key, (state, value) in expected.items():
            result = grid[key]["variables"]["value"]
            self.assertEqual(result["state"], state, key)
            if state == "value":
                self.assertEqual(result["value"], value)
        # (D2,10) carries two independent pieces of evidence
        evidence = grid[("D2", 10)]["variables"]["value"]["evidence"]
        self.assertEqual(sorted(e["source_id"] for e in evidence),
                         ["jia", "yi"])
        self.assertEqual([e["indices"] for e in evidence],
                         [[0, 1], [0, 1]])
        # masked cell hides the underlying 320
        masked = grid[("D3", 20)]["variables"]["value"]
        self.assertNotIn("value", json.dumps(masked))
        self.assertEqual(summary["variables"]["value"],
                         {"value": 7, "masked": 1, "absent": 1})

    def test_calibration_broadcasts_along_batch(self):
        cells, _ = self.store.read_cells(["calibration"])
        grid = {tuple(c["coordinates"][i]["label"]
                      for i in range(2)): c for c in cells}
        for batch in (10, 20, 30):
            self.assertEqual(grid[("D1", batch)]["variables"]
                             ["calibration"]["value"], 1001)
            self.assertEqual(grid[("D3", batch)]["variables"]
                             ["calibration"]["value"], 3003)

    def test_last_axis_fastest_ordering(self):
        cells, _ = self.store.read_cells(["value"])
        keys = [tuple(c["coordinates"][i]["label"]
                      for i in range(2)) for c in cells]
        self.assertEqual(keys, [
            ("D1", 10), ("D1", 20), ("D1", 30),
            ("D2", 10), ("D2", 20), ("D2", 30),
            ("D3", 10), ("D3", 20), ("D3", 30),
        ])

    def test_arrays_are_readonly(self):
        for info in self.store.variables.values():
            for slot in info.slots:
                self.assertFalse(slot.array.flags.writeable)

    def test_reordering_registration_and_axes_and_coordinates_is_invariant(self):
        """Swap source order, transpose a matrix and reverse coordinate order.

        The physical files are rebuilt with matching reordering; every
        logical reading must stay identical to the original descriptor.
        """
        d2 = tempfile.mkdtemp()
        # jia again, but coordinates reversed and matrix rows reversed to
        # stay physically consistent.
        save_npy(d2, "jia_dev.npy", np.array(["D1", "D2"], dtype="U8"))
        save_npy(d2, "jia_batch.npy", np.array([10, 20], dtype="<i8"))
        save_npy(d2, "jia_v.npy",
                 np.array([[110, 120], [210, 220]], dtype="<i8"))
        # yi physically transposed to device x batch, reversed device order
        save_npy(d2, "yi_dev.npy", np.array(["D2", "D3"], dtype="U8"))
        save_npy(d2, "yi_batch.npy", np.array([30, 10], dtype="<i8"))
        save_npy(d2, "yi_v.npy",
                 np.array([[230, 210], [330, 310]], dtype="<i8"))
        save_npy(d2, "bing_dev.npy", np.array(["D3"], dtype="U8"))
        save_npy(d2, "bing_batch.npy", np.array([20], dtype="<i8"))
        save_npy(d2, "bing_v.npy", np.array([[320]], dtype="<i8"))
        save_npy(d2, "bing_mask.npy", np.array([[False]], dtype=np.bool_))
        save_npy(d2, "cal_jia.npy", np.array([1001, 2002], dtype="<i8"))
        save_npy(d2, "cal_yi.npy", np.array([2002, 3003], dtype="<i8"))
        axes = [
            {"name": "device", "type": "text", "set": "union"},
            {"name": "batch", "type": "integer", "set": "union"},
        ]
        sources = [
            {"source_id": "bing", "coordinates": {
                "device": {"path": "bing_dev.npy"},
                "batch": {"path": "bing_batch.npy"}}},
            {"source_id": "yi", "coordinates": {
                "device": {"path": "yi_dev.npy"},
                "batch": {"path": "yi_batch.npy"}}},
            {"source_id": "jia", "coordinates": {
                "device": {"path": "jia_dev.npy"},
                "batch": {"path": "jia_batch.npy"}}},
        ]
        variables = [
            {"name": "calibration", "source_id": "yi", "path": "cal_yi.npy",
             "dimensions": ["device"]},
            {"name": "value", "source_id": "bing", "path": "bing_v.npy",
             "mask_path": "bing_mask.npy",
             "dimensions": ["device", "batch"]},
            {"name": "value", "source_id": "yi", "path": "yi_v.npy",
             "dimensions": ["device", "batch"]},
            {"name": "calibration", "source_id": "jia",
             "path": "cal_jia.npy", "dimensions": ["device"]},
            {"name": "value", "source_id": "jia", "path": "jia_v.npy",
             "dimensions": ["device", "batch"]},
        ]
        reordered = DescriptorBuilder(d2, axes, sources, variables).open()
        a, _ = self.store.read_cells(["value", "calibration"])
        b, _ = reordered.read_cells(["value", "calibration"])

        def logical(store, cells):
            """Normalise evidence to the logical labels each index points at."""
            out = []
            for cell in cells:
                slim = {
                    "coordinates": cell["coordinates"],
                    "variables": {},
                }
                for name, result in cell["variables"].items():
                    evidence = []
                    for ev in result["evidence"]:
                        source = store.sources[ev["source_id"]]
                        labels = []
                        for axis_name, index in zip(ev["physical_axes"],
                                                    ev["indices"]):
                            vector = source.axes[axis_name][0]
                            label = vector[index]
                            labels.append((axis_name,
                                           label.item() if hasattr(label, "item")
                                           else label))
                        evidence.append((ev["source_id"], ev["state"],
                                         tuple(sorted(labels)),
                                         ev.get("value")))
                    slim["variables"][name] = {
                        "state": result["state"],
                        **({"value": result["value"]}
                           if result["state"] == "value" else {}),
                        "evidence": sorted(evidence),
                    }
                out.append(slim)
            return out

        self.assertEqual(logical(self.store, a), logical(reordered, b))


class IntersectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        save_npy(d, "a_dev.npy", np.array(["D1", "D2"], dtype="U8"))
        save_npy(d, "b_dev.npy", np.array(["D2", "D3"], dtype="U8"))
        save_npy(d, "a_b.npy", np.array([10, 20], dtype="<i8"))
        save_npy(d, "b_b.npy", np.array([20, 30], dtype="<i8"))
        # 2-D arrays aligned to each source's (device x batch) coordinates;
        # the overlapping cell (D2, 20) holds the same value in both sources
        save_npy(d, "a_v.npy",
                 np.array([[10, 20], [11, 77]], dtype="<i8"))
        save_npy(d, "b_v.npy",
                 np.array([[77, 40], [31, 41]], dtype="<i8"))

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self, device_strategy, batch_strategy):
        d = self.tmp.name
        axes = [
            {"name": "device", "type": "text", "set": device_strategy},
            {"name": "batch", "type": "integer", "set": batch_strategy},
        ]
        sources = [
            {"source_id": "a", "coordinates": {
                "device": {"path": "a_dev.npy"},
                "batch": {"path": "a_b.npy"}}},
            {"source_id": "b", "coordinates": {
                "device": {"path": "b_dev.npy"},
                "batch": {"path": "b_b.npy"}}},
        ]
        variables = [
            {"name": "v", "source_id": "a", "path": "a_v.npy",
             "dimensions": ["device", "batch"]},
            {"name": "v", "source_id": "b", "path": "b_v.npy",
             "dimensions": ["device", "batch"]},
        ]
        return DescriptorBuilder(d, axes, sources, variables).open()

    def test_intersection_keeps_only_shared_labels(self):
        store = self._store("intersection", "intersection")
        self.assertEqual(store.visible_labels("device"), ["D2"])
        self.assertEqual(store.visible_labels("batch"), [20])
        cells, summary = store.read_cells(["v"])
        self.assertEqual(summary["cells"], 1)
        result = cells[0]["variables"]["v"]
        self.assertEqual(result["state"], "value")

    def test_empty_intersection_yields_empty_grid(self):
        store = self._store("intersection", "union")
        save_npy(self.tmp.name, "b_dev2.npy",
                 np.array(["D8", "D9"], dtype="U8"))
        # rebuild source b coordinates so devices do not overlap
        desc = json.loads(json.dumps(store._raw))
        desc["sources"][1]["coordinates"]["device"] = {"path": "b_dev2.npy"}
        store2 = open_descriptor(desc, base_dir=self.tmp.name)
        self.assertEqual(store2.visible_labels("device"), [])
        cells, summary = store2.read_cells(["v"])
        self.assertEqual(cells, [])
        self.assertEqual(summary["cells"], 0)
        self.assertEqual(summary["variables"]["v"],
                         {"value": 0, "masked": 0, "absent": 0})

    def test_sources_not_using_axis_are_not_empty_sets(self):
        # add a device-only variable source: it must not make the batch
        # intersection become empty
        d = self.tmp.name
        save_npy(d, "c_dev.npy", np.array(["D2"], dtype="U8"))
        save_npy(d, "c_v.npy", np.array([99], dtype="<i8"))
        store = self._store("intersection", "intersection")
        desc = json.loads(json.dumps(store._raw))
        desc["sources"].append({"source_id": "c", "coordinates": {
            "device": {"path": "c_dev.npy"}}})
        desc["variables"].append(
            {"name": "w", "source_id": "c", "path": "c_v.npy",
             "dimensions": ["device"]})
        store2 = open_descriptor(desc, base_dir=d)
        self.assertEqual(store2.visible_labels("batch"), [20])
        self.assertEqual(store2.visible_labels("device"), ["D2"])


class ConflictAndPriorityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        save_npy(d, "a_dev.npy", np.array(["D1"], dtype="U8"))
        save_npy(d, "b_dev.npy", np.array(["D1"], dtype="U8"))
        save_npy(d, "a_b.npy", np.array([1], dtype="<i8"))
        save_npy(d, "b_b.npy", np.array([1], dtype="<i8"))
        save_npy(d, "a_v.npy", np.array([[100]], dtype="<i8"))
        save_npy(d, "b_v.npy", np.array([[101]], dtype="<i8"))
        # third source: same coordinate, masked
        save_npy(d, "c_dev.npy", np.array(["D1"], dtype="U8"))
        save_npy(d, "c_b.npy", np.array([1], dtype="<i8"))
        save_npy(d, "c_v.npy", np.array([[102]], dtype="<i8"))
        save_npy(d, "c_mask.npy", np.array([[False]], dtype=np.bool_))
        axes = [
            {"name": "device", "type": "text", "set": "union"},
            {"name": "batch", "type": "integer", "set": "union"},
        ]
        sources = [
            {"source_id": "a", "coordinates": {
                "device": {"path": "a_dev.npy"}, "batch": {"path": "a_b.npy"}}},
            {"source_id": "b", "coordinates": {
                "device": {"path": "b_dev.npy"}, "batch": {"path": "b_b.npy"}}},
            {"source_id": "c", "coordinates": {
                "device": {"path": "c_dev.npy"}, "batch": {"path": "c_b.npy"}}},
        ]
        variables = [
            {"name": "v", "source_id": "a", "path": "a_v.npy",
             "dimensions": ["device", "batch"]},
            {"name": "v", "source_id": "b", "path": "b_v.npy",
             "dimensions": ["device", "batch"]},
            {"name": "v", "source_id": "c", "path": "c_v.npy",
             "mask_path": "c_mask.npy", "dimensions": ["device", "batch"]},
        ]
        self.store = DescriptorBuilder(d, axes, sources, variables).open()

    def tearDown(self):
        self.tmp.cleanup()

    def test_strict_conflict_reports_both_positions(self):
        with self.assertRaises(CellDataError) as caught:
            list(self.store.iter_cells(["v"]))
        err = caught.exception
        self.assertEqual(err.variable, "v")
        source_ids = {e["source_id"] for e in err.evidence}
        self.assertEqual(source_ids, {"a", "b"})
        # masked source c never participates in the comparison
        self.assertNotIn("c", source_ids)
        values = sorted(e["value"] for e in err.evidence)
        self.assertEqual(values, [100, 101])

    def test_priority_skips_masked_high_priority_source(self):
        # c is listed first but masked; the engine must skip it
        result = list(self.store.iter_cells(
            ["v"], mode="priority", priority=["c", "a", "b"]))
        self.assertEqual(result[0]["variables"]["v"]["value"], 100)
        self.assertEqual(result[0]["variables"]["v"]["selected_source"], "a")
        # priority independent of descriptor registration order: b first now
        result_b = list(self.store.iter_cells(
            ["v"], mode="priority", priority=["b", "a", "c"]))
        self.assertEqual(result_b[0]["variables"]["v"]["value"], 101)

    def test_priority_requires_complete_unique_list(self):
        with self.assertRaises(ReadRequestError):
            list(self.store.iter_cells(["v"], mode="priority",
                                       priority=["a"]))
        with self.assertRaises(ReadRequestError):
            list(self.store.iter_cells(["v"], mode="priority",
                                       priority=["a", "a", "b", "c"]))
        with self.assertRaises(ReadRequestError):
            list(self.store.iter_cells(
                ["v"], mode="priority", priority=["a", "b", "c", "zzz"]))

    def test_strict_equal_integers_keep_all_evidence(self):
        d = self.tmp.name
        save_npy(d, "b_v_eq.npy", np.array([[100]], dtype="<i8"))
        desc = json.loads(json.dumps(self.store._raw))
        for item in desc["variables"]:
            if item["source_id"] == "b":
                item["path"] = "b_v_eq.npy"
        store = open_descriptor(desc, base_dir=d)
        cells = list(store.iter_cells(["v"]))
        evidence = cells[0]["variables"]["v"]["evidence"]
        self.assertEqual({e["source_id"] for e in evidence},
                         {"a", "b", "c"})
        self.assertEqual([e["state"] for e in evidence],
                         ["value", "value", "masked"])


class FloatSemanticsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        save_npy(d, "dev.npy", np.array(["D1", "D2", "D3", "D4"], dtype="U8"))
        save_npy(d, "v.npy", np.array(
            [np.nan, np.inf, 0.0, -0.0], dtype=">f8"))  # big endian float64
        save_npy(d, "v_mask.npy",
                 np.array([False, True, True, True], dtype=np.bool_))
        axes = [{"name": "device", "type": "text", "set": "union"}]
        sources = [{"source_id": "s", "coordinates": {
            "device": {"path": "dev.npy"}}}]
        variables = [{"name": "f", "source_id": "s", "path": "v.npy",
                      "mask_path": "v_mask.npy", "dimensions": ["device"]}]
        self.store = DescriptorBuilder(d, axes, sources, variables).open()

    def tearDown(self):
        self.tmp.cleanup()

    def test_masked_nan_is_not_an_error(self):
        cells = list(self.store.iter_cells(
            ["f"], selection={"device": ["D1"]}))
        self.assertEqual(cells[0]["variables"]["f"]["state"], "masked")

    def test_unmasked_nan_and_infinity_are_errors(self):
        with self.assertRaises(CellDataError) as caught:
            list(self.store.iter_cells(
                ["f"], selection={"device": ["D2"]}))  # D2 = inf
        self.assertIn("infinity", str(caught.exception))
        d = self.tmp.name
        save_npy(d, "v_nomask.npy",
                 np.array([np.nan, 1.0], dtype="<f8"))
        save_npy(d, "dev2.npy", np.array(["X1", "X2"], dtype="U8"))
        desc = {
            "format": "archindex-label-read-1",
            "axes": [{"name": "device", "type": "text", "set": "union"}],
            "sources": [{"source_id": "s2", "coordinates": {
                "device": {"path": "dev2.npy"}}}],
            "variables": [{"name": "g", "source_id": "s2",
                           "path": "v_nomask.npy",
                           "dimensions": ["device"]}],
        }
        store2 = open_descriptor(desc, base_dir=d)
        with self.assertRaises(CellDataError) as caught:
            list(store2.iter_cells(["g"]))
        self.assertIn("NaN", str(caught.exception))

    def test_signed_zeroes_compare_equal_without_tolerance(self):
        d = self.tmp.name
        save_npy(d, "dev_a.npy", np.array(["Z"], dtype="U8"))
        save_npy(d, "dev_b.npy", np.array(["Z"], dtype="U8"))
        save_npy(d, "za.npy", np.array([0.0], dtype="<f8"))
        save_npy(d, "zb.npy", np.array([-0.0], dtype="<f8"))
        desc = {
            "format": "archindex-label-read-1",
            "axes": [{"name": "device", "type": "text", "set": "union"}],
            "sources": [
                {"source_id": "a", "coordinates": {
                    "device": {"path": "dev_a.npy"}}},
                {"source_id": "b", "coordinates": {
                    "device": {"path": "dev_b.npy"}}},
            ],
            "variables": [
                {"name": "z", "source_id": "a", "path": "za.npy",
                 "dimensions": ["device"]},
                {"name": "z", "source_id": "b", "path": "zb.npy",
                 "dimensions": ["device"]},
            ],
        }
        store2 = open_descriptor(desc, base_dir=d)
        cells = list(store2.iter_cells(["z"]))
        self.assertEqual(cells[0]["variables"]["z"]["state"], "value")

    def test_int_and_float_never_equal_and_no_tolerance(self):
        d = self.tmp.name
        save_npy(d, "ia.npy", np.array([1], dtype="<i8"))
        save_npy(d, "fb.npy", np.array([1.0], dtype="<f8"))
        save_npy(d, "da.npy", np.array(["K"], dtype="U8"))
        save_npy(d, "db.npy", np.array(["K"], dtype="U8"))
        desc = {
            "format": "archindex-label-read-1",
            "axes": [{"name": "device", "type": "text", "set": "union"}],
            "sources": [
                {"source_id": "a", "coordinates": {
                    "device": {"path": "da.npy"}}},
                {"source_id": "b", "coordinates": {
                    "device": {"path": "db.npy"}}},
            ],
            "variables": [
                {"name": "x", "source_id": "a", "path": "ia.npy",
                 "dimensions": ["device"]},
                {"name": "x", "source_id": "b", "path": "fb.npy",
                 "dimensions": ["device"]},
            ],
        }
        with self.assertRaises(DescriptorError):
            open_descriptor(desc, base_dir=d)


class LargeIntegersAndDtypesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_integers_beyond_2_53_stay_exact(self):
        big = 2 ** 63 - 1
        small = -(2 ** 63)
        save_npy(self.d, "dev.npy",
                 np.array([big, small], dtype=">i8"))  # big endian int64
        save_npy(self.d, "v.npy",
                 np.array([big - 1, small + 1], dtype="<i8"))
        desc = {
            "format": "archindex-label-read-1",
            "axes": [{"name": "id", "type": "integer", "set": "union"}],
            "sources": [{"source_id": "s", "coordinates": {
                "id": {"path": "dev.npy"}}}],
            "variables": [{"name": "v", "source_id": "s",
                           "path": "v.npy", "dimensions": ["id"]}],
        }
        store = open_descriptor(desc, base_dir=self.d)
        cells, _ = store.read_cells(["v"])
        values = {c["coordinates"][0]["label"]:
                  c["variables"]["v"]["value"] for c in cells}
        self.assertEqual(values[big], big - 1)
        self.assertEqual(values[small], small + 1)
        # labels and values must be Python ints in JSON (never float)
        payload = json.dumps(cells)
        self.assertNotIn(str(float(big)), payload)
        self.assertIn(str(big), payload)

    def test_object_array_rejected(self):
        path = os.path.join(self.d, "obj.npy")
        with open(path, "wb") as handle:
            np.save(handle, np.array([object(), object()], dtype=object))
        save_npy(self.d, "lab.npy", np.array(["a"], dtype="U8"))
        desc = {
            "format": "archindex-label-read-1",
            "axes": [{"name": "d", "type": "text", "set": "union"}],
            "sources": [{"source_id": "s", "coordinates": {
                "d": {"path": "lab.npy"}}}],
            "variables": [{"name": "o", "source_id": "s", "path": "obj.npy",
                           "dimensions": ["d"]}],
        }
        with self.assertRaises(DescriptorError):
            open_descriptor(desc, base_dir=self.d)

    def test_boolean_coordinates_rejected(self):
        save_npy(self.d, "bool_lab.npy", np.array([True, False]))
        desc = {
            "format": "archindex-label-read-1",
            "axes": [{"name": "d", "type": "integer", "set": "union"}],
            "sources": [{"source_id": "s", "coordinates": {
                "d": {"path": "bool_lab.npy"}}}],
            "variables": [],
        }
        with self.assertRaises(DescriptorError):
            open_descriptor(desc, base_dir=self.d)

    def test_unsigned_integer_coordinates_rejected(self):
        save_npy(self.d, "u64_lab.npy",
                 np.array([1, 2], dtype=np.uint64))
        desc = {
            "format": "archindex-label-read-1",
            "axes": [{"name": "d", "type": "integer", "set": "union"}],
            "sources": [{"source_id": "s", "coordinates": {
                "d": {"path": "u64_lab.npy"}}}],
            "variables": [],
        }
        with self.assertRaises(DescriptorError) as caught:
            open_descriptor(desc, base_dir=self.d)
        self.assertIn("signed int64", str(caught.exception))

    def test_unsupported_value_dtype_rejected(self):
        save_npy(self.d, "lab.npy", np.array(["a", "b"], dtype="U8"))
        save_npy(self.d, "f32.npy", np.array([1.0, 2.0], dtype="<f4"))
        desc = {
            "format": "archindex-label-read-1",
            "axes": [{"name": "d", "type": "text", "set": "union"}],
            "sources": [{"source_id": "s", "coordinates": {
                "d": {"path": "lab.npy"}}}],
            "variables": [{"name": "f", "source_id": "s",
                           "path": "f32.npy", "dimensions": ["d"]}],
        }
        with self.assertRaises(DescriptorError):
            open_descriptor(desc, base_dir=self.d)


class StructuralValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        save_npy(d, "dev.npy", np.array(["D1", "D1"], dtype="U8"))  # dup!
        save_npy(d, "batch.npy", np.array([1, 2], dtype="<i8"))
        save_npy(d, "v2x2.npy", np.zeros((2, 2), dtype="<i8"))
        save_npy(d, "v2x3.npy", np.zeros((2, 3), dtype="<i8"))
        self.d = d

    def tearDown(self):
        self.tmp.cleanup()

    def _desc(self, **over):
        desc = {
            "format": "archindex-label-read-1",
            "axes": [
                {"name": "device", "type": "text", "set": "union"},
                {"name": "batch", "type": "integer", "set": "union"},
            ],
            "sources": [{"source_id": "s", "coordinates": {
                "device": {"path": "dev.npy"},
                "batch": {"path": "batch.npy"}}}],
            "variables": [{"name": "v", "source_id": "s",
                           "path": "v2x2.npy",
                           "dimensions": ["device", "batch"]}],
        }
        desc.update(over)
        return desc

    def test_duplicate_label_rejected(self):
        with self.assertRaises(DescriptorError) as caught:
            open_descriptor(self._desc(), base_dir=self.d)
        self.assertIn("repeats label", str(caught.exception))

    def test_bad_shape_rejected(self):
        save_npy(self.d, "dev_uniq.npy",
                 np.array(["D1", "D2"], dtype="U8"))
        desc = self._desc()
        desc["sources"][0]["coordinates"]["device"] = {"path": "dev_uniq.npy"}
        desc["variables"][0]["path"] = "v2x3.npy"
        with self.assertRaises(DescriptorError) as caught:
            open_descriptor(desc, base_dir=self.d)
        self.assertIn("length", str(caught.exception))

    def test_unknown_axis_rejected(self):
        with self.assertRaises(DescriptorError):
            open_descriptor(self._desc(axes=[
                {"name": "device", "type": "text", "set": "union"}]),
                base_dir=self.d)

    def test_repeated_dimension_name_rejected(self):
        save_npy(self.d, "dev_uniq.npy",
                 np.array(["D1", "D2"], dtype="U8"))
        desc = self._desc()
        desc["sources"][0]["coordinates"]["device"] = {"path": "dev_uniq.npy"}
        desc["variables"][0]["dimensions"] = ["device", "device"]
        with self.assertRaises(DescriptorError):
            open_descriptor(desc, base_dir=self.d)

    def test_dimension_set_conflict_rejected(self):
        save_npy(self.d, "dev_uniq.npy",
                 np.array(["D1", "D2"], dtype="U8"))
        save_npy(self.d, "stage.npy", np.array([1, 2], dtype="<i8"))
        save_npy(self.d, "w2x2.npy", np.zeros((2, 2), dtype="<i8"))
        desc = self._desc()
        desc["sources"][0]["coordinates"]["device"] = {"path": "dev_uniq.npy"}
        desc["axes"].append(
            {"name": "stage", "type": "integer", "set": "union"})
        desc["sources"][0]["coordinates"]["stage"] = {"path": "stage.npy"}
        # second slot of the same variable, same rank, different logical set
        desc["variables"].append(
            {"name": "v", "source_id": "s", "path": "w2x2.npy",
             "dimensions": ["device", "stage"]})
        with self.assertRaises(DescriptorError) as caught:
            open_descriptor(desc, base_dir=self.d)
        self.assertIn("dimension set", str(caught.exception))

    def test_source_missing_coordinate_for_dimension_rejected(self):
        save_npy(self.d, "dev_uniq.npy",
                 np.array(["D1", "D2"], dtype="U8"))
        desc = self._desc()
        desc["sources"][0]["coordinates"] = {"device": {"path": "dev_uniq.npy"}}
        with self.assertRaises(DescriptorError):
            open_descriptor(desc, base_dir=self.d)

    def test_inline_coordinates_supported(self):
        desc = self._desc()
        desc["sources"][0]["coordinates"] = {
            "device": {"values": ["A", "B"]},
            "batch": {"values": [1, 2]},
        }
        store = open_descriptor(desc, base_dir=self.d)
        self.assertEqual(store.visible_labels("device"), ["A", "B"])

    def test_missing_file_rejected(self):
        desc = self._desc()
        desc["variables"][0]["path"] = "missing.npy"
        with self.assertRaises(DescriptorError):
            open_descriptor(desc, base_dir=self.d)

    def test_mask_wrong_shape_and_type_rejected(self):
        save_npy(self.d, "dev_uniq.npy",
                 np.array(["D1", "D2"], dtype="U8"))
        save_npy(self.d, "mask_bad_shape.npy",
                 np.ones((2, 3), dtype=np.bool_))
        save_npy(self.d, "mask_bad_type.npy",
                 np.ones((2, 2), dtype=np.uint8))
        base = self._desc()
        base["sources"][0]["coordinates"]["device"] = {"path": "dev_uniq.npy"}
        bad_shape = json.loads(json.dumps(base))
        bad_shape["variables"][0]["mask_path"] = "mask_bad_shape.npy"
        with self.assertRaises(DescriptorError) as caught:
            open_descriptor(bad_shape, base_dir=self.d)
        self.assertIn("does not match", str(caught.exception))
        bad_type = json.loads(json.dumps(base))
        bad_type["variables"][0]["mask_path"] = "mask_bad_type.npy"
        with self.assertRaises(DescriptorError) as caught:
            open_descriptor(bad_type, base_dir=self.d)
        self.assertIn("boolean", str(caught.exception))


class ThreeAxisTest(unittest.TestCase):
    """A device x batch x stage example with variables of differing ranks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        save_npy(d, "dev.npy", np.array(["D1", "D2"], dtype="U8"))
        save_npy(d, "batch.npy", np.array([10, 20], dtype="<i8"))
        save_npy(d, "stage.npy", np.array([1, 2], dtype="<i8"))
        # full 3-D value: value[device, batch, stage] = deterministic int
        full = np.zeros((2, 2, 2), dtype="<i8")
        for i in range(2):
            for j in range(2):
                for k in range(2):
                    full[i, j, k] = (i + 1) * 100 + (10 if j == 0 else 20) + k
        save_npy(d, "full.npy", full)
        # physically transposed copy in a second source, same logical values
        full_t = np.transpose(full, (2, 0, 1)).copy()  # stage x device x batch
        save_npy(d, "full_t.npy", full_t)
        # device x stage matrix (no batch)
        ds = np.array([[11, 12], [21, 22]], dtype="<i8")
        save_npy(d, "devstage.npy", ds)
        # device-only vector
        save_npy(d, "devonly.npy", np.array([7, 9], dtype="<i8"))
        axes = [
            {"name": "device", "type": "text", "set": "union"},
            {"name": "batch", "type": "integer", "set": "union"},
            {"name": "stage", "type": "integer", "set": "union"},
        ]
        sources = [{"source_id": "s", "coordinates": {
            "device": {"path": "dev.npy"},
            "batch": {"path": "batch.npy"},
            "stage": {"path": "stage.npy"}}}]
        variables = [
            {"name": "full", "source_id": "s", "path": "full.npy",
             "dimensions": ["device", "batch", "stage"]},
            {"name": "full_t", "source_id": "s", "path": "full_t.npy",
             "dimensions": ["stage", "device", "batch"]},
            {"name": "devstage", "source_id": "s", "path": "devstage.npy",
             "dimensions": ["device", "stage"]},
            {"name": "devonly", "source_id": "s", "path": "devonly.npy",
             "dimensions": ["device"]},
        ]
        self.store = DescriptorBuilder(d, axes, sources, variables).open()

    def tearDown(self):
        self.tmp.cleanup()

    def test_three_axis_grid_and_rank_broadcast(self):
        cells, summary = self.store.read_cells(
            ["full", "full_t", "devstage", "devonly"])
        self.assertEqual(summary["cells"], 2 * 2 * 2)
        for cell in cells:
            coord = {x["axis"]: x["label"] for x in cell["coordinates"]}
            i = {"D1": 0, "D2": 1}[coord["device"]]
            j = {10: 0, 20: 1}[coord["batch"]]
            k = {1: 0, 2: 1}[coord["stage"]]
            results = cell["variables"]
            expected_full = (i + 1) * 100 + (10 if j == 0 else 20) + k
            self.assertEqual(results["full"]["value"], expected_full)
            # transposed source must land in the same cell
            self.assertEqual(results["full_t"]["value"], expected_full)
            self.assertEqual(results["full"]["evidence"][0]["indices"],
                             [i, j, k])
            self.assertEqual(results["full_t"]["evidence"][0]["indices"],
                             [k, i, j])
            # devstage broadcasts over batch
            self.assertEqual(results["devstage"]["value"],
                             [[11, 12], [21, 22]][i][k])
            # devonly broadcasts over batch and stage
            self.assertEqual(results["devonly"]["value"], [7, 9][i])

    def test_ordering_last_axis_fastest_in_3d(self):
        cells = list(self.store.iter_cells(["devonly"]))
        keys = [tuple(x["label"] for x in c["coordinates"]) for c in cells]
        self.assertEqual(keys[0], ("D1", 10, 1))
        self.assertEqual(keys[1], ("D1", 10, 2))
        self.assertEqual(keys[2], ("D1", 20, 1))
        self.assertEqual(keys[-1], ("D2", 20, 2))


class ProjectionLazinessTest(unittest.TestCase):
    """Unrequested variables must not have their payloads touched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        save_npy(d, "dev.npy", np.array(["D1"], dtype="U8"))
        save_npy(d, "good.npy", np.array([1], dtype="<i8"))
        save_npy(d, "other.npy", np.array([2], dtype="<i8"))
        desc = {
            "format": "archindex-label-read-1",
            "axes": [{"name": "device", "type": "text", "set": "union"}],
            "sources": [{"source_id": "s", "coordinates": {
                "device": {"path": "dev.npy"}}}],
            "variables": [
                {"name": "good", "source_id": "s", "path": "good.npy",
                 "dimensions": ["device"]},
                {"name": "other", "source_id": "s", "path": "other.npy",
                 "dimensions": ["device"]},
            ],
        }
        self.store = open_descriptor(desc, base_dir=d)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_requested_variable_appears(self):
        cells = list(self.store.iter_cells(["good"]))
        self.assertEqual(set(cells[0]["variables"]), {"good"})

    def test_close_releases_memmaps_and_is_idempotent(self):
        slot = self.store.variables["good"].slots[0]
        self.assertIsNotNone(slot.array._mmap)
        self.store.close()
        self.assertTrue(self.store._closed)
        self.store.close()  # idempotent
        self.assertTrue(slot.array._mmap.closed)

    def test_context_manager_closes_on_exit(self):
        desc = {
            "format": "archindex-label-read-1",
            "axes": [{"name": "device", "type": "text", "set": "union"}],
            "sources": [{"source_id": "s", "coordinates": {
                "device": {"path": "dev.npy"}}}],
            "variables": [{"name": "good", "source_id": "s",
                           "path": "good.npy", "dimensions": ["device"]}],
        }
        with open_descriptor(desc, base_dir=self.tmp.name) as managed:
            slot = managed.variables["good"].slots[0]
        self.assertTrue(slot.array._mmap.closed)


class PathResolutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _descriptor(self, value_path):
        save_npy(self.d, "dev.npy", np.array(["D1"], dtype="U8"))
        return {
            "format": "archindex-label-read-1",
            "axes": [{"name": "device", "type": "text", "set": "union"}],
            "sources": [{"source_id": "s", "coordinates": {
                "device": {"path": "dev.npy"}}}],
            "variables": [{"name": "v", "source_id": "s",
                           "path": value_path, "dimensions": ["device"]}],
        }

    def test_relative_and_absolute_paths(self):
        save_npy(self.d, "v.npy", np.array([7], dtype="<i8"))
        store_rel = open_descriptor(self._descriptor("v.npy"),
                                    base_dir=self.d)
        self.assertEqual(
            list(store_rel.iter_cells(["v"]))[0]["variables"]["v"]["value"],
            7)
        store_abs = open_descriptor(
            self._descriptor(os.path.join(self.d, "v.npy")), base_dir=self.d)
        self.assertEqual(
            list(store_abs.iter_cells(["v"]))[0]["variables"]["v"]["value"],
            7)

    def test_bad_json_descriptor_rejected(self):
        path = os.path.join(self.d, "bad.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(DescriptorError):
            load_descriptor(path)


class PartialCoordinateCoverageTest(unittest.TestCase):
    """Two sources covering half the coordinates each cannot form a cell."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        # source p knows device D1/D2 and batch 10 only
        save_npy(d, "p_dev.npy", np.array(["D1", "D2"], dtype="U8"))
        save_npy(d, "p_batch.npy", np.array([10], dtype="<i8"))
        save_npy(d, "p_v.npy", np.array([[110], [210]], dtype="<i8"))
        # source q knows device D2 only and batches 10/20
        save_npy(d, "q_dev.npy", np.array(["D2"], dtype="U8"))
        save_npy(d, "q_batch.npy", np.array([10, 20], dtype="<i8"))
        save_npy(d, "q_v.npy", np.array([[210, 220]], dtype="<i8"))
        axes = [
            {"name": "device", "type": "text", "set": "union"},
            {"name": "batch", "type": "integer", "set": "union"},
        ]
        sources = [
            {"source_id": "p", "coordinates": {
                "device": {"path": "p_dev.npy"},
                "batch": {"path": "p_batch.npy"}}},
            {"source_id": "q", "coordinates": {
                "device": {"path": "q_dev.npy"},
                "batch": {"path": "q_batch.npy"}}},
        ]
        variables = [
            {"name": "v", "source_id": "p", "path": "p_v.npy",
             "dimensions": ["device", "batch"]},
            {"name": "v", "source_id": "q", "path": "q_v.npy",
             "dimensions": ["device", "batch"]},
        ]
        self.store = DescriptorBuilder(d, axes, sources, variables).open()

    def tearDown(self):
        self.tmp.cleanup()

    def test_holes_are_absent_not_stitched_from_two_sources(self):
        cells, summary = self.store.read_cells(["v"])
        grid = {tuple(c["coordinates"][i]["label"]
                      for i in range(2)): c for c in cells}
        # (D1, 20): p lacks batch 20, q lacks device D1 -> absent, not stitched
        self.assertEqual(grid[("D1", 20)]["variables"]["v"]["state"],
                         "absent")
        # (D2, 10): both locate the full coordinate -> value with two proofs
        overlap = grid[("D2", 10)]["variables"]["v"]
        self.assertEqual(overlap["state"], "value")
        self.assertEqual(
            sorted(e["source_id"] for e in overlap["evidence"]),
            ["p", "q"])
        # union grid is devices {D1,D2} x batches {10,20} = 4 cells; only
        # (D1,20) has no source locating BOTH coordinates
        self.assertEqual(summary["cells"], 4)
        self.assertEqual(summary["variables"]["v"],
                         {"value": 3, "masked": 0, "absent": 1})
        absent = {tuple(c["coordinates"][i]["label"] for i in range(2))
                  for c in cells
                  if c["variables"]["v"]["state"] == "absent"}
        self.assertEqual(absent, {("D1", 20)})


class CommandLineStreamingTest(unittest.TestCase):
    """The label-read command's success/failure stream contract."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        # D0 exists only in source a (emits fine first); D1 conflicts
        save_npy(d, "a_dev.npy", np.array(["D0", "D1"], dtype="U8"))
        save_npy(d, "b_dev.npy", np.array(["D1"], dtype="U8"))
        save_npy(d, "a_v.npy", np.array([10, 1], dtype="<i8"))
        save_npy(d, "b_v.npy", np.array([2], dtype="<i8"))
        desc = {
            "format": "archindex-label-read-1",
            "axes": [{"name": "device", "type": "text", "set": "union"}],
            "sources": [
                {"source_id": "a", "coordinates": {
                    "device": {"path": "a_dev.npy"}}},
                {"source_id": "b", "coordinates": {
                    "device": {"path": "b_dev.npy"}}},
            ],
            "variables": [
                {"name": "v", "source_id": "a", "path": "a_v.npy",
                 "dimensions": ["device"]},
                {"name": "v", "source_id": "b", "path": "b_v.npy",
                 "dimensions": ["device"]},
            ],
        }
        self.desc_path = os.path.join(d, "desc.json")
        with open(self.desc_path, "w", encoding="utf-8") as handle:
            json.dump(desc, handle)
        self.d = d
        self.main_py = os.path.join(ROOT, "archindex", "__main__.py")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, request):
        import subprocess
        req_path = os.path.join(self.d, "request.json")
        with open(req_path, "w", encoding="utf-8") as handle:
            json.dump(request, handle)
        return subprocess.run(
            [sys.executable, "-I", self.main_py, "label-read",
             self.desc_path, "--request", req_path],
            capture_output=True, text=True, cwd=ROOT)

    def test_conflict_midstream_fails_nonzero_without_success_marker(self):
        proc = self._run({"variables": ["v"]})
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn('"status": "ok"', proc.stdout)
        self.assertIn("strict conflict", proc.stderr)
        # a partial stream is allowed before the failing cell, but every
        # emitted line must remain valid JSONL; the summary never appears
        lines = proc.stdout.splitlines()
        self.assertTrue(lines, "expected at least one cell before conflict")
        for line in lines:
            parsed = json.loads(line)
            self.assertIn("coordinates", parsed)
        self.assertFalse(any("summary" in line for line in lines))

    def test_bad_request_emits_nothing_and_fails(self):
        proc = self._run({"variables": ["v"],
                          "selection": {"device": ["Nope"]}})
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertNotIn('"status": "ok"', proc.stdout)

    def test_empty_grid_streams_summary_successfully(self):
        # Rebuild descriptors whose intersection is empty so the CLI must
        # emit zero cells but still finish successfully with a summary.
        save_npy(self.d, "c_dev.npy", np.array(["ZZ"], dtype="U8"))
        save_npy(self.d, "c_v.npy", np.array([9], dtype="<i8"))
        desc = {
            "format": "archindex-label-read-1",
            "axes": [{"name": "device", "type": "text",
                      "set": "intersection"}],
            "sources": [
                {"source_id": "a", "coordinates": {
                    "device": {"path": "a_dev.npy"}}},
                {"source_id": "c", "coordinates": {
                    "device": {"path": "c_dev.npy"}}},
            ],
            "variables": [
                {"name": "v", "source_id": "a", "path": "a_v.npy",
                 "dimensions": ["device"]},
                {"name": "v", "source_id": "c", "path": "c_v.npy",
                 "dimensions": ["device"]},
            ],
        }
        desc_path = os.path.join(self.d, "empty_desc.json")
        with open(desc_path, "w", encoding="utf-8") as handle:
            json.dump(desc, handle)
        req_path = os.path.join(self.d, "empty_req.json")
        with open(req_path, "w", encoding="utf-8") as handle:
            json.dump({"variables": ["v"]}, handle)
        import subprocess
        proc = subprocess.run(
            [sys.executable, "-I", self.main_py, "label-read",
             desc_path, "--request", req_path],
            capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["summary"]["cells"], 0)
        self.assertEqual(payload[0]["summary"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
