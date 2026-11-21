"""Loaded at every interpreter start (incl. subprocess / spawn children) via PYTHONPATH.
Serves patched file contents for imports and open() reads, from the job file in $BR_OVERLAY."""
import os

_jp = os.environ.get("BR_OVERLAY")
if _jp:
    import builtins
    import io
    import json
    import sys

    sys.dont_write_bytecode = True
    _files = json.load(open(_jp))
    _py = {p: t.encode("utf-8") for p, t in _files.items() if p.endswith(".py")}
    import importlib._bootstrap_external as _be

    _orig_get = _be.FileLoader.get_data

    def _get_data(self, path):
        b = _py.get(path)
        return b if b is not None else _orig_get(self, path)

    _be.FileLoader.get_data = _get_data

    _orig_open = builtins.open

    def _open(file, mode="r", *a, **k):
        if isinstance(file, (str, os.PathLike)) and not any(c in mode for c in "wax+"):
            t = _files.get(os.path.abspath(os.fspath(file)))
            if t is not None:
                return io.BytesIO(t.encode("utf-8")) if "b" in mode else io.StringIO(t)
        return _orig_open(file, mode, *a, **k)

    builtins.open = _open
    io.open = _open
