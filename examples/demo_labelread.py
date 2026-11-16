#!/usr/bin/env python3
"""Hand-computed verification of the label-coordinate joint reader.

Every expected cell below is written out explicitly with its logical
coordinate, state, value and the *physical* source positions that prove it.
The reader's output is then compared cell by cell.  Run with::

    python3 -I examples/demo_labelread.py            # full demo incl. 1 GiB
    python3 -I examples/demo_labelread.py --no-big   # skip the large files

The expected grid for the two-axis hand example (axes device then batch,
batch fastest) is::

    device labels (text, code-point order): D1, D2, D3
    batch  labels (integer order):          10, 20, 30

    value:
      (D1,10)=110  jia[device D1->1, batch 10->1]   indices (1,1)
      (D1,20)=120  jia[1,0]
      (D1,30)=absent
      (D2,10)=210  jia[0,1] AND yi[batch 10->0, device D2->1] (0,1)
      (D2,20)=220  jia[0,0]
      (D2,30)=230  yi[1,1]
      (D3,10)=310  yi[0,0]
      (D3,20)=masked  bing[0,0] located but mask False (underlying 320
                   is never read); NOT absent
      (D3,30)=330  yi[1,0]
    calibration (device only, broadcasts over batch):
      D1 -> 1001 (jia), D2 -> 2002 (jia and yi, equal), D3 -> 3003 (yi)
"""

from __future__ import annotations

import json
import os
import resource
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from archindex._vendor import ensure_numpy  # noqa: E402

np = ensure_numpy()

from archindex import (  # noqa: E402
    CellDataError,
    DescriptorError,
    ReadRequestError,
    load_descriptor,
    open_descriptor,
    summarize,
)

DATA_DIR = os.path.join(ROOT, "examples", "data")
HAND_DESC = os.path.join(DATA_DIR, "handcalc_desc.json")


def _states(cells, variable):
    return {tuple(c["coordinates"][i]["label"] for i in range(len(c["coordinates"]))):
            c["variables"][variable]
            for c in cells}


def check_handcalc() -> None:
    print("== hand-computed two-axis example ==")
    store = load_descriptor(HAND_DESC)
    inspection = store.inspect()
    assert [a["name"] for a in inspection["axes"]] == ["device", "batch"]
    assert inspection["axes"][0]["visible_labels"] == ["D1", "D2", "D3"]
    assert inspection["axes"][1]["visible_labels"] == [10, 20, 30]
    assert store.variables["value"].dimensions == ("device", "batch")
    assert store.variables["calibration"].dimensions == ("device",)

    cells, summary = store.read_cells(["value", "calibration"])
    assert summary["cells"] == 9, summary
    values = _states(cells, "value")
    cals = _states(cells, "calibration")

    expected_value = {
        ("D1", 10): ("value", 110, {"jia": (1, 1)}),
        ("D1", 20): ("value", 120, {"jia": (1, 0)}),
        ("D1", 30): ("absent", None, {}),
        ("D2", 10): ("value", 210, {"jia": (0, 1), "yi": (0, 1)}),
        ("D2", 20): ("value", 220, {"jia": (0, 0)}),
        ("D2", 30): ("value", 230, {"yi": (1, 1)}),
        ("D3", 10): ("value", 310, {"yi": (0, 0)}),
        ("D3", 20): ("masked", None, {"bing": (0, 0)}),
        ("D3", 30): ("value", 330, {"yi": (1, 0)}),
    }
    for key, (state, value, positions) in expected_value.items():
        got = values[key]
        assert got["state"] == state, (key, got)
        if state == "value":
            assert got["value"] == value and isinstance(got["value"], int), (key, got)
        observed = {ev["source_id"]: tuple(ev["indices"])
                    for ev in got["evidence"]}
        assert observed == positions, (key, observed, positions)
        # every evidence entry names its file, physical axis order and indices
        for ev in got["evidence"]:
            assert ev["file"].endswith(".npy")
            assert len(ev["physical_axes"]) == len(ev["indices"])

    # masked cell: underlying 320 must not appear anywhere in the output
    masked = values[("D3", 20)]
    assert all("value" not in ev for ev in masked["evidence"])
    assert json.dumps(masked).find("320") == -1

    expected_cal = {
        ("D1", 10): 1001, ("D1", 20): 1001, ("D1", 30): 1001,
        ("D2", 10): 2002, ("D2", 20): 2002, ("D2", 30): 2002,
        ("D3", 10): 3003, ("D3", 20): 3003, ("D3", 30): 3003,
    }
    for key, value in expected_cal.items():
        got = cals[key]
        assert got["state"] == "value", (key, got)
        assert got["value"] == value, (key, got)
    # D2 calibration is corroborated by both sources -> two evidence entries
    assert len(cals[("D2", 20)]["evidence"]) == 2
    # broadcast: the same physical position backs several grid cells
    d1_evidence = {tuple(ev["indices"]) for ev in cals[("D1", 10)]["evidence"]}
    d1_evidence_2 = {tuple(ev["indices"]) for ev in cals[("D1", 30)]["evidence"]}
    assert d1_evidence == d1_evidence_2 == {(1,)}

    assert summary["variables"]["value"] == {"value": 7, "masked": 1, "absent": 1}
    assert summary["variables"]["calibration"] == {"value": 9, "masked": 0,
                                                   "absent": 0}
    print("  all 9 grid cells match the hand-written table and provenance")
    print("  summary:", json.dumps(summary, ensure_ascii=False))


def check_explicit_and_batches() -> None:
    print("== explicit out-of-order selection, batching and streaming ==")
    store = load_descriptor(HAND_DESC)
    selection = {"batch": [30, 10], "device": ["D3", "D1"]}
    seen = []
    for batch in store.iter_batches(["value"], selection, batch_size=2):
        assert len(batch) <= 2
        seen.extend(batch)
    keys = [tuple(x["label"] for x in c["coordinates"]) for c in seen]
    # global axis order device then batch, batch fastest; request order kept
    assert keys == [("D3", 30), ("D3", 10), ("D1", 30), ("D1", 10)], keys
    states = _states(seen, "value")
    assert states[("D3", 30)]["value"] == 330
    assert states[("D1", 30)]["state"] == "absent"

    # request errors are refused before the first cell
    for bad in (
        {"batch": [10, 10]},                       # repeated label
        {"batch": [99]},                          # not visible
        {"device": ["Nope"]},                    # unknown label
    ):
        try:
            list(store.iter_cells(["value"], bad))
        except ReadRequestError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"expected refusal for {bad}")
    try:
        list(store.iter_cells(["nope"]))
    except ReadRequestError:
        pass
    else:  # pragma: no cover
        raise AssertionError("unknown variable was accepted")
    print("  request order preserved; duplicates/unknown labels refused early")


def main() -> int:
    check_handcalc()
    check_explicit_and_batches()
    print("hand example OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
