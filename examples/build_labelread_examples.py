#!/usr/bin/env python3
"""Build the hand-computable label-coordinate example.

Layout (all arrays are read-only ``.npy`` files)::

    data/
      handcalc/                 # the worked two-axis example
        jia_device.npy  [D2, D1]
        jia_batch.npy   [20, 10]
        jia_value.npy   device x batch, int64, [[220, 210], [120, 110]]
        yi_device.npy   [D3, D2]
        yi_batch.npy    [10, 30]
        yi_value.npy    batch x device, int64, [[310, 210], [330, 230]]
        bing_device.npy [D3]
        bing_batch.npy  [20]
        bing_value.npy  [[320]]           # physical layout device x batch
        bing_value_mask.npy [[False]]     # forces (D3, 20) to be masked
        cal_jia.npy     [2, 1]           # device-only calibration, broadcasts
        cal_yi.npy      [1, 2]           # batch x device physical, same logic
      handcalc_desc.json                  # descriptor referencing the files

The third source ``bing`` exists so that (D3, 20) is *located but fully
masked* -- without it the cell would be ``absent``, which is a different
state.  Nothing here is ever converted to a relational table or merged into
one aligned array.
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from archindex._vendor import ensure_numpy  # noqa: E402

np = ensure_numpy()


def save(path, array):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        np.save(handle, array)


def build_handcalc(base: str) -> str:
    data_dir = os.path.join(base, "handcalc")
    os.makedirs(data_dir, exist_ok=True)

    # --- source jia: device x batch, coordinate order deliberately shuffled
    save(f"{data_dir}/jia_device.npy", np.array(["D2", "D1"], dtype="U8"))
    save(f"{data_dir}/jia_batch.npy", np.array([20, 10], dtype="<i8"))
    jia_value = np.array([[220, 210],
                          [120, 110]], dtype="<i8")
    save(f"{data_dir}/jia_value.npy", jia_value)

    # --- source yi: SAME logical data but physically batch x device,
    #     different devices/batches and its own local order
    save(f"{data_dir}/yi_device.npy", np.array(["D3", "D2"], dtype="U8"))
    save(f"{data_dir}/yi_batch.npy", np.array([10, 30], dtype="<i8"))
    yi_value = np.array([[310, 210],   # batch 10: D3 -> 310, D2 -> 210
                         [330, 230]],  # batch 30: D3 -> 330, D2 -> 230
                        dtype="<i8")
    save(f"{data_dir}/yi_value.npy", yi_value)

    # --- source bing: one cell (D3, 20), present but masked false.  Its
    #     underlying 320 must never be read as a business value.
    save(f"{data_dir}/bing_device.npy", np.array(["D3"], dtype="U8"))
    save(f"{data_dir}/bing_batch.npy", np.array([20], dtype="<i8"))
    save(f"{data_dir}/bing_value.npy", np.array([[320]], dtype="<i8"))
    save(f"{data_dir}/bing_value_mask.npy",
         np.array([[False]], dtype=np.bool_))

    # --- device-only calibration variable, two sources.  Both declare the
    #     same logical dimension set {device}; each array is 1-D and aligned
    #     to that source's own device coordinate order.  When read together
    #     with `value` (device x batch), calibration broadcasts along batch
    #     by its own dimension declaration.
    save(f"{data_dir}/cal_jia.npy",
         np.array([2002, 1001], dtype="<i8"))          # aligned to [D2, D1]
    save(f"{data_dir}/cal_yi.npy",
         np.array([3003, 2002], dtype="<i8"))           # aligned to [D3, D2]

    rel = lambda leaf: f"handcalc/{leaf}"  # paths relative to descriptor dir
    descriptor = {
        "format": "archindex-label-read-1",
        "axes": [
            {"name": "device", "type": "text", "set": "union"},
            {"name": "batch", "type": "integer", "set": "union"},
        ],
        "sources": [
            {
                "source_id": "jia",
                "coordinates": {
                    "device": {"path": rel("jia_device.npy")},
                    "batch": {"path": rel("jia_batch.npy")},
                },
            },
            {
                "source_id": "yi",
                "coordinates": {
                    "device": {"path": rel("yi_device.npy")},
                    "batch": {"path": rel("yi_batch.npy")},
                },
            },
            {
                "source_id": "bing",
                "coordinates": {
                    "device": {"path": rel("bing_device.npy")},
                    "batch": {"path": rel("bing_batch.npy")},
                },
            },
        ],
        "variables": [
            {
                "name": "value",
                "source_id": "jia",
                "path": rel("jia_value.npy"),
                "dimensions": ["device", "batch"],
            },
            {
                "name": "value",
                "source_id": "yi",
                "path": rel("yi_value.npy"),
                # physical order swapped relative to jia on purpose
                "dimensions": ["batch", "device"],
            },
            {
                "name": "value",
                "source_id": "bing",
                "path": rel("bing_value.npy"),
                "mask_path": rel("bing_value_mask.npy"),
                "dimensions": ["device", "batch"],
            },
            {
                "name": "calibration",
                "source_id": "jia",
                "path": rel("cal_jia.npy"),
                "dimensions": ["device"],
            },
            {
                "name": "calibration",
                "source_id": "yi",
                "path": rel("cal_yi.npy"),
                "dimensions": ["device"],
            },
        ],
    }
    desc_path = os.path.join(base, "handcalc_desc.json")
    with open(desc_path, "w", encoding="utf-8") as handle:
        json.dump(descriptor, handle, ensure_ascii=False, indent=2)
    return desc_path


def main() -> None:
    base = os.path.join(ROOT, "examples", "data")
    path = build_handcalc(base)
    print("wrote", path)


if __name__ == "__main__":
    main()
