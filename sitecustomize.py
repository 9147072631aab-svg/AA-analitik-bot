"""Auto-install the scan scheduler without modifying the existing bot.py.

Python imports sitecustomize during interpreter startup. We install a tiny
import hook so that once the project's bot module is loaded, the scheduler
wraps it automatically.
"""
import builtins

_original_import = builtins.__import__
_installed = False

def _install(module):
    global _installed
    if _installed:
        return
    try:
        from scan_scheduler import install
        install(module)
        _installed = True
    except Exception as exc:
        print(f"AUTO SCAN SCHEDULER INSTALL ERROR: {type(exc).__name__}: {exc}", flush=True)

def _import(name, globals=None, locals=None, fromlist=(), level=0):
    module = _original_import(name, globals, locals, fromlist, level)
    if not _installed and name == "bot":
        _install(module)
    return module

builtins.__import__ = _import
