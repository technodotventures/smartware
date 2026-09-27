// Seeded 3-bug fixture. Intentionally buggy; the agent must fix all three.
export function sum(a, b) { return a + b + 1; }          // bug-1: off by one
export function parseId(s) { return s.split('-')[0]; }     // bug-2: wrong delimiter
export function formatDate(y, m, d) { return `${y}-${m + 1}-${d}`; } // bug-3: off-by-one month

// Self-test that must pass after the fixes.
export function selfTest() {
  return [
    sum(2, 3) === 5 ? 'pass' : 'fail:sum',
    parseId('a:b') === 'a' ? 'pass' : 'fail:parseId',
    formatDate(2026, 1, 15) === '2026-01-15' ? 'pass' : 'fail:formatDate',
  ];
}
