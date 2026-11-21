"""Build per-test line coverage + fixture-open maps from the pinned sqlglot checkout."""
import glob, json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

SG = "/opt/sqlglot"
MAP = "/opt/map"
TMP = "/tmp/mapbuild"
os.makedirs(MAP + "/files", exist_ok=True)
os.makedirs(TMP, exist_ok=True)

rc = TMP + "/cov.rc"
open(rc, "w").write("[run]\nsource = sqlglot\ndynamic_context = test_function\n")

files = sorted(glob.glob(SG + "/tests/**/test_*.py", recursive=True))
rel = [os.path.relpath(f, SG) for f in files]
order = {"test_executor.py": 0, "test_optimizer.py": 1, "test_parser.py": 2}
rel.sort(key=lambda r: (order.get(os.path.basename(r), 3), r))
groups = [[r] for r in rel]  # one process per test file: a crash loses only that file
status = {}
LOG = open(MAP + "/build.log", "w")


degraded_failed = set()
LADDER = ["", "duckdb", "duckdb,pandas,numpy"]


def run(i):
    for level, block in enumerate(LADDER):
        for stale in (f"{TMP}/cov{i}", f"{TMP}/trace{i}.json"):
            if os.path.exists(stale):
                os.remove(stale)
        r = run_once(i, block)
        if r.returncode in (0, 1, 5):
            break  # not a crash
        if r.returncode > 0:
            break  # collection/usage error: retrying won't help
    ok = r.returncode in (0, 1, 5) and os.path.exists(f"{TMP}/cov{i}") or r.returncode == 5
    if ok and level > 0:
        try:
            degraded_failed.update(json.load(open(f"{TMP}/trace{i}.json"))["failed"])
        except Exception:
            ok = False
    status[groups[i][0]] = ok
    msg = f"{groups[i][0]}: rc={r.returncode} level={level} ok={ok} | {r.stdout.strip()[-200:]!r} {r.stderr.strip()[-200:]!r}"
    print(msg, file=sys.stderr)
    LOG.write(msg + "\n")
    LOG.flush()


def run_once(i, block):
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=f"{SG}:/build", MAP_ROOT=SG,
               MAPTRACE_OUT=f"{TMP}/trace{i}.json", COVERAGE_FILE=f"{TMP}/cov{i}",
               PYTHONDONTWRITEBYTECODE="1", BLOCK_MODULES=block)
    cmd = [sys.executable, "-m", "coverage", "run", f"--rcfile={rc}", "-m", "pytest",
           "-p", "maptrace", "-p", "no:cacheprovider", "-q", "--tb=no", "--timeout=600"] + groups[i]
    return subprocess.run(cmd, cwd=SG, env=env, capture_output=True, text=True)


with ThreadPoolExecutor(3) as ex:
    list(ex.map(run, range(len(groups))))

from coverage import CoverageData

ctx_idx = {}
ctx_names = []
perfile = {}  # relpath -> {line: set(idx)}
for i in range(len(groups)):
    p = f"{TMP}/cov{i}"
    if not os.path.exists(p):
        continue
    d = CoverageData(basename=p)
    d.read()
    for f in d.measured_files():
        r = os.path.relpath(f, SG)
        if not r.startswith("sqlglot/"):
            continue
        m = perfile.setdefault(r, {})
        for line, ctxs in d.contexts_by_lineno(f).items():
            s = m.setdefault(line, set())
            for c in ctxs:
                if c == "":
                    s.add(-1)
                else:
                    if c not in ctx_idx:
                        ctx_idx[c] = len(ctx_names)
                        ctx_names.append(c)
                    s.add(ctx_idx[c])

if not ctx_names:
    sys.exit("no contexts recorded")
for r, m in perfile.items():
    json.dump({str(k): sorted(v) for k, v in m.items()}, open(f"{MAP}/files/{r.replace('/', '__')}.json", "w"))
json.dump(ctx_names, open(MAP + "/contexts.json", "w"))

json.dump({"files": sorted(f for f, ok in status.items() if not ok), "tests": sorted(degraded_failed)},
          open(MAP + "/incomplete.json", "w"))
fx = {}
for i in range(len(groups)):
    p = f"{TMP}/trace{i}.json"
    if os.path.exists(p):
        for k, v in json.load(open(p)).get("opens", {}).items():
            if not k.startswith("tests/fixtures/"):
                continue
            e = fx.setdefault(k, {"tests": set(), "files": set()})
            e["tests"].update(v["tests"])
            e["files"].update(v["files"])
json.dump({k: {"tests": sorted(v["tests"]), "files": sorted(v["files"])} for k, v in fx.items()},
          open(MAP + "/fixtures.json", "w"))
open(MAP + "/OK", "w").write("ok")
print(f"map: {len(perfile)} files, {len(ctx_names)} contexts, {len(fx)} fixture files", file=sys.stderr)
