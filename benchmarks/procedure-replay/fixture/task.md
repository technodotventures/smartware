# Procedure-replay coding fixture: three seeded bugs

A minimal JavaScript module `repo/index.js` carries three intentional bugs. The
agent must fix all three so the self-test passes. Fresh state is restored before
every run (the runner re-reads `repo/index.js` as the pristine snapshot).

Bugs:
1. `sum` returns `a + b + 1` (off by one). Fix: return `a + b`.
2. `parseId` splits on `-` instead of `:`. Fix: split on `:`.
3. `formatDate` prints month `m + 1`. Fix: print month `m`.

Expected: `selfTest()` returns `[pass, pass, pass]` after the fixes.
