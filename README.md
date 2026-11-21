# Blast radius selector
Build (Dockerfile): download sqlglot @ eb11aec, run its tests under per-test line coverage (+ fixture-open tracing), store maps in /opt/map.
Run (selector.py): for each patch, map changed statements -> tests that executed them; fixtures -> tests that opened them; unknown shapes, syntax errors, import-time function lines -> run everything. Pure comment edits -> nothing. Workers chosen to minimise makespan + 2s/worker.
Test with `make sample`.
