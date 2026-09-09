"""Test bootstrap for the server package.

Two things the tests need that a plain checkout does not provide:

* ``sofabaton`` importable: in this repository the library's source is
  ``custom_components/sofabaton_x1s/lib`` and only becomes the
  ``sofabaton`` package at wheel-build time. Load it under that name
  here (the same alias shim the library's own tests use), unless a real
  ``sofabaton`` install is already present.
* ``sofabaton_server`` importable from ``src/`` without an install.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER_SRC = HERE.parent / "src"
REPO = HERE.parents[1]
LIB_DIR = REPO / "custom_components" / "sofabaton_x1s" / "lib"


def _alias_library() -> None:
    if "sofabaton" in sys.modules:
        return
    # Prefer the in-tree library so a change there is tested here at once;
    # set SOFABATON_SERVER_TESTS_USE_INSTALLED=1 to run against an installed
    # sofabaton-x instead (what CI does after the wheel smoke).
    if os.environ.get("SOFABATON_SERVER_TESTS_USE_INSTALLED") == "1":
        if importlib.util.find_spec("sofabaton") is not None:
            return
    spec = importlib.util.spec_from_file_location(
        "sofabaton", LIB_DIR / "__init__.py", submodule_search_locations=[str(LIB_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["sofabaton"] = module
    spec.loader.exec_module(module)


_alias_library()
if str(SERVER_SRC) not in sys.path:
    sys.path.insert(0, str(SERVER_SRC))
