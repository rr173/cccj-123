#!/usr/bin/env python3
"""Test runner usable under isolated mode (``python3 -I run_tests.py``).

``-I``/``-P`` keep the working directory off ``sys.path``; the runner locates
the project via its own absolute ``__file__`` instead.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.discover(
        start_dir=os.path.join(ROOT, "tests"),
        top_level_dir=ROOT,
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
