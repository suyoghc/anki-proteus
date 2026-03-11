"""
Test infrastructure: stub aqt and pre-import the addon as a proper package.

Pytest discovers __init__.py in the root directory and tries to import it.
Since aqt is unavailable outside Anki and relative imports require package
context, we set up both here before collection begins.
"""

import importlib.util
import os
import sys
import types

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# 1. Stub aqt modules (only when running outside Anki)
# ---------------------------------------------------------------------------
if "aqt" not in sys.modules:
    _aqt = types.ModuleType("aqt")
    _aqt.mw = None

    _hooks = types.ModuleType("aqt.gui_hooks")
    _hook_cls = type("_Hook", (), {"append": lambda self, fn: None})
    for _name in (
        "profile_did_open", "card_will_show",
        "reviewer_did_show_question", "reviewer_did_show_answer",
        "webview_did_receive_js_message", "state_did_change",
    ):
        setattr(_hooks, _name, _hook_cls())
    _aqt.gui_hooks = _hooks

    _utils = types.ModuleType("aqt.utils")
    _utils.showInfo = lambda *a, **kw: None
    _utils.tooltip = lambda *a, **kw: None

    _qt = types.ModuleType("aqt.qt")
    _qt.QAction = type("QAction", (), {"__init__": lambda *a, **kw: None})
    _qt.QThread = type("QThread", (), {"__init__": lambda *a, **kw: None})
    _qt.pyqtSignal = lambda *a, **kw: lambda *a2, **kw2: None
    _qt.QObject = type("QObject", (), {})
    _qt.QTimer = type("QTimer", (), {
        "singleShot": staticmethod(lambda *a, **kw: None),
    })

    _webview = types.ModuleType("aqt.webview")
    _webview.AnkiWebView = type("AnkiWebView", (), {})

    sys.modules["aqt"] = _aqt
    sys.modules["aqt.gui_hooks"] = _hooks
    sys.modules["aqt.utils"] = _utils
    sys.modules["aqt.qt"] = _qt
    sys.modules["aqt.webview"] = _webview

# ---------------------------------------------------------------------------
# 2. Pre-import addon submodules under the package name "__init__"
#    so that relative imports (from .generator import ...) resolve.
# ---------------------------------------------------------------------------
_PKG = "__init__"  # name pytest will use when importing __init__.py

if _PKG not in sys.modules:
    # Import submodules first (dependency order)
    for _sub in ("generator", "cache", "prefetch", "batch_prefetch"):
        _fqn = f"{_PKG}.{_sub}"
        _sub_spec = importlib.util.spec_from_file_location(
            _fqn, os.path.join(ADDON_DIR, f"{_sub}.py"),
        )
        _sub_mod = importlib.util.module_from_spec(_sub_spec)
        _sub_mod.__package__ = _PKG
        sys.modules[_fqn] = _sub_mod
        # Also register as top-level so `import generator` works in tests
        sys.modules.setdefault(_sub, _sub_mod)

    # Execute submodules (order matters: generator, cache before prefetch/batch)
    for _sub in ("generator", "cache", "prefetch", "batch_prefetch"):
        _fqn = f"{_PKG}.{_sub}"
        sys.modules[_fqn].__spec__.loader.exec_module(sys.modules[_fqn])

    # Now create and execute the package itself
    _pkg_spec = importlib.util.spec_from_file_location(
        _PKG,
        os.path.join(ADDON_DIR, "__init__.py"),
        submodule_search_locations=[ADDON_DIR],
    )
    _pkg_mod = importlib.util.module_from_spec(_pkg_spec)
    _pkg_mod.__package__ = _PKG
    sys.modules[_PKG] = _pkg_mod
    _pkg_spec.loader.exec_module(_pkg_mod)
