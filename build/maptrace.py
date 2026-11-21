"""pytest plugin used only while building the map: records fixture/file opens per test."""
import builtins, importlib, io, json, os, pkgutil, sys
import pytest

ROOT = os.environ.get("MAP_ROOT", "/opt/sqlglot")
OUT = os.environ.get("MAPTRACE_OUT", "/tmp/maptrace.json")
state = {"cur": None, "file": None}
opens = {}
uses = {}
failed = set()
seen = set()
_cls_files = {}
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
    if not (p.startswith(ROOT + "/tests/") or (p.startswith(ROOT + "/sqlglot/") and p.endswith(".py"))):
        return
    e = opens.setdefault(os.path.relpath(p, ROOT), {"tests": set(), "files": set()})
    if state["cur"]:
        e["tests"].add(state["cur"])
    else:
        e["files"].add(state["file"] or "*")


def _target():
    if state["cur"]:
        return "tests", state["cur"]
    if state["file"]:
        return "files", state["file"]
    return None


def _add_use(relfile):
    t = _target()
    if t:
        uses.setdefault(relfile, {"tests": set(), "files": set()})[t[0]].add(t[1])


def _note(t):
    seen.add(t)
    fl = _cls_files.get(t)
    if fl is None:
        fl = []
        for c in t.__mro__:
            f = getattr(sys.modules.get(c.__module__), "__file__", None)
            if f and f.startswith(ROOT + "/sqlglot/"):
                fl.append(os.path.relpath(f, ROOT))
        _cls_files[t] = fl
    for f in fl:
        _add_use(f)


def _wrap_init(cls):
    orig = cls.__dict__.get("__init__")
    if orig is None:
        return

    def __init__(self, *a, **k):
        t = type(self)
        if t not in seen:
            _note(t)
        return orig(self, *a, **k)

    cls.__init__ = __init__


def _install_hooks():
    import sqlglot  # noqa
    from sqlglot import exp, generator, parser, tokens
    from sqlglot.dialects.dialect import Dialect

    for c in (exp.Expr, parser.Parser, generator.Generator, tokens.Tokenizer, Dialect):
        try:
            _wrap_init(c)
        except Exception:
            pass
    try:
        orig = Dialect.__dict__["get_or_raise"].__func__

        def get_or_raise(cls, dialect):
            r = orig(cls, dialect)
            if type(r) not in seen:
                _note(type(r))
            return r

        Dialect.get_or_raise = classmethod(get_or_raise)
    except Exception:
        pass
    _oim = importlib.import_module

    def import_module(name, package=None):
        r = _oim(name, package)
        f = getattr(r, "__file__", None)
        if f and f.startswith(ROOT + "/sqlglot/"):
            _add_use(os.path.relpath(f, ROOT))
        return r

    importlib.import_module = import_module


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
    try:
        _install_hooks()
    except BaseException:
        pass


def pytest_collectstart(collector):
    state["file"] = collector.nodeid.split("::")[0]


def pytest_collection_finish(session):
    state["file"] = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    state["cur"], state["file"] = None, item.nodeid.split("::")[0]
    seen.clear()
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    state["cur"] = item.nodeid
    seen.clear()
    yield
    state["cur"] = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    state["cur"], state["file"] = None, item.nodeid.split("::")[0]
    seen.clear()
    yield
    state["file"] = None


def pytest_runtest_logreport(report):
    if report.failed:
        failed.add(report.nodeid)


def pytest_sessionfinish(session):
    out = {k: {"tests": sorted(v["tests"]), "files": sorted(v["files"])} for k, v in opens.items()}
    with _orig(OUT, "w") as f:
        json.dump({"opens": out, "failed": sorted(failed),
                   "uses": {k: {"tests": sorted(v["tests"]), "files": sorted(v["files"])} for k, v in uses.items()}}, f)
