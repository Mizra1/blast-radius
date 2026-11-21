"""Run the given unittest-style tests (already running under the patched overlay); report failures."""
import importlib
import json
import os
import re
import signal
import sys
import unittest

os.environ["SKIP_INTEGRATION"] = "0"
job = json.load(open(sys.argv[1]))
out_path = sys.argv[2]
sys.path.insert(0, os.environ.get("SG_ROOT", "/opt/sqlglot"))

by_key = {}
unrun, failed = [], set()
suite_by_mod = {}
for t in job["tests"]:
    by_key[f'{t["mod"]}.{t["cls"]}.{t["name"]}'] = t
    suite_by_mod.setdefault(t["mod"], []).append(t)


class Timeout(Exception):
    pass


def on_alarm(signum, frame):
    raise Timeout("test timed out")


signal.signal(signal.SIGALRM, on_alarm)


class R(unittest.TestResult):
    def _mark(self, test):
        k = test.id()
        if k in by_key:
            failed.add(by_key[k]["id"])
            return
        m = re.search(r"\(([\w.]+)\)", str(test))  # setUpClass (module.Class) etc.
        if m:
            for kk, t in by_key.items():
                if kk.startswith(m.group(1) + "."):
                    failed.add(t["id"])

    def startTest(self, test):
        super().startTest(test)
        signal.setitimer(signal.ITIMER_REAL, 20)

    def stopTest(self, test):
        signal.setitimer(signal.ITIMER_REAL, 0)
        super().stopTest(test)

    def addFailure(self, test, err):
        self._mark(test)

    def addError(self, test, err):
        self._mark(test)

    def addUnexpectedSuccess(self, test):
        self._mark(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._mark(test)


devnull = open(os.devnull, "w")
sys.stdout = sys.stderr = devnull
result = R()
for mod, items in suite_by_mod.items():
    try:
        m = importlib.import_module(mod)
    except BaseException:
        unrun.extend(t["id"] for t in items)
        continue
    suite = unittest.TestSuite()
    items.sort(key=lambda t: t["cls"])
    for t in items:
        cls = getattr(m, t["cls"], None)
        if not (isinstance(cls, type) and issubclass(cls, unittest.TestCase)) or not hasattr(cls, t["name"]):
            unrun.append(t["id"])
            continue
        suite.addTest(cls(t["name"]))
    try:
        suite.run(result)
    except BaseException:
        unrun.extend(t["id"] for t in items)

with open(out_path, "w") as f:
    json.dump({"failed": sorted(failed), "unrun": sorted(set(unrun))}, f)
    f.flush()
os._exit(0)
