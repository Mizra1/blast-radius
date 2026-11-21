"""Blast-radius selector: per-test line coverage map (built in the image) -> tests a patch can break."""
import ast
import json
import os
import re
import sys
from pathlib import Path

DATA = Path(os.environ.get("DATA_ROOT", "/data"))
SG = Path(os.environ.get("SG_ROOT", "/opt/sqlglot"))
APP = Path(__file__).resolve().parent
MAP = Path(os.environ.get("MAP_ROOT", "/opt/map"))
WORKER_COST = 2000
INSURE_MS = int(os.environ.get("INSURE_MS", "2"))  # tests at most this long are always executed (cheap safety net)
EXEC_MAX_MS = int(os.environ.get("EXEC_MAX_MS", "2000"))  # longer candidates are printed unrun
EXEC = os.environ.get("EXEC", "1") == "1"
META_FILES = ("test_dialect_imports.py", "test_dialect_entry_points.py", "test_build.py", "test_typing.py",
              "test_time.py", "test_docs.py", "test_integration_loader.py", "test_sqlglotc.py", "test_dialect.py")
NEVER_EXEC_FILES = ("test_sqlglotc.py",)


class AllTests(Exception):
    pass


# ---------------------------------------------------------------- patch parsing
def parse_patch(text):
    files, cur = [], None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", ln)
            cur = {"old": m.group(1), "new": m.group(2), "status": "mod", "hunks": []}
            files.append(cur)
        elif cur is None:
            pass
        elif ln.startswith("new file mode"):
            cur["status"] = "new"
        elif ln.startswith("deleted file mode"):
            cur["status"] = "deleted"
        elif ln.startswith("rename ") or ln.startswith("copy "):
            cur["status"] = "rename"
        elif ln.startswith("GIT binary patch") or ln.startswith("Binary files"):
            cur["status"] = "binary"
        elif ln.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", ln)
            h = {"old_start": int(m.group(1)), "old_len": int(m.group(2) or 1), "lines": []}
            nold = h["old_len"]
            nnew = int(m.group(4) or 1)
            i += 1
            while i < len(lines) and (nold > 0 or nnew > 0):
                l = lines[i]
                if l.startswith("\\"):
                    i += 1
                    continue
                tag, body = (l[:1] or " "), l[1:]
                if tag == " ":
                    nold -= 1
                    nnew -= 1
                elif tag == "-":
                    nold -= 1
                elif tag == "+":
                    nnew -= 1
                else:
                    break
                h["lines"].append((tag, body))
                i += 1
            cur["hunks"].append(h)
            continue
        i += 1
    return files


def is_code(s):
    s = s.strip()
    return bool(s) and not s.startswith("#")


def anchors_and_new(old_src, hunks, all_code=False):
    """Old-file line numbers touched by code changes, plus the patched source."""
    old_lines = old_src.split("\n")
    out, anchors = [], set()
    pos = 0  # next old line index (0-based) to copy
    for h in hunks:
        start = h["old_start"] - 1 if h["old_len"] else h["old_start"]
        out.extend(old_lines[pos:start])
        o = start  # 0-based index of next old line
        removed = False  # current change block removed code (so its + lines are a replacement)
        for tag, body in h["lines"]:
            if tag == " ":
                out.append(body)
                o += 1
                removed = False
            elif tag == "-":
                if all_code or is_code(body):
                    anchors.add(o + 1)
                    removed = True
                o += 1
            else:
                out.append(body)
                if (all_code or is_code(body)) and not removed:
                    anchors.add(max(o, 1))  # line before insertion
                    anchors.add(o + 1)  # line after insertion
        pos = o
    out.extend(old_lines[pos:])
    return anchors, "\n".join(out)


def stmt_spans(tree):
    spans = []

    def walk(body, in_func):
        for st in body:
            body = getattr(st, "body", None)
            hdr = body[0].lineno - 1 if isinstance(body, list) and body and isinstance(body[0], ast.stmt) else st.end_lineno
            hdr = max(st.lineno, min(hdr, st.end_lineno))
            spans.append((st.lineno, st.end_lineno, in_func, hdr))
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(st.body, True)
            else:
                for f in ("body", "orelse", "finalbody"):
                    v = getattr(st, f, None)
                    if isinstance(v, list) and v and isinstance(v[0], ast.stmt):
                        walk(v, in_func)
                for h in getattr(st, "handlers", []) or []:
                    walk(h.body, in_func)
                for c in getattr(st, "cases", []) or []:
                    walk(c.body, in_func)

    walk(tree.body, False)
    return spans


def locate(spans, line):
    best = None
    for s, e, fn, h in spans:
        if s <= line <= e and (best is None or (e - s, -s) < (best[1] - best[0], -best[0])):
            best = (s, e, fn, h)
    return ((best[0], best[3]), best[2]) if best else (None, False)


# ---------------------------------------------------------------- selection
class Ctx:
    def __init__(self):
        self.tests = json.loads((DATA / "tests.json").read_text())["tests"]
        self.all = [t["number"] for t in self.tests]
        self.dur = {t["number"]: round(t["seconds"] * 1000) for t in self.tests}
        self.by_file = {}
        self.by_id = {}
        self.by_key = {}
        for t in self.tests:
            path, _, rest = t["id"].partition("::")
            self.by_file.setdefault(path, []).append(t["number"])
            self.by_id.setdefault(re.sub(r"\[.*$", "", t["id"]), []).append(t["number"])
            parts = rest.split("::")
            stem = os.path.basename(path)[:-3]
            self.by_key.setdefault(".".join([stem] + [re.sub(r"\[.*$", "", p) for p in parts]), []).append(t["number"])
        self.insurance = {n for n in self.all if self.dur[n] <= INSURE_MS}
        self.id_of = {t["number"]: t["id"] for t in self.tests}
        for path, nums in self.by_file.items():
            if os.path.basename(path) in META_FILES and path != "tests/dialects/test_dialect.py":
                self.insurance.update(nums)
        self.ctx_names = json.loads((MAP / "contexts.json").read_text())
        self.ctx_cache = {}
        self.fixtures = json.loads((MAP / "fixtures.json").read_text())
        up = MAP / "uses.json"
        self.uses = json.loads(up.read_text()) if up.exists() else {}
        self.incomplete = set()
        inc = json.loads((MAP / "incomplete.json").read_text())
        for fp in inc["files"]:
            self.incomplete.update(self.by_file.get(fp, []))
        for tid in inc["tests"]:
            self.incomplete.update(self.by_id.get(re.sub(r"\[.*$", "", tid), []))

    def ctx_tests(self, idx):
        r = self.ctx_cache.get(idx)
        if r is None:
            parts = self.ctx_names[idx].split(".")
            r = self.by_key.get(".".join(parts[-3:])) or self.by_key.get(".".join(parts[-2:])) or []
            self.ctx_cache[idx] = r
        return r


def load_linemap(rel):
    p = MAP / "files" / (rel.replace("/", "__") + ".json")
    if not p.exists():
        return None
    return json.loads(p.read_text())


def entry_tests(cx, e):
    sel = set()
    if not e:
        return sel
    for tid in e["tests"]:
        sel.update(cx.by_id.get(re.sub(r"\[.*$", "", tid), []))
    for fp in e["files"]:
        if fp == "*":
            raise AllTests()
        sel.update(cx.by_file.get(fp, []))
    return sel


def select_py_source(cx, rel, f):
    p = SG / rel
    old_src = p.read_text(encoding="utf-8")
    anchors, new_src = anchors_and_new(old_src, f["hunks"])
    compile(new_src, rel, "exec")  # syntax error -> every test importing it fails
    old_tree = ast.parse(old_src)
    same = ast.dump(old_tree) == ast.dump(ast.parse(new_src))
    reads = entry_tests(cx, cx.fixtures.get(rel))  # tests that read this file as text
    if same:
        return reads  # comment/whitespace only: only tests that read the source text can notice
    if not anchors:  # a "comment" inside a string literal, etc: count every changed line
        anchors, _ = anchors_and_new(old_src, f["hunks"], all_code=True)
    lm = load_linemap(rel) or {}
    spans = stmt_spans(old_tree)
    sel = set(reads)
    union_done = False
    for a in anchors:
        start, in_func = locate(spans, a)
        if in_func:
            ctxs = set()
            for ln in range(start[0], start[1] + 1):
                ctxs.update(lm.get(str(ln), ()))
            if -1 in ctxs:
                raise AllTests()
            for c in ctxs:
                sel.update(cx.ctx_tests(c))
        elif not union_done:
            union_done = True
            seen = set()
            for ctxs in lm.values():
                seen.update(ctxs)
            for c in seen:
                if c >= 0:
                    sel.update(cx.ctx_tests(c))
            sel |= entry_tests(cx, cx.uses.get(rel))  # tests that instantiate/import things from this file
            stem = os.path.basename(rel)[:-3]
            for path, nums in cx.by_file.items():
                if os.path.basename(path) == f"test_{stem}.py":
                    sel.update(nums)
    return sel


def select_fixture(cx, rel):
    e = cx.fixtures.get(rel)
    sel = set()
    if e:
        for tid in e["tests"]:
            sel.update(cx.by_id.get(tid, []))
        for fpath in e["files"]:
            if fpath == "*":
                raise AllTests()
            sel.update(cx.by_file.get(fpath, []))
        return sel
    base = os.path.basename(rel)
    parent = os.path.dirname(rel)[len("tests/"):] if rel.startswith("tests/") else parent_fallback(rel)
    for path, nums in cx.by_file.items():
        try:
            src = (SG / path).read_text(encoding="utf-8")
        except OSError:
            continue
        if base in src or parent in src:
            sel.update(nums)
    if not sel:
        raise AllTests()
    return sel


def parent_fallback(rel):
    return os.path.dirname(rel)


def select_file(cx, f):
    st = f["status"]
    rel = f["new"]
    if st in ("new", "deleted", "rename", "binary"):
        if rel.endswith((".md", ".txt", ".png", ".rst")):
            return set()
        raise AllTests()
    if rel.startswith("tests/fixtures/"):
        return select_fixture(cx, rel)
    if rel.startswith("tests/") and rel.endswith(".py"):
        if rel in cx.by_file:
            return set(cx.by_file[rel])
        raise AllTests()
    if rel.startswith("sqlglot/") and rel.endswith(".py"):
        return select_py_source(cx, rel, f)
    if rel.endswith((".md", ".txt", ".png", ".rst")) or rel.startswith(("posts/", "benchmarks/", "pdoc/")):
        return set()
    raise AllTests()


def select(cx, text):
    sel = set()
    for f in parse_patch(text):
        sel |= select_file(cx, f)
    return sel


def split(cx, sel):
    if not sel:
        return []
    ds = sorted(((cx.dur[n], n) for n in sel), reverse=True)
    best = None
    for k in range(1, min(8, len(ds)) + 1):
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for d, n in ds:
            j = loads.index(min(loads))
            loads[j] += d
            ws[j].append(n)
        c = max(loads) + WORKER_COST * k
        if best is None or c < best[0]:
            best = (c, ws)
    return best[1]



def build_job(cx, text, files):
    """{abs path: patched text} for every file in the patch, or None if it can't be modelled."""
    out = {}
    for f in files:
        if f["status"] != "mod" or not f["hunks"]:
            return None
        p = SG / f["new"]
        try:
            old = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        _, new = anchors_and_new(old, f["hunks"])
        out[str(p)] = new
    return out


def exec_ids(cx, nums):
    runnable, blind = [], set()
    for n in nums:
        tid = cx.id_of[n]
        path, _, rest = tid.partition("::")
        parts = rest.split("::")
        if (cx.dur[n] > EXEC_MAX_MS or len(parts) != 2 or "[" in tid
                or os.path.basename(path) in NEVER_EXEC_FILES):
            blind.add(n)
            continue
        runnable.append({"id": tid, "mod": path[:-3].replace("/", "."), "cls": parts[0], "name": parts[1]})
    return runnable, blind


_duck = {}


def uses_duckdb(path):
    v = _duck.get(path)
    if v is None:
        try:
            v = bool(re.search(r"^\s*(import|from) (duckdb|pandas|numpy)", (SG / path).read_text(encoding="utf-8"), re.M))
        except OSError:
            v = True
        _duck[path] = v
    return v


def _attempt(runnable, files_job, timeout, tag):
    """One runner subprocess; returns {"failed": [...], "unrun": [...]} or None if it crashed."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        ov, jb, res = f"{d}/ov.json", f"{d}/job.json", f"{d}/res.json"
        json.dump(files_job, open(ov, "w"))
        json.dump({"tests": runnable}, open(jb, "w"))
        env = dict(os.environ, BR_OVERLAY=ov, PYTHONPATH=f"{APP}/overlay:{SG}", PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPYCACHEPREFIX="/nonexistent-pyc", HOME="/tmp")
        try:
            subprocess.run([sys.executable, str(APP / "runner.py"), jb, res], cwd=str(SG), env=env,
                           timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
            return json.load(open(res))
        except subprocess.CalledProcessError as e:
            print(f"{tag}: runner crashed rc={e.returncode}: {(e.stderr or b'')[-200:]!r}", file=sys.stderr)
        except Exception as e:
            print(f"{tag}: runner error {type(e).__name__}", file=sys.stderr)
        return None


def run_exec(cx, name, files_job, nums, timeout):
    runnable, blind = exec_ids(cx, nums)
    if not runnable:
        return set(blind)
    r = _attempt(runnable, files_job, timeout, name)
    if r is None:
        # e.g. duckdb/numpy cannot run on this CPU: retry without the modules that import them
        safe = [t for t in runnable if not uses_duckdb(t["id"].partition("::")[0])]
        if safe and len(safe) < len(runnable):
            r = _attempt(safe, files_job, timeout, name + " (retry)")
            if r is not None:
                blind.update(n for t in runnable if t not in safe for n in cx.by_id.get(t["id"], []))
    if r is None:
        print(f"{name}: exec failed, printing all candidates", file=sys.stderr)
        return set(nums)
    final = set(blind)
    for tid in r["failed"] + r["unrun"]:
        final.update(cx.by_id.get(tid, []))
    return final


def main():
    import time
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    cx = Ctx() if (MAP / "OK").exists() else None
    if cx is None:
        tests = json.loads((DATA / "tests.json").read_text())["tests"]
        cx = type("C", (), {})()
        cx.all = [t["number"] for t in tests]
        cx.dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    patches = sorted((DATA / "commits").glob("*.patch"))
    plans = {}  # commit -> (static candidate set, exec job or None)
    for patch in patches:
        job = None
        try:
            if not hasattr(cx, "ctx_names"):
                raise AllTests()
            text = patch.read_text(encoding="utf-8")
            sel = select(cx, text)
            files = parse_patch(text)
            if any(not f["new"].endswith((".md", ".txt", ".png", ".rst")) for f in files):
                sel |= cx.insurance
            if sel and cx.incomplete:
                sel |= cx.incomplete
            if EXEC and sel:
                job = build_job(cx, text, files)
        except Exception as e:  # anything unexpected: be safe
            print(f"{patch.stem}: fallback ALL ({type(e).__name__}: {e})", file=sys.stderr)
            sel = set(cx.all)
        plans[patch.stem] = (sel, job)

    deadline = t0 + 4.2 * len(patches)  # batch limit is ~6 s per commit; keep a margin

    def finish(name):
        sel, job = plans[name]
        if job is None or not sel:
            return sel
        left = deadline - time.time()
        if left < 5:
            return sel  # out of time budget: print the static candidates
        try:
            return run_exec(cx, name, job, sel, min(90, left))
        except Exception as e:
            print(f"{name}: exec error {type(e).__name__}: {e}", file=sys.stderr)
            return sel

    names = sorted(plans, key=lambda n: -len(plans[n][0]))  # biggest first for better packing
    with ThreadPoolExecutor(2) as ex:
        finals = dict(zip(names, ex.map(finish, names)))
    for name in sorted(finals):
        print(json.dumps({"commit": name, "workers": split(cx, finals[name])}))
        print(f"{name}: candidates {len(plans[name][0])} -> final {len(finals[name])}", file=sys.stderr)


if __name__ == "__main__":
    main()
