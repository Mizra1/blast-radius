"""pytest plugin used only while building the map: records fixture/file opens per test."""
import builtins, importlib, io, json, os, pkgutil
import pytest

ROOT = os.environ.get("MAP_ROOT", "/opt/sqlglot")
OUT = os.environ.get("MAPTRACE_OUT", "/tmp/maptrace.json")
state = {"cur": None, "file": None}
opens = {}
failed = set()
def _stub(name):
    import types
    m = types.ModuleType(name)

    def __getattr__(attr):
        if attr.startswith("__"):
            raise AttributeError(attr)

        def blocked(*a, **k):
            raise RuntimeError(f"{name} is stubbed out during the map build")

        return blocked

    m.__getattr__ = __getattr__
    return m


for _m in filter(None, os.environ.get("BLOCK_MODULES", "").split(",")):
    import sys
    sys.modules[_m] = _stub(_m)  # imports succeed; any use raises
_orig = builtins.open


def _rec(path):
    try:
        p = os.path.abspath(os.fsdecode(path))
    except Exception:
        return
    if not p.startswith(ROOT + "/tests/"):
        return
    e = opens.setdefault(os.path.relpath(p, ROOT), {"tests": set(), "files": set()})
    if state["cur"]:
        e["tests"].add(state["cur"])
    else:
        e["files"].add(state["file"] or "*")


def _open(file, *a, **k):
    if isinstance(file, (str, bytes, os.PathLike)):
        _rec(file)
    return _orig(file, *a, **k)


builtins.open = _open
io.open = _open


def pytest_configure(config):
    import sqlglot
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        try:
            importlib.import_module(m.name)
        except BaseException:
            pass


def pytest_collectstart(collector):
    state["file"] = collector.nodeid.split("::")[0]


def pytest_collection_finish(session):
    state["file"] = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    state["cur"], state["file"] = None, item.nodeid.split("::")[0]
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    state["cur"] = item.nodeid
    yield
    state["cur"] = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    state["cur"], state["file"] = None, item.nodeid.split("::")[0]
    yield
    state["file"] = None


def pytest_runtest_logreport(report):
    if report.failed:
        failed.add(report.nodeid)


def pytest_sessionfinish(session):
    out = {k: {"tests": sorted(v["tests"]), "files": sorted(v["files"])} for k, v in opens.items()}
    with _orig(OUT, "w") as f:
        json.dump({"opens": out, "failed": sorted(failed)}, f)
