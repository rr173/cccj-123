"""Dependency bootstrap for the optional NumPy-based label-reading module.

The project itself only uses the Python standard library, but label-coordinate
matrix reading requires NumPy.  In this environment NumPy is vendored under
``<repo>/vendor`` (a plain directory next to the package, never on the global
search path).  Running under ``python -I`` keeps user site-packages out of
``sys.path``, so we add the vendored location explicitly *before* importing
NumPy.  A system-installed NumPy always wins because it is already
importable; nothing here shadows it.
"""

from __future__ import annotations

import os
import sys

_VENDOR_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")


def ensure_numpy():
    """Return the NumPy module, adding the vendored copy when needed."""
    try:
        import numpy as np  # noqa: PLC0415 - import after path bootstrap
    except ModuleNotFoundError:
        if os.path.isdir(_VENDOR_DIR) and _VENDOR_DIR not in sys.path:
            sys.path.insert(0, _VENDOR_DIR)
        import numpy as np  # noqa: PLC0415
    return np
